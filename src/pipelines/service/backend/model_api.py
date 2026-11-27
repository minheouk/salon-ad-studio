"""웹 백엔드에서 모델 서버의 파일 기반 생성 API를 호출합니다."""

import json
from http import HTTPStatus
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import uuid4

CALL_TIMEOUT = 70
MAX_JSON_BYTES = 1024 * 1024
MAX_PNG_BYTES = 64 * 1024 * 1024
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class ModelError(Exception):
    """모델 서버 오류를 웹 응답으로 전달하는 데 필요한 정보를 보관합니다."""

    # 모델 서버 오류에 웹 API로 전달할 코드와 대기 시간을 보관합니다.
    def __init__(self, code, message, status=HTTPStatus.BAD_GATEWAY, retry_after=None, *, field=None):
        super().__init__(message)
        self.code = code
        self.status = status
        self.retry_after = retry_after
        self.field = field


class NoRedirect(HTTPRedirectHandler):
    """모델 요청이 다른 서버에 파일을 전송하지 않도록 합니다."""

    # 리디렉션 요청을 거부해 모델 호출이 다른 주소로 넘어가지 않게 합니다.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


# 파일 필드는 입력 순서대로 반복하고, 값이 없는 선택 필드는 호출부에서 생략합니다.
def multipart(fields, files):
    boundary = uuid4().hex
    prefix = f"--{boundary}\r\n".encode()
    parts = []
    for name, value in fields.items():
        parts.extend((prefix, f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
                      value.encode("utf-8"), b"\r\n"))
    for name, filename, mime, content in files:
        header = f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
        parts.extend((prefix, header.encode(), f"Content-Type: {mime}\r\n\r\n".encode(), content, b"\r\n"))
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


# 잘못된 응답으로 채팅 결과를 덮어쓰지 않습니다.
def invalid_response():
    return ModelError("INVALID_MODEL_RESPONSE", "모델 서버 응답을 읽지 못했습니다. 다시 시도해 주세요.")


# 모델의 공개 오류 코드와 Retry-After를 웹 API로 전달합니다.
def response_error(error):
    try:
        data = json.loads(error.read(MAX_JSON_BYTES))
        data = data.get("error") if isinstance(data, dict) else None
    except (ValueError, OSError):
        data = {}
    if error.code == 504:
        return ModelError("GENERATION_TIMEOUT", "요청 시간이 초과됐습니다. 다시 시도해 주세요.", 504)
    if not 400 <= error.code <= 599:
        return invalid_response()
    data = data if isinstance(data, dict) else {}
    message = data.get("message")
    field = data.get("field")
    code = data.get("code")
    return ModelError(
        code if isinstance(code, str) else "MODEL_REQUEST_FAILED",
        message if isinstance(message, str) else "모델 요청에 실패했습니다. 다시 시도해 주세요.",
        error.code,
        error.headers.get("Retry-After") if error.code == 429 else None,
        field=field if isinstance(field, str) and field else None,
    )


# JSON 응답의 형식을 검증합니다.
def decode_json(content):
    try:
        data = json.loads(content)
    except ValueError as error:
        raise invalid_response() from error
    if not isinstance(data, dict):
        raise invalid_response()
    return data


# 파일을 저장하기 전에 PNG 형식인지 확인합니다.
def png(content):
    if not content.startswith(PNG_SIGNATURE):
        raise invalid_response()
    return content


class ModelAPI:
    """서빙과 통신하고 서명 URL의 결과는 즉시 내려받습니다."""

    # 모델 서버의 기준 주소와 리디렉션을 제한한 HTTP 클라이언트를 설정합니다.
    def __init__(self, base_url):
        self.base_url = base_url.rstrip("/") + "/"
        self.origin = urlsplit(self.base_url)
        self.opener = build_opener(NoRedirect())

    # 호출은 한 번만 실행하고, 응답 크기를 제한합니다.
    def fetch(self, request):
        try:
            with self.opener.open(request, timeout=CALL_TIMEOUT) as response:
                content_type = response.headers.get_content_type()
                limit = MAX_PNG_BYTES if content_type == "image/png" else MAX_JSON_BYTES
                content = response.read(limit + 1)
        except HTTPError as error:
            with error:
                raise response_error(error) from None
        except (URLError, OSError) as error:
            if isinstance(error, TimeoutError) or isinstance(getattr(error, "reason", None), TimeoutError):
                raise ModelError("GENERATION_TIMEOUT", "요청 시간이 초과됐습니다. 다시 시도해 주세요.", 504) from error
            raise ModelError("MODEL_UNAVAILABLE", "모델 서버에 연결하지 못했습니다. 다시 시도해 주세요.") from error
        if len(content) > limit:
            raise invalid_response()
        return content_type, content

    # 공통 multipart 형식으로 모델 서버에 요청을 전달합니다.
    def post(self, endpoint, fields, files):
        body, content_type = multipart(fields, files)
        return self.fetch(Request(urljoin(self.base_url, endpoint), data=body,
                                  headers={"Content-Type": content_type}))

    # 다운로드는 모델 서버와 같은 출처의 서명 URL로 제한합니다.
    def download(self, item):
        if not isinstance(item, dict) or not isinstance(item.get("url"), str) or not item["url"]:
            raise invalid_response()
        try:
            url = urljoin(self.base_url, item["url"])
            target = urlsplit(url)
        except ValueError as error:
            raise invalid_response() from error
        if (target.scheme, target.netloc) != (self.origin.scheme, self.origin.netloc):
            raise invalid_response()
        return png(self.fetch(Request(url))[1])

    # 가격표와 이미지 순서를 검증한 후 서버 보관용 파일을 넘깁니다.
    def generate(self, fields, photo, mime):
        extension = "png" if mime == "image/png" else "jpg"
        _, content = self.post("generate", fields, [("images", f"input.{extension}", mime, photo)])
        items = decode_json(content).get("results")
        if not isinstance(items, list) or not 2 <= len(items) <= 13:
            raise invalid_response()
        kinds = ["price_table"] + ["nail_tip"] * (len(items) - 1)
        if not all(isinstance(item, dict) and item.get("type") == kind
                   for item, kind in zip(items, kinds)):
            raise invalid_response()
        return [(kind, self.download(item)) for kind, item in zip(kinds, items)]

    # 단건 수정 응답은 PNG 또는 하나의 서명 URL을 사용하는 임시 형식으로 받습니다.
    def edit(self, image, common_prompt, image_prompt):
        fields = {name: value for name, value in
                  (("commonPrompt", common_prompt), ("imagePrompt", image_prompt)) if value}
        mime, content = self.post("edit", fields, [("image", "edit.png", "image/png", image)])
        return png(content) if mime == "image/png" else self.download(decode_json(content))

    # 선택된 이미지 순서로 하나의 게시물 문구를 요청합니다.
    def copy(self, fields, images):
        files = [("images", f"image-{index}.png", "image/png", content)
                 for index, content in enumerate(images, 1)]
        _, content = self.post("copy", fields, files)
        data = decode_json(content)
        if not all(isinstance(data.get(key), str) for key in ("caption", "hashtags")):
            raise invalid_response()
        return {"caption": data["caption"], "hashtags": data["hashtags"]}
