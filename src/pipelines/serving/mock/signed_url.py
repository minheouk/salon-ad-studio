"""결과 다운로드용 서명 URL.

주소에 만료 시각과 서명을 붙여, 서버가 발급한 주소만 정해진 시간 안에 쓸 수 있게 한다.
서명 키는 서버를 켤 때마다 새로 만들고 저장하지 않는다. 서버를 다시 켜면 이전 주소는 모두 무효가 된다.
"""

import hashlib
import hmac
import secrets
import time

from src.pipelines.serving.mock.validation import ApiError

URL_TTL_SECONDS = 60 * 60
SECRET = secrets.token_bytes(32)


def sign(path: str, expires: int) -> str:
    return hmac.new(SECRET, f"{path}?{expires}".encode(), hashlib.sha256).hexdigest()


def signed_url(path: str) -> str:
    expires = int(time.time()) + URL_TTL_SECONDS
    return f"{path}?expires={expires}&signature={sign(path, expires)}"


def check_signed_url(path: str, query: dict[str, list[str]]) -> None:
    expires = query.get("expires", [""])[0]
    signature = query.get("signature", [""])[0]
    if not expires.isdigit() or not hmac.compare_digest(signature, sign(path, int(expires))):
        raise ApiError("INVALID_SIGNATURE", "올바르지 않은 다운로드 주소입니다.", 403)
    if int(expires) < time.time():
        raise ApiError(
            "URL_EXPIRED", "다운로드 주소가 만료되었습니다. 작업 상태를 다시 조회해 새 주소를 받아 주세요.", 403
        )
