"""프런트엔드와 광고 작업 웹 API를 같은 주소에서 제공합니다."""

import argparse
import json
import os
import re
import struct
import zlib
from contextlib import contextmanager, nullcontext
from datetime import datetime
from email import policy
from email.parser import BytesParser
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Event, Thread
from time import time
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4

import psycopg

from .auth import AuthError, supabase_auth
from .chat_store import CHAT_LIMIT, RETENTION_SECONDS, ChatStore
from .model_api import ModelAPI, ModelError
from .retention import run_retention

FRONTEND = Path(__file__).resolve().parent.parent / "frontend"
MIB = 1024 * 1024
FIELDS = ("shop", "instagramId", "businessHours", "booking", "prompt")
CHAT_PREFIX = "/api/ad-sessions/"


class ApiError(Exception):
    """웹 명세의 오류 응답을 구성합니다."""

    # 웹 API 오류 응답에 상태 코드와 재시도 정보를 보관합니다.
    def __init__(self, code, message, status=HTTPStatus.BAD_REQUEST, field=None, retry_after=None):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after
        self.body = {"code": code, "message": message}
        if field:
            self.body["field"] = field


# 화면과 저장소에서 같은 보관 정책을 사용합니다.
def chat_timestamps(updated):
    return {"updatedAt": round(updated * 1000), "expiresAt": round((updated + RETENTION_SECONDS) * 1000)}


# 수정 버전을 구별할 수 있는 간단한 예시 PNG를 만듭니다.
def sample_png(kind, version):
    width, height = 360, 360
    tint = 220 + version % 18
    background = bytes((tint, 218, 235))
    accent = bytes((129, 105, 161))
    rows = []
    for y in range(height):
        stripe = (y // 25) % 2 == 0 if kind == "price_table" else 105 < y < 255
        row = background * 110 + accent * 140 + background * 110 if stripe else background * width
        rows.append(b"\x00" + row)
    chunks = [(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)),
              (b"IDAT", zlib.compress(b"".join(rows))), (b"IEND", b"")]
    return b"\x89PNG\r\n\x1a\n" + b"".join(
        struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))
        for tag, data in chunks
    )


# 서버에 저장한 결과 이미지에 식별자와 다운로드 경로를 부여합니다.
def create_image(store, session_id, kind, content):
    image_id = uuid4().hex
    store.save_image(image_id, session_id, kind, content)
    return {"image_id": image_id, "type": kind, "image_url": f"/api/images/{image_id}"}


# 서비스의 예약 URL과 필수 텍스트 필드를 검증합니다.
def validate_fields(fields):
    for name in FIELDS:
        if not fields.get(name):
            raise ApiError("INVALID_INPUT", "필수 입력값을 입력해 주세요.", field=name)
    if len(fields["prompt"]) > 200:
        raise ApiError("INVALID_INPUT", "프롬프트는 200자까지 입력할 수 있습니다.", field="prompt")
    if fields["instagramId"].startswith("@"):
        raise ApiError("INVALID_INPUT", "@를 빼고 계정명을 입력해 주세요.", field="instagramId")
    if not re.match(r"^https?://[^\s/?#]+", fields["booking"], re.IGNORECASE):
        raise ApiError("INVALID_INPUT", "올바른 예약 링크를 입력해 주세요.", field="booking")


# 업로드 파일이 요청 형식과 크기 제한에 맞는지 검증합니다.
def validate_photo(files):
    if len(files) != 1:
        raise ApiError("PHOTO_COUNT", "사진을 한 장만 첨부해 주세요.", field="images")
    image = files[0]
    data = image.get_payload(decode=True)
    if len(data) > 10 * MIB:
        raise ApiError("FILE_TOO_LARGE", "10 MiB 이하의 사진을 선택해 주세요.", field="images")
    png = image.get_content_type() == "image/png" and data.startswith(b"\x89PNG\r\n\x1a\n")
    jpeg = image.get_content_type() == "image/jpeg" and data.startswith(b"\xff\xd8\xff")
    if not (png or jpeg):
        raise ApiError("INVALID_INPUT", "JPG 또는 PNG 사진을 선택해 주세요.", field="images")


