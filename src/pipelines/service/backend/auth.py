"""Supabase 이메일 OTP 및 사용자 세션 요청을 처리합니다."""

import json
import os
from http import HTTPStatus
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class AuthError(Exception):
    """클라이언트에 표시할 인증 오류입니다."""

    # 인증 오류에 HTTP 상태와 사용자 안내 문구를 보관합니다.
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


# 인증 키를 서버에서만 사용하고 공급자의 오류 본문은 노출하지 않습니다.
def supabase_auth(path, payload=None, access_token=None):
    base_url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    api_key = os.environ.get("SUPABASE_PUBLISHABLE_KEY") or os.environ.get("SUPABASE_ANON_KEY")
    if not base_url.startswith("https://") or not api_key:
        raise AuthError(HTTPStatus.SERVICE_UNAVAILABLE, "이메일 인증 설정을 확인해 주세요.")

    headers = {"apikey": api_key, "Content-Type": "application/json"}
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"
    body = json.dumps(payload).encode() if payload is not None else None
    request = Request(f"{base_url}/auth/v1/{path}", data=body, headers=headers)
    try:
        with urlopen(request, timeout=8) as response:
            content = response.read()
            return json.loads(content) if content else {}
    except HTTPError as error:
        with error:
            status = error.code
        raise provider_error(path, status) from None
    except (URLError, TimeoutError, ValueError):
        raise AuthError(HTTPStatus.BAD_GATEWAY, "이메일 인증 서비스에 연결하지 못했습니다.") from None


# 공급자 상태를 기존 웹 인증 오류 형식으로 변환합니다.
def provider_error(path, status):
    if status == HTTPStatus.TOO_MANY_REQUESTS:
        return AuthError(status, "잠시 후 인증을 다시 시도해 주세요.")
    if path in ("signup", "resend") and status in (400, 403, 422):
        return AuthError(status, "인증 코드를 보낼 수 없습니다. 이메일과 가입 여부를 확인해 주세요.")
    if status in (400, 401, 403, 422):
        message = ("이메일, 비밀번호와 가입 인증 여부를 확인해 주세요." if path == "token?grant_type=password"
                   else "인증 코드 또는 세션이 만료되었는지 확인해 주세요.")
        return AuthError(HTTPStatus.UNAUTHORIZED, message)
    return AuthError(HTTPStatus.BAD_GATEWAY, "이메일 인증 서비스에 연결하지 못했습니다.")
