"""서빙 목업 API 서버.

진짜 모델(GPT Image 2, GPT6 LUNA) 대신 정해진 형식의 가짜 결과를 돌려준다.
모델이 완성되기 전에도 서비스 개발이 화면을 만들 수 있게 하는 것이 목적이다.
받은 사진과 요청 내용은 메모리에서만 쓰고 디스크에 저장하지 않는다.

실행 (저장소 루트에서): python -m src.pipelines.serving.mock.server --port 8004
"""

import argparse
import json
import re
import time
from email import policy
from email.parser import BytesParser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from src.pipelines.serving.mock import generation
from src.pipelines.serving.mock.signed_url import check_signed_url, signed_url
from src.pipelines.serving.mock.validation import (
    MAX_COPY_IMAGES,
    MAX_FILE_BYTES,
    MAX_PHOTOS,
    ApiError,
    check_copy_images,
    check_edit_instructions,
    check_edit_prompts,
    check_photos,
    check_prompt,
)

# 외부에 포트를 열지 않는다. VM에서는 SSH 포트 전달로만 접속한다.
HOST = "127.0.0.1"
# 주소 -> 생성 종류.
# /generate: 네일 팁 세트 사진 1장으로 이미지를 만든다.
# /edit: 결과 수정 단계. 이미지 1장을 받아 수정본 1장을 만든다. 문구는 만들지 않는다.
# /copy: 이미지를 다 고른 뒤 게시 미리보기에서 따로 부른다. 최종 이미지 여러 장으로 문구를 만든다.
GENERATE_ROUTES = {"/generate": generation.IMAGES, "/edit": generation.EDIT, "/copy": generation.COPY}
# 파일로 받는 요청 필드. /generate·/copy는 images, /edit은 수정할 이미지 1장을 image로 받는다.
FILE_FIELDS = ("images", "image")
# 웹 백엔드가 채팅에 저장해 둔 매장 정보. /copy는 이 값을 게시글 본문에 넣는다.
STORE_FIELDS = ("shop", "instagramId", "businessHours", "booking")
# 이미지 최대 장수 × 최대 크기에, 텍스트 필드와 multipart 경계 문자열 몫으로 1MB를 더한다.
# /copy 한도는 장수·용량이 정해지지 않아 임시 값이다.
EXTRA_BODY_BYTES = 1024 * 1024
MAX_BODY_BYTES = {
    generation.IMAGES: MAX_PHOTOS * MAX_FILE_BYTES + EXTRA_BODY_BYTES,
    generation.EDIT: MAX_PHOTOS * MAX_FILE_BYTES + EXTRA_BODY_BYTES,
    generation.COPY: MAX_COPY_IMAGES * MAX_FILE_BYTES + EXTRA_BODY_BYTES,
}
QUEUE_FULL_RETRY_AFTER_SECONDS = 10
RESULT_PATH = re.compile(r"/api/results/([0-9a-f]{32})/([0-9]+)")

# 프롬프트에 이 표시가 있으면 목업 처리가 그 상태로 끝난다. 웹의 실패·시간 초과 화면을 시험하는 용도.
MOCK_OUTCOME_MARKERS = {"[mock:failed]": generation.FAILED, "[mock:timeout]": generation.TIMEOUT}


def mock_outcome(prompt: str) -> str:
    for marker, outcome in MOCK_OUTCOME_MARKERS.items():
        if marker in prompt:
            return outcome
    return generation.SUCCEEDED


def parse_form(content_type: str, body: bytes) -> tuple[dict[str, list[str]], dict[str, list[bytes]]]:
    """multipart/form-data 본문을 텍스트 필드와 파일 필드(images, image)로 나눈다.

    같은 이름이 여러 번 올 수 있어서(editPrompts, images) 값은 목록으로 모은다. 파일은 보낸 순서를 지킨다.
    """
    message = BytesParser(policy=policy.HTTP).parsebytes(
        f"Content-Type: {content_type}\r\n\r\n".encode() + body
    )
    fields: dict[str, list[str]] = {}
    files: dict[str, list[bytes]] = {}
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        data = part.get_payload(decode=True) or b""
        if name in FILE_FIELDS:
            files.setdefault(name, []).append(data)
        elif name:
            fields.setdefault(name, []).append(data.decode("utf-8", errors="replace"))
    return fields, files


def last_value(fields: dict[str, list[str]], name: str) -> str | None:
    values = fields.get(name)
    return values[-1] if values else None


def check_generate_input(fields: dict[str, list[str]], files: dict[str, list[bytes]]) -> str:
    check_photos(files.get("images", []))
    return check_prompt(last_value(fields, "prompt"))


def check_edit_input(fields: dict[str, list[str]], files: dict[str, list[bytes]]) -> str:
    # 수정할 이미지 1장(image)과, 공통 프롬프트(commonPrompt)·개별 프롬프트(imagePrompt) 중 하나 이상을 받는다.
    check_photos(files.get("image", []), field="image")
    return check_edit_instructions(last_value(fields, "commonPrompt"), last_value(fields, "imagePrompt"))