class WebHandler(SimpleHTTPRequestHandler):
    """정적 화면과 광고 작업 API를 처리합니다."""

    model = None

    # 서비스 프런트엔드 디렉터리만 정적 파일로 제공합니다.
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(FRONTEND), **kwargs)

    # HTTP 응답을 보내고 필요한 인증 쿠키와 재시도 헤더를 설정합니다.
    def reply(self, status, body=b"", content_type="application/json", filename=None, retry_after=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for cookie in getattr(self, "auth_cookies", ()):
            self.send_header("Set-Cookie", cookie)
        if retry_after is not None:
            self.send_header("Retry-After", str(retry_after))
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        self.wfile.write(body)

    # 공급자가 발급한 토큰만 브라우저의 HttpOnly 쿠키에 보관합니다.
    def set_auth_session(self, session):
        secure = "; Secure" if os.environ.get("ONECUT_SECURE_COOKIES") == "1" else ""
        self.auth_cookies = [
            f"{name}={session[key] if session else ''}; Path=/; HttpOnly; SameSite=Strict; Max-Age={age}{secure}"
            for name, key, age in (
                ("onecut_access", "access_token", int(session["expires_in"]) if session else 0),
                ("onecut_refresh", "refresh_token", 30 * 24 * 60 * 60 if session else 0),
            )
        ]

    # 토큰을 검증하고 만료되면 Supabase 세션을 갱신합니다.
    def browser_owner(self):
        if getattr(self, "auth_owner", None):
            return self.auth_owner
        cookies = SimpleCookie(self.headers.get("Cookie", ""))
        access = cookies.get("onecut_access")
        refresh = cookies.get("onecut_refresh")
        user = None
        if access:
            try:
                user = supabase_auth("user", access_token=access.value)
                self.auth_access_token = access.value
            except AuthError as error:
                if error.status != HTTPStatus.UNAUTHORIZED:
                    raise
        if user is None and refresh:
            session = supabase_auth("token?grant_type=refresh_token", {"refresh_token": refresh.value})
            self.set_auth_session(session)
            self.auth_access_token = session["access_token"]
            user = session["user"]
        if not user:
            raise AuthError(HTTPStatus.UNAUTHORIZED, "로그인이 필요합니다.")
        self.auth_owner = f'auth:{UUID(user["id"])}'
        return self.auth_owner

    # 이 브라우저의 Supabase 세션을 종료한 뒤 두 인증 쿠키를 만료시킵니다.
    def logout(self):
        try:
            self.browser_owner()
            supabase_auth("logout?scope=local", {}, access_token=self.auth_access_token)
        except AuthError as error:
            if error.status != HTTPStatus.UNAUTHORIZED:
                raise
        self.set_auth_session(None)

    # 인증 요청의 이메일을 공통 형식으로 검사합니다.
    def auth_data(self):
        data = self.read_json()
        email = data.get("email")
        if not isinstance(email, str) or len(email) > 254 or not re.fullmatch(r"[^@\s]+@[^@\s]+", email):
            raise ApiError("INVALID_EMAIL", "이메일 주소를 확인해 주세요.")
        return email, data

    # 가입과 로그인의 비밀번호 입력을 같은 방식으로 검사합니다.
    def auth_credentials(self):
        email, data = self.auth_data()
        password = data.get("password")
        if not isinstance(password, str) or not password or len(password.encode()) > 72:
            raise ApiError("INVALID_PASSWORD", "비밀번호를 확인해 주세요.")
        return {"email": email, "password": password}

    # 신규 가입은 이메일 인증을 완료해야 웹 세션을 발급합니다.
    def signup(self):
        credentials = self.auth_credentials()
        if len(credentials["password"]) < 8:
            raise ApiError("INVALID_PASSWORD", "비밀번호를 8자 이상 입력해 주세요.")
        if supabase_auth("signup", credentials).get("access_token"):
            raise ApiError("EMAIL_CONFIRMATION_DISABLED", "이메일 가입 인증 설정을 확인해 주세요.",
                           HTTPStatus.SERVICE_UNAVAILABLE)

    # 비밀번호 로그인 결과를 브라우저 세션에 반영합니다.
    def login(self):
        self.set_auth_session(supabase_auth("token?grant_type=password", self.auth_credentials()))

    # 가입 확인 코드만 다시 요청합니다.
    def resend_signup_code(self):
        email, _ = self.auth_data()
        supabase_auth("resend", {"email": email, "type": "signup"})

    # 확인 코드 검증을 마치면 인증 쿠키를 발급합니다.
    def verify_signup_code(self):
        email, data = self.auth_data()
        code = data.get("code")
        if not isinstance(code, str) or not re.fullmatch(r"[0-9]{6,10}", code):
            raise ApiError("INVALID_CODE", "이메일로 받은 인증 코드를 입력해 주세요.")
        self.set_auth_session(supabase_auth("verify", {"email": email, "token": code, "type": "email"}))

    # 인증 경로의 성공과 오류 응답 형식을 통일합니다.
    def run_auth(self, action):
        try:
            action()
            self.reply(HTTPStatus.NO_CONTENT)
        except AuthError as error:
            self.reply_error(ApiError("AUTH_ERROR", str(error), error.status))
        except ApiError as error:
            self.reply_error(error)

    # 현재 브라우저에 속한 작업만 조회합니다.
    def session(self, session_id, *, lock=False, include_photo=False):
        session = self.store.chat(session_id, lock=lock, include_photo=include_photo) if isinstance(session_id, str) else None
        if not session or session["owner"] != self.browser_owner():
            raise ApiError("NOT_FOUND", "작업을 찾을 수 없습니다. 이미지를 다시 생성해 주세요.", HTTPStatus.NOT_FOUND)
        return session

    # 서비스 오류를 공통 JSON 형식으로 반환합니다.
    def reply_error(self, error):
        self.reply(error.status, json.dumps({"error": error.body}, ensure_ascii=False).encode(), retry_after=error.retry_after)

    # 요청 본문을 읽기 전에 허용 크기를 확인합니다.
    def read_body(self, limit):
        length = self.headers.get("Content-Length", "")
        if not length.isdecimal() or len(length) > 12:
            raise ApiError("INVALID_FORM", "요청 본문을 확인해 주세요.")
        if int(length) > limit:
            self.close_connection = True
            raise ApiError("BODY_TOO_LARGE", "요청 크기가 너무 큽니다.", HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
        return self.rfile.read(int(length))

    # JSON 객체만 허용합니다.
    def read_json(self):
        if self.headers.get_content_type() != "application/json":
            raise ApiError("INVALID_FORM", "JSON 요청이 필요합니다.", HTTPStatus.UNSUPPORTED_MEDIA_TYPE)
        try:
            data = json.loads(self.read_body(64 * 1024))
        except (UnicodeError, ValueError) as error:
            raise ApiError("INVALID_FORM", "요청 본문을 확인해 주세요.") from error
        if not isinstance(data, dict):
            raise ApiError("INVALID_FORM", "요청 본문을 확인해 주세요.")
        return data

    # 멀티파트 요청의 필드와 사진을 읽습니다.
    def read_multipart(self):
        if self.headers.get_content_type() != "multipart/form-data":
            raise ApiError("INVALID_FORM", "폼 요청이 필요합니다.", HTTPStatus.UNSUPPORTED_MEDIA_TYPE)
        content_type = self.headers["Content-Type"]
        message = BytesParser(policy=policy.default).parsebytes(
            f"Content-Type: {content_type}\r\n\r\n".encode() + self.read_body(11 * MIB)
        )
        if not message.is_multipart():
            raise ApiError("INVALID_FORM", "폼 요청을 확인해 주세요.")
        return message.iter_parts()

    # 생성 요청의 입력과 서버 저장용 채팅 ID를 구분합니다.
    def generation_inputs(self):
        fields, files = {}, []
        for part in self.read_multipart():
            name = part.get_param("name", header="content-disposition")
            if name in (*FIELDS, "session_id") and not part.get_filename():
                fields[name] = part.get_payload(decode=True).decode("utf-8", errors="replace").strip()
            elif name == "images" and part.get_filename():
                files.append(part)
        validate_fields(fields)
        validate_photo(files)
        return fields, files[0]

    # 기존 채팅의 수정 횟수를 유지하며 새 사진 결과를 저장합니다.
    def generate(self):
        fields, photo = self.generation_inputs()
        session_id = fields.pop("session_id", uuid4().hex)
        current = self.ensure_chat(session_id)
        draft = json.loads(current["draft"]) if current else {}
        draft["fields"] = fields
        self.store.save(session_id, self.browser_owner(), draft, self.photo_record(photo))
        result_id = uuid4().hex
        images = self.model.generate(fields, photo.get_payload(decode=True), photo.get_content_type()) if self.model else [
            (kind, sample_png(kind, index))
            for index, kind in enumerate(("price_table", "nail_tip", "nail_tip"))
        ]
        results = [create_image(self.store, session_id, kind, content) for kind, content in images]
        result = {"session_id": session_id, "result_id": result_id, "results": results}
        self.store.result(session_id, fields, result, current["edits"] if current else 0)
        return result

    # 브라우저별 채팅 한도와 채팅 ID를 확인합니다.
    def ensure_chat(self, chat_id):
        if not re.fullmatch(r"[a-f0-9-]{32,36}", chat_id):
            raise ApiError("INVALID_INPUT", "채팅 ID를 확인해 주세요.", field="session_id")
        self.store.lock_chat(chat_id)
        current = self.store.chat(chat_id, lock=True, include_expired=True)
        if current:
            if current["owner"] != self.browser_owner() or current["updated"] + RETENTION_SECONDS <= time():
                raise ApiError("NOT_FOUND", "작업을 찾을 수 없습니다.", HTTPStatus.NOT_FOUND)
            return current
        if len(self.store.list(self.browser_owner())) >= CHAT_LIMIT:
            raise ApiError("CHAT_LIMIT_EXCEEDED", "저장된 채팅을 삭제한 뒤 다시 시도해 주세요.", HTTPStatus.CONFLICT)
        return None

    # 첨부 파일과 표시 이름을 보관합니다.
    def photo_record(self, part):
        return part.get_payload(decode=True), part.get_filename(), part.get_content_type()

    # 채팅 초안을 저장하되 결과 버전은 생성 및 수정 경로에서만 갱신합니다.
    def save_draft(self, chat_id):
        self.ensure_chat(chat_id)
        parts = {part.get_param("name", header="content-disposition"): part for part in self.read_multipart()}
        try:
            draft = json.loads(parts["draft"].get_payload(decode=True))
            if not isinstance(draft, dict) or not isinstance(draft.get("fields"), dict):
                raise ValueError
        except (KeyError, ValueError) as error:
            raise ApiError("INVALID_INPUT", "채팅 초안을 확인해 주세요.") from error
        photo = parts.get("photo")
        if photo:
            validate_photo([photo])
        keep_photo = draft.pop("keepPhoto", False) is True and photo is None
        self.store.save(chat_id, self.browser_owner(), draft, self.photo_record(photo) if photo else None,
                        keep_photo=keep_photo)
        return self.chat_payload(self.session(chat_id))

    # 저장된 초안과 현재 이미지 버전으로 화면 조회 응답을 만듭니다.
    def chat_payload(self, row):
        draft = json.loads(row["draft"])
        result = json.loads(row["result"]) if row["result"] else None
        return {**draft, "id": row["id"], **chat_timestamps(row["updated"]),
                "apiResults": result, "hasResults": bool(result), "editCount": row["edits"],
                "photoName": row["photo_name"], "photoType": row["photo_type"]}

    # 저장된 초안을 목록용으로 요약합니다.
    def chat_summaries(self, include_policy=False):
        chats = [
            {"id": row["id"], "fields": json.loads(row["draft"]).get("fields", {}),
             **chat_timestamps(row["updated"])}
            for row in self.store.list(self.browser_owner())
        ]
        if include_policy:
            return {"chats": chats, "chatLimit": CHAT_LIMIT, "retentionSeconds": RETENTION_SECONDS}
        return chats

    # 요청 한 건의 로컬 오류 상태를 재현합니다.
    def mock_error(self):
        mode = self.headers.get("X-Onecut-Example")
        if mode == "queue":
            raise ApiError("QUEUE_FULL", "처리 중인 요청이 많습니다. 잠시 후 다시 시도해 주세요.",
                           HTTPStatus.TOO_MANY_REQUESTS, retry_after=10)
        if mode == "timeout":
            raise ApiError("GENERATION_TIMEOUT", "요청 시간이 초과됐습니다. 다시 시도해 주세요.",
                           HTTPStatus.GATEWAY_TIMEOUT)

    # 공통 지시와 개별 지시를 독립적으로 검증합니다.
    def edit_prompt(self, value, field):
        if not isinstance(value, str) or len(value.strip()) > 200:
            raise ApiError("INVALID_INPUT", "수정 프롬프트는 각각 200자까지 입력할 수 있습니다.", field=field)
        return value.strip()

    # 기존 한 장 수정 요청과 여러 장 수정 요청을 처리합니다.
    def edit(self):
        data = self.read_json()
        if "images" in data:
            return self.edit_batch(data)

        image_id = data.get("image_id")
        image = self.store.image(image_id) if isinstance(image_id, str) else None
        if not image:
            raise ApiError("INVALID_INPUT", "수정할 네일팁 이미지를 선택해 주세요.", field="image_id")
        result = self.edit_batch({
            "session_id": image["chat_id"],
            "images": [{"image_id": image_id, "image_prompt": data.get("prompt", "")}],
        })
        item = result.pop("results")[0]
        if "error" in item:
            error = item["error"]
            raise ApiError(error["code"], error["message"], status=error.get("status", HTTPStatus.BAD_REQUEST),
                           field=error.get("field"), retry_after=error.get("retry_after"))
        return {**result, "image": item["image"]}

    # 수정 대상과 이미지별 지시를 확인한 뒤 요청 순서대로 처리합니다.
    def edit_batch(self, data):
        session_id = data.get("session_id")
        session = self.session(session_id, lock=True)
        common_prompt = self.edit_prompt(data.get("common_prompt", ""), "common_prompt")
        items = data.get("images")
        if not isinstance(items, list) or not 1 <= len(items) <= 13:
            raise ApiError("INVALID_INPUT", "수정할 이미지를 1~13장 선택해 주세요.", field="images")
        targets = [self.edit_target(item, common_prompt) for item in items]
        if len({target[0] for target in targets}) != len(targets):
            raise ApiError("INVALID_INPUT", "같은 이미지를 중복 선택할 수 없습니다.", field="images")
        current = json.loads(session["result"]) if session["result"] else None
        if current is None:
            raise ApiError("INVALID_INPUT", "이미지를 생성한 뒤 수정해 주세요.")
        if session["edits"] >= 3:
            raise ApiError("EDIT_LIMIT_EXCEEDED", "채팅당 수정은 최대 3회까지 가능합니다.")
        version = session["edits"] + 1
        active_ids = {image["image_id"] for image in current["results"]}
        results = self.edit_items(session_id, targets, version, active_ids)
        if any("image" in item for item in results):
            replacements = {item["image_id"]: item["image"] for item in results if "image" in item}
            current["results"] = [replacements.get(image["image_id"], image) for image in current["results"]]
            self.store.result(session_id, json.loads(session["fields"]), current, version)
        return {"session_id": session_id, "result_id": current["result_id"], "results": results}

    # 이미지 ID와 프롬프트를 별도 값으로 유지합니다.
    def edit_target(self, item, common_prompt):
        if not isinstance(item, dict) or not isinstance(item.get("image_id"), str):
            raise ApiError("INVALID_INPUT", "수정할 이미지 ID를 입력해 주세요.", field="image_id")
        image_prompt = self.edit_prompt(item.get("image_prompt", ""), "image_prompt")
        if not common_prompt and not image_prompt:
            raise ApiError("INVALID_INPUT", "공통 또는 이미지별 수정 프롬프트를 입력해 주세요.", field="image_prompt")
        return item["image_id"], common_prompt, image_prompt

    # 대기열이 찬 경우 나머지 이미지도 사용자 재시도 대상으로 남깁니다.
    def edit_items(self, session_id, targets, version, active_ids):
        results, queue_error = [], None
        for target in targets:
            try:
                if queue_error:
                    raise queue_error
                image = self.edit_item(session_id, target, version, active_ids)
                results.append({"image_id": target[0], "image": image})
            except ApiError as error:
                results.append({"image_id": target[0], "error": error.body})
            except ModelError as error:
                if error.status == HTTPStatus.TOO_MANY_REQUESTS:
                    queue_error = error
                body = ApiError(error.code, str(error), error.status, field=error.field).body
                body.update(status=error.status, retry_after=error.retry_after)
                results.append({"image_id": target[0], "error": body})
        return results

    # 현재 결과의 이미지 파일과 수정 프롬프트를 전달합니다.
    def edit_item(self, session_id, target, version, active_ids):
        image_id, common_prompt, image_prompt = target
        image = self.store.image(image_id) if image_id in active_ids else None
        if not image or image["chat_id"] != session_id or image["kind"] not in ("nail_tip", "price_table"):
            raise ApiError("INVALID_INPUT", "이 채팅에서 수정할 수 있는 이미지를 선택해 주세요.", field="image_id")
        kind = image["kind"]
        if kind == "price_table":
            if not image_prompt:
                raise ApiError("INVALID_INPUT", "가격표에는 이미지별 수정 프롬프트를 사용해 주세요.", field="image_prompt")
            common_prompt = ""
        content = self.model.edit(image["content"], common_prompt, image_prompt) if self.model else sample_png(kind, 3 + version)
        return create_image(self.store, session_id, kind, content)

    # 게시에 선택한 이미지 묶음의 문구 초안을 반환합니다.
    def copy(self):
        data = self.read_json()
        session_id = data.get("session_id")
        session = self.session(session_id)
        image_ids = data.get("image_ids")
        if not isinstance(image_ids, list) or not 1 <= len(image_ids) <= 13:
            raise ApiError("INVALID_INPUT", "게시할 이미지를 1~13장 선택해 주세요.", field="image_ids")
        if not all(isinstance(item, str) for item in image_ids):
            raise ApiError("INVALID_INPUT", "현재 작업에 속한 이미지만 선택해 주세요.", field="image_ids")
        if len(set(image_ids)) != len(image_ids):
            raise ApiError("INVALID_INPUT", "같은 이미지를 중복 선택할 수 없습니다.", field="image_ids")
        images = self.store.selected_images(session_id, image_ids)
        if len(images) != len(image_ids):
            raise ApiError("INVALID_INPUT", "현재 작업에 속한 이미지만 선택해 주세요.", field="image_ids")
        fields = json.loads(session["fields"])
        submitted = json.loads(session["draft"]).get("submittedFields")
        if isinstance(submitted, dict):
            fields.update({name: submitted[name].strip() for name in FIELDS
                           if name != "prompt" and isinstance(submitted.get(name), str)})
            validate_fields(fields)
        if self.model:
            shop_fields = {name: fields[name] for name in FIELDS if name != "prompt"}
            return self.model.copy(shop_fields, [images[image_id] for image_id in image_ids])
        caption = (f'{fields["shop"]} 이달의 아트\n인스타그램 @{fields["instagramId"]}\n'
                   f'영업시간 {fields["businessHours"]}\n예약 {fields["booking"]}')
        return {"caption": caption, "hashtags": "#이달의아트 #네일아트"}

    # 인증한 요청마다 독립 DB 연결을 사용합니다.
    @contextmanager
    def request_store(self):
        owner = self.browser_owner()
        self.store = ChatStore(self.database_url, initialize=False)
        try:
            yield owner
        finally:
            self.store.db.close()

    # 쓰기만 계정 단위로 직렬화하여 읽기 요청은 모델 응답을 기다리지 않습니다.
    def run_api(self, action, status=HTTPStatus.OK, *, write=False):
        try:
            with self.request_store() as owner:
                transaction = self.store.db.transaction() if write else nullcontext()
                with transaction:
                    if write:
                        self.store.lock_owner(owner)
                    result = action()
            body = json.dumps(result, ensure_ascii=False).encode() if result is not None else b""
            self.reply(status, body)
        except ApiError as error:
            self.reply_error(error)
        except ModelError as error:
            self.reply_error(ApiError(error.code, str(error), error.status, field=error.field,
                                      retry_after=error.retry_after))
        except AuthError as error:
            self.reply_error(ApiError("AUTH_ERROR", str(error), error.status))
        except psycopg.OperationalError:
            self.reply_error(ApiError("DATABASE_UNAVAILABLE", "저장소에 연결할 수 없습니다. 다시 시도해 주세요.",
                                      HTTPStatus.SERVICE_UNAVAILABLE))

    # 생성, 수정, 미리보기 문구 요청을 분기합니다.
    def do_POST(self):
        routes = {"/api/ads/generate": self.generate, "/api/ads/edit": self.edit,
                  "/api/ads/preview/copy": self.copy}
        path = urlsplit(self.path).path
        auth_routes = {
            "/api/auth/signup": self.signup, "/api/auth/login": self.login,
            "/api/auth/resend": self.resend_signup_code, "/api/auth/verify": self.verify_signup_code,
            "/api/auth/logout": self.logout,
        }
        if path in auth_routes:
            return self.run_auth(auth_routes[path])
        route = routes.get(path)
        self.run_api(lambda: self.run_ad(route), write=path in ("/api/ads/generate", "/api/ads/edit"))

    # 광고 작업 검증 전에 요청 전체의 목업 오류를 적용합니다.
    def run_ad(self, route):
        if not route:
            raise ApiError("NOT_FOUND", "요청 경로를 찾을 수 없습니다.", HTTPStatus.NOT_FOUND)
        if self.model is None:
            self.mock_error()
        return route()

    # 채팅 초안 저장 요청을 처리합니다.
    def do_PUT(self):
        self.run_api(lambda: self.save_draft(self.path_chat_id()), write=True)

    # 세션 URL에서 채팅 ID를 가져옵니다.
    def path_chat_id(self):
        path = urlsplit(self.path).path
        if not path.startswith(CHAT_PREFIX):
            raise ApiError("NOT_FOUND", "요청 경로를 찾을 수 없습니다.", HTTPStatus.NOT_FOUND)
        return path.removeprefix(CHAT_PREFIX)

    # 정적 화면과 저장된 채팅 및 이미지 요청을 처리합니다.
    def do_GET(self):
        target = urlsplit(self.path)
        path = target.path
        if path == "/api/auth/session":
            return self.run_auth(self.browser_owner)
        if path == CHAT_PREFIX.rstrip("/"):
            return self.run_api(lambda: self.chat_summaries("policy" in parse_qs(target.query).get("include", [])))
        if path.startswith(CHAT_PREFIX):
            if path.endswith("/photo"):
                return self.serve_file(path)
            return self.run_api(lambda: self.chat_payload(self.session(self.path_chat_id())))
        if path.startswith("/api/images/"):
            return self.serve_file(path)
        return super().do_GET()

    # 조회한 파일만 응답하며, 만료 삭제는 예약 작업에 맡깁니다.
    def serve_file(self, path):
        try:
            with self.request_store():
                content, mime, filename = self.load_file(path)
            self.reply(HTTPStatus.OK, content, mime, filename)
        except ApiError as error:
            self.reply_error(error)
        except AuthError as error:
            self.reply_error(ApiError("AUTH_ERROR", str(error), error.status))
        except psycopg.OperationalError:
            self.reply_error(ApiError("DATABASE_UNAVAILABLE", "저장소에 연결할 수 없습니다. 다시 시도해 주세요.",
                                      HTTPStatus.SERVICE_UNAVAILABLE))

    # 소유 계정과 유효기간을 확인한 뒤 서버에 저장된 파일을 읽습니다.
    def load_file(self, path):
        if path.startswith(CHAT_PREFIX):
            row = self.session(self.path_chat_id().removesuffix("/photo"), include_photo=True)
            if row["photo"] is None:
                raise ApiError("NOT_FOUND", "첨부 사진을 찾을 수 없습니다.", HTTPStatus.NOT_FOUND)
            return row["photo"], row["photo_type"], None
        image_id = path.removeprefix("/api/images/")
        image = self.store.image(image_id)
        if not image:
            raise ApiError("NOT_FOUND", "이미지를 찾을 수 없습니다.", HTTPStatus.NOT_FOUND)
        self.session(image["chat_id"])
        stamp = datetime.now().strftime("%Y-%m-%d-%H-%M-%S")
        return image["content"], "image/png", f"{stamp}_{image_id}.png"

    # 채팅과 모든 이미지 버전을 한 트랜잭션에서 삭제합니다.
    def do_DELETE(self):
        self.run_api(self.delete_session, HTTPStatus.NO_CONTENT, write=True)

    # 삭제 전에 브라우저 소유권을 확인합니다.
    def delete_session(self):
        chat_id = self.path_chat_id()
        self.session(chat_id)
        self.store.delete(chat_id)


# 웹 개발 서버를 로컬 인터페이스에서 실행합니다.
def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8507)
    args = parser.parse_args()
    database_url = os.environ.get("SUPABASE_DB_URL")
    if not database_url:
        parser.exit(2, "SUPABASE_DB_URL 환경 변수를 설정해 주세요.\n")
    model_url = os.environ.get("AI_BASE_URL", "").strip()
    if model_url:
        model_address = urlsplit(model_url)
        if model_address.scheme not in ("http", "https") or not model_address.hostname:
            parser.exit(2, "AI_BASE_URL에 모델 서버의 HTTP(S) 주소를 입력해 주세요.\n")
        WebHandler.model = ModelAPI(model_url)
    try:
        ChatStore(database_url).db.close()
        WebHandler.database_url = database_url
    except psycopg.Error:
        parser.exit(1, "Postgres 접속 정보와 스키마 생성 권한을 확인해 주세요.\n")
    stop = Event()
    worker = Thread(target=run_retention, args=(database_url, stop), name="chat-retention", daemon=True)
    try:
        with ThreadingHTTPServer(("127.0.0.1", args.port), WebHandler) as server:
            worker.start()
            server.serve_forever()
    finally:
        stop.set()


if __name__ == "__main__":
    main()
