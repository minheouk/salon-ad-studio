"""서비스 채팅과 이미지 버전을 Supabase Postgres에 저장합니다."""

import json
from time import time

import psycopg
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row

CHAT_LIMIT = 5
RETENTION_SECONDS = 120 * 60 * 60
NOTICE_SECONDS = 24 * 60 * 60


class ChatStore:
    """채팅과 결과 파일을 같은 데이터베이스에서 관리합니다."""

    # 서버 전용 스키마를 준비하고 외부 Data API에 노출하지 않습니다.
    def __init__(self, database_url: str, *, initialize=True):
        self.connection_options = conninfo_to_dict(database_url)
        if self.connection_options.get("sslmode") not in {"require", "verify-ca", "verify-full"}:
            self.connection_options["sslmode"] = "require"
        self.connection_options.setdefault("connect_timeout", "10")
        self.db = self.connect()
        if initialize:
            try:
                self.initialize_schema()
            except psycopg.Error:
                self.db.close()
                raise

    # 스키마 초기화는 서버 시작 시에만 실행합니다.
    def initialize_schema(self):
        with self.db.transaction():
            self.db.execute("SELECT pg_advisory_xact_lock(47205131)")
            self.db.execute("CREATE SCHEMA IF NOT EXISTS onecut_web")
            self.db.execute("""
                CREATE TABLE IF NOT EXISTS onecut_web.chats (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, updated DOUBLE PRECISION NOT NULL,
                    draft TEXT NOT NULL DEFAULT '{}', photo BYTEA, photo_name TEXT,
                    photo_type TEXT, fields TEXT, result TEXT, edits INTEGER NOT NULL DEFAULT 0
                )
            """)
            self.db.execute("""
                CREATE TABLE IF NOT EXISTS onecut_web.images (
                    id TEXT PRIMARY KEY, chat_id TEXT NOT NULL
                    REFERENCES onecut_web.chats(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL, content BYTEA NOT NULL
                )
            """)
            self.db.execute("""
                ALTER TABLE onecut_web.chats
                    ADD COLUMN IF NOT EXISTS notice_sent_for DOUBLE PRECISION,
                    ADD COLUMN IF NOT EXISTS notice_retry_at DOUBLE PRECISION
            """)
            self.db.execute("CREATE INDEX IF NOT EXISTS chats_owner_updated ON onecut_web.chats (owner, updated)")
            self.db.execute("CREATE INDEX IF NOT EXISTS chats_updated ON onecut_web.chats (updated)")
            self.db.execute("CREATE INDEX IF NOT EXISTS images_chat_id ON onecut_web.images (chat_id)")

    # 작업마다 독립 연결을 만듭니다.
    def connect(self):
        return psycopg.connect(autocommit=True, row_factory=dict_row, **self.connection_options)

    # 같은 브라우저의 저장과 수정 횟수 변경을 순차 처리합니다.
    def lock_owner(self, owner: str):
        self.db.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (owner,))

    # 채팅 ID가 동시에 등록되면 소유권 검사를 순차 처리합니다.
    def lock_chat(self, chat_id: str):
        self.db.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 1))", (chat_id,))

    # 만료된 채팅과 이미지 버전을 함께 정리합니다.
    def prune(self):
        self.db.execute("""
            DELETE FROM onecut_web.chats WHERE id IN (
                SELECT id FROM onecut_web.chats WHERE updated <= %s FOR UPDATE SKIP LOCKED
            )
        """, (time() - RETENTION_SECONDS,))

    # 메타데이터 조회에서는 첨부 원본 바이트를 읽지 않습니다.
    def chat(self, chat_id, *, lock=False, include_expired=False, include_photo=False):
        columns = "id, owner, updated, draft, photo_name, photo_type, fields, result, edits"
        if include_photo:
            columns += ", photo"
        query = f"SELECT {columns} FROM onecut_web.chats WHERE id = %s"
        params = (chat_id,)
        if not include_expired:
            query += " AND updated > %s"
            params += (time() - RETENTION_SECONDS,)
        if lock:
            query += " FOR UPDATE"
        return self.db.execute(query, params).fetchone()

    # 계정의 만료되지 않은 채팅을 최신순으로 반환합니다.
    def list(self, owner):
        return self.db.execute(
            "SELECT id, updated, draft FROM onecut_web.chats "
            "WHERE owner = %s AND updated > %s ORDER BY updated DESC", (owner, time() - RETENTION_SECONDS)
        ).fetchall()

    # 초안 저장과 첨부 파일 변경을 구분해 기존 바이너리를 다시 쓰지 않습니다.
    def save(self, chat_id, owner, draft, photo=None, keep_photo=False):
        self.db.execute("""
            INSERT INTO onecut_web.chats (id, owner, updated, draft)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT(id) DO UPDATE SET updated=excluded.updated, draft=excluded.draft,
                notice_retry_at=NULL
        """, (chat_id, owner, time(), json.dumps(draft)))
        if not keep_photo:
            values = photo or (None, None, None)
            self.db.execute("""
                UPDATE onecut_web.chats AS chat
                SET photo=incoming.photo, photo_name=incoming.name, photo_type=incoming.mime
                FROM (VALUES (%s::bytea, %s::text, %s::text)) AS incoming(photo, name, mime)
                WHERE chat.id=%s AND (chat.photo, chat.photo_name, chat.photo_type)
                    IS DISTINCT FROM (incoming.photo, incoming.name, incoming.mime)
            """, (*values, chat_id))

    # 새 생성 결과나 수정 결과를 기록하고 보관 기간을 갱신합니다.
    def result(self, chat_id, fields, result, edits):
        self.db.execute(
            "UPDATE onecut_web.chats SET fields=%s, result=%s, edits=%s, updated=%s, notice_retry_at=NULL WHERE id=%s",
            (json.dumps(fields), json.dumps(result), edits, time(), chat_id),
        )

    # 이미지 버전의 원본 바이트를 반환합니다.
    def image(self, image_id):
        return self.db.execute("SELECT chat_id, kind, content FROM onecut_web.images WHERE id=%s", (image_id,)).fetchone()

    # 선택 이미지의 소유 채팅을 제한하면서 파일을 한 쿼리로 읽습니다.
    def selected_images(self, chat_id, image_ids):
        rows = self.db.execute(
            "SELECT id, content FROM onecut_web.images WHERE chat_id=%s AND id=ANY(%s)",
            (chat_id, image_ids),
        ).fetchall()
        return {row["id"]: row["content"] for row in rows}

    # 다운로드 가능한 이미지 버전을 추가합니다.
    def save_image(self, image_id, chat_id, kind, content):
        self.db.execute("INSERT INTO onecut_web.images VALUES (%s, %s, %s, %s)", (image_id, chat_id, kind, content))

    # 채팅과 관련 이미지 버전을 삭제합니다.
    def delete(self, chat_id):
        self.db.execute("DELETE FROM onecut_web.chats WHERE id=%s", (chat_id,))

    # 발송 기록과 채팅 수정이 겹치지 않도록 대상 행을 트랜잭션 종료까지 잠급니다.
    def due_notice(self):
        now = time()
        expires_after = now - RETENTION_SECONDS
        return self.db.execute("""
            SELECT id, owner, updated FROM onecut_web.chats
            WHERE updated > %s AND updated <= %s
                AND owner LIKE 'auth:%%'
                AND notice_sent_for IS DISTINCT FROM updated
                AND (notice_retry_at IS NULL OR notice_retry_at <= %s)
            ORDER BY updated LIMIT 1 FOR UPDATE SKIP LOCKED
        """, (expires_after, expires_after + NOTICE_SECONDS, now)).fetchone()

    # 발송은 완료된 가입 이메일에만 허용합니다.
    def confirmed_email(self, owner):
        row = self.db.execute(
            "SELECT email FROM auth.users WHERE id=%s AND email_confirmed_at IS NOT NULL",
            (owner.removeprefix("auth:"),),
        ).fetchone()
        return row["email"] if row else None

    # 동일한 삭제 예정 시점의 발송을 한 번만 기록합니다.
    def mark_notice(self, row):
        self.db.execute(
            "UPDATE onecut_web.chats SET notice_sent_for=%s, notice_retry_at=NULL WHERE id=%s",
            (row["updated"], row["id"]),
        )

    # 일시적인 발송 실패는 다음 실행에서 재시도합니다.
    def delay_notice(self, chat_id, retry_at):
        self.db.execute("UPDATE onecut_web.chats SET notice_retry_at=%s WHERE id=%s", (retry_at, chat_id))
