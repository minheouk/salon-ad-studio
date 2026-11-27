"""접속과 별개로 채팅을 정리하고 만료 안내를 발송합니다."""

import argparse
import hashlib
import json
import logging
import os
from datetime import datetime
from threading import Event
from time import time
from urllib.error import URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

import psycopg

from .chat_store import RETENTION_SECONDS, ChatStore

POLL_SECONDS = 60
RETRY_SECONDS = 5 * 60
NOTICE_BATCH = 25
LOGGER = logging.getLogger(__name__)
RESEND_ENDPOINT = "https://api.resend.com/emails"


class RetentionMailer:
    """가입 이메일로 채팅 삭제 예정일을 안내합니다."""

    # 만료 안내에 사용할 Resend 키와 발신 주소를 보관합니다.
    def __init__(self, api_key, sender):
        self.api_key = api_key
        self.sender = sender

    # 네트워크 재시도에서 같은 삭제 예정 안내가 중복 접수되는 것을 막습니다.
    def send(self, chat, email):
        expiry = datetime.fromtimestamp(chat["updated"] + RETENTION_SECONDS, ZoneInfo("Asia/Seoul"))
        version = f'{chat["id"]}:{chat["updated"]}'
        key = hashlib.sha256(version.encode()).hexdigest()
        body = json.dumps({
            "from": self.sender,
            "to": [email],
            "subject": "[원컷 네일] 채팅 삭제 예정 안내",
            "text": (
                f"원컷 네일 채팅이 {expiry:%Y-%m-%d %H:%M} (한국 시간)에 삭제될 예정입니다.\n"
                "삭제 시 첨부 사진과 광고 결과물도 함께 제거됩니다.\n"
                "채팅에서 작업을 계속하면 마지막 수정 시점부터 보관 기간이 다시 계산됩니다."
            ),
        }).encode()
        request = Request(RESEND_ENDPOINT, data=body, headers={
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "User-Agent": "OneCutNail-Retention/1.0",
            "Idempotency-Key": f"chat-expiry/{key}",
        })
        with urlopen(request, timeout=10):
            return


# 발송할 채팅을 잠근 뒤 메일을 보내고 발송 또는 재시도 시점을 기록합니다.
def notify_one(store, mailer):
    with store.db.transaction():
        chat = store.due_notice()
        if chat is None:
            return False
        email = store.confirmed_email(chat["owner"])
        if email and chat["updated"] + RETENTION_SECONDS > time():
            try:
                mailer.send(chat, email)
            except (URLError, OSError) as error:
                store.delay_notice(chat["id"], time() + RETRY_SECONDS)
                LOGGER.warning("채팅 만료 안내 발송 실패 (%s). 다음 실행에서 다시 시도합니다.",
                               getattr(error, "code", type(error).__name__))
                return True
        store.mark_notice(chat)
    return True


# 한 번에 처리하는 메일 수를 제한해 다음 삭제 작업이 오래 밀리지 않게 합니다.
def retention_pass(store, stop, mailer):
    store.prune()
    if not mailer:
        return
    for _ in range(NOTICE_BATCH):
        if stop.is_set() or not notify_one(store, mailer):
            break


# 발송 설정 없이도 만료 삭제 작업은 실행합니다.
def configured_mailer():
    api_key = os.environ.get("RESEND_API_KEY")
    sender = os.environ.get("ONECUT_RETENTION_FROM")
    if api_key and sender:
        return RetentionMailer(api_key, sender)
    LOGGER.warning("RESEND_API_KEY와 ONECUT_RETENTION_FROM이 없어 만료 이메일은 발송하지 않습니다.")
    return None


# 연결 오류가 생기면 다음 실행에서 새 연결을 엽니다.
def run_retention(database_url, stop, *, once=False):
    mailer = configured_mailer()
    store = None
    try:
        while not stop.is_set():
            try:
                if store is None:
                    store = ChatStore(database_url)
                retention_pass(store, stop, mailer)
            except psycopg.Error as error:
                LOGGER.warning("채팅 보관 작업 실패 (%s).", type(error).__name__)
                if store:
                    store.db.close()
                    store = None
                if once:
                    return False
            if once:
                return True
            stop.wait(POLL_SECONDS)
    finally:
        if store:
            store.db.close()
    return True


# 웹 서버가 상시 실행되지 않는 환경에서 예약 작업으로 한 번 실행합니다.
def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    database_url = os.environ.get("SUPABASE_DB_URL")
    if not database_url:
        parser.exit(2, "SUPABASE_DB_URL 환경 변수를 설정해 주세요.\n")
    if not run_retention(database_url, Event(), once=True):
        parser.exit(1, "채팅 보관 작업을 실행하지 못했습니다.\n")


if __name__ == "__main__":
    main()