def check_copy_input(fields: dict[str, list[str]], files: dict[str, list[bytes]]) -> str:
    # 게시할 최종 이미지 여러 장과, 최초 프롬프트에 더해 수정 단계의 수정 프롬프트도 받는다.
    check_copy_images(files.get("images", []))
    check_edit_prompts(fields.get("editPrompts", []))
    return check_prompt(last_value(fields, "prompt"))


# 생성 종류 -> 입력 검사. 검사를 통과하면 목업 표시([mock:...])를 찾을 프롬프트를 돌려준다.
INPUT_CHECKS = {
    generation.IMAGES: check_generate_input,
    generation.EDIT: check_edit_input,
    generation.COPY: check_copy_input,
}


def ms_between(start: float, end: float) -> int:
    return round((end - start) * 1000)


def generation_view(done: generation.Generation) -> dict:
    timing = {
        "wait": ms_between(done.received_at, done.started_at),
        "run": ms_between(done.started_at, done.finished_at),
        "total": ms_between(done.received_at, done.finished_at),
    }
    if done.copy is not None:
        return {"timing_ms": timing, **done.copy}
    expires_in = round(done.finished_wall_time + generation.RESULT_TTL_SECONDS - time.time())
    if done.kind == generation.EDIT:
        # 수정본은 1장이라 목록 대신 result 하나로 준다. 종류(type)는 보낸 쪽이 알고 있어서 넣지 않는다.
        return {
            "generation_id": done.id,
            "timing_ms": timing,
            "result": {"url": signed_url(f"/api/results/{done.id}/1")},
            "results_expire_in_seconds": expires_in,
        }
    return {
        "generation_id": done.id,
        "timing_ms": timing,
        "results": [
            {"type": result["type"], "url": signed_url(f"/api/results/{done.id}/{number}")}
            for number, result in enumerate(done.results, start=1)
        ],
        "results_expire_in_seconds": expires_in,
    }


class MockHandler(BaseHTTPRequestHandler):
    generator: generation.Generator

    def do_POST(self) -> None:
        try:
            kind = GENERATE_ROUTES.get(urlsplit(self.path).path)
            if kind is None:
                raise ApiError("NOT_FOUND", "없는 주소입니다.", 404)
            self.send_json(HTTPStatus.OK, generation_view(self.generate(kind)))
        except ApiError as error:
            self.send_error_json(error)

    def do_GET(self) -> None:
        try:
            url = urlsplit(self.path)
            match = RESULT_PATH.fullmatch(url.path)
            if not match:
                raise ApiError("NOT_FOUND", "없는 주소입니다.", 404)
            check_signed_url(url.path, parse_qs(url.query))
            self.send_png(self.generator.result_png(match[1], int(match[2])))
        except ApiError as error:
            self.send_error_json(error)

    def generate(self, kind: str) -> generation.Generation:
        fields, files = self.read_form(MAX_BODY_BYTES[kind])
        # 처리 시간은 요청 본문을 다 받은 뒤부터 잰다. 업로드 시간은 넣지 않는다.
        received_at = time.monotonic()
        prompt = INPUT_CHECKS[kind](fields, files)
        # 매장 정보 형식 검사는 웹 백엔드가 한다. 목업은 받은 값을 그대로 쓴다.
        store = {name: last_value(fields, name) or "" for name in STORE_FIELDS}
        return self.generator.generate(kind, mock_outcome(prompt), received_at, store)

    def read_form(self, max_body_bytes: int) -> tuple[dict[str, list[str]], dict[str, list[bytes]]]:
        length = int(self.headers.get("Content-Length") or 0)
        if length > max_body_bytes:
            # 본문을 읽지 않고 거절하므로 연결을 닫아 남은 데이터가 다음 요청으로 섞이지 않게 한다.
            self.close_connection = True
            raise ApiError("BODY_TOO_LARGE", "요청 크기가 너무 큽니다.", 413)
        content_type = self.headers.get("Content-Type", "")
        if self.headers.get_content_type() != "multipart/form-data":
            raise ApiError("INVALID_FORM", "multipart/form-data 형식으로 보내 주세요.", 415)
        return parse_form(content_type, self.rfile.read(length))

    def send_error_json(self, error: ApiError) -> None:
        headers = {}
        if error.status == HTTPStatus.TOO_MANY_REQUESTS:
            headers["Retry-After"] = str(QUEUE_FULL_RETRY_AFTER_SECONDS)
        payload = {"error": {"code": error.code, "field": error.field, "message": error.message}}
        self.send_json(error.status, payload, headers)

    def send_json(self, status: int, payload: dict, headers: dict | None = None) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_body(status, "application/json; charset=utf-8", body, headers or {})

    def send_png(self, png: bytes) -> None:
        self.send_body(HTTPStatus.OK, "image/png", png, {})

    def send_body(self, status: int, content_type: str, body: bytes, headers: dict) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for name, value in headers.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    parser = argparse.ArgumentParser(description="서빙 목업 API 서버")
    parser.add_argument("--port", type=int, default=8004)
    port = parser.parse_args().port
    MockHandler.generator = generation.Generator()
    server = ThreadingHTTPServer((HOST, port), MockHandler)
    print(f"목업 API: http://{HOST}:{port}  (종료: Ctrl+C)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()


if __name__ == "__main__":
    main()
