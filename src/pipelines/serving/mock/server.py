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
    MAX_FILE_BYTES,
    MAX_PHOTOS,
    ApiError,
    check_photos,
    check_prompt,
)

# 외부에 포트를 열지 않는다. VM에서는 SSH 포트 전달로만 접속한다.
HOST = "127.0.0.1"
GENERATE_PATH = "/generate"
# 사진 최대 장수 × 최대 크기에, 텍스트 필드와 multipart 경계 문자열 몫으로 1MB를 더한다.
MAX_BODY_BYTES = MAX_PHOTOS * MAX_FILE_BYTES + 1024 * 1024
QUEUE_FULL_RETRY_AFTER_SECONDS = 10
RESULT_PATH = re.compile(r"/api/results/([0-9a-f]{32})/([0-9]+)")

# 프롬프트에 이 표시가 있으면 목업 처리가 그 상태로 끝난다. 웹의 실패·시간 초과 화면을 시험하는 용도.
MOCK_OUTCOME_MARKERS = {"[mock:failed]": generation.FAILED, "[mock:timeout]": generation.TIMEOUT}


def mock_outcome(prompt: str) -> str:
    for marker, outcome in MOCK_OUTCOME_MARKERS.items():
        if marker in prompt:
            return outcome
    return generation.SUCCEEDED


def parse_form(content_type: str, body: bytes) -> tuple[dict[str, str], list[bytes]]:
    """multipart/form-data 본문을 텍스트 필드와 images 파일 목록으로 나눈다."""
    message = BytesParser(policy=policy.HTTP).parsebytes(
        f"Content-Type: {content_type}\r\n\r\n".encode() + body
    )
    fields: dict[str, str] = {}
    images: list[bytes] = []
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        data = part.get_payload(decode=True) or b""
        if name == "images":
            images.append(data)
        elif name:
            fields[name] = data.decode("utf-8", errors="replace")
    return fields, images


def ms_between(start: float, end: float) -> int:
    return round((end - start) * 1000)


def generation_view(done: generation.Generation) -> dict:
    return {
        "generation_id": done.id,
        "timing_ms": {
            "wait": ms_between(done.received_at, done.started_at),
            "run": ms_between(done.started_at, done.finished_at),
            "total": ms_between(done.received_at, done.finished_at),
        },
        "results": [
            {"url": signed_url(f"/api/results/{done.id}/{number}"), "copy": result["copy"]}
            for number, result in enumerate(done.results, start=1)
        ],
        "results_expire_in_seconds": round(
            done.finished_wall_time + generation.RESULT_TTL_SECONDS - time.time()
        ),
    }


class MockHandler(BaseHTTPRequestHandler):
    generator: generation.Generator

    def do_POST(self) -> None:
        try:
            if urlsplit(self.path).path != GENERATE_PATH:
                raise ApiError("NOT_FOUND", "없는 주소입니다.", 404)
            self.send_json(HTTPStatus.OK, generation_view(self.generate()))
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

    def generate(self) -> generation.Generation:
        fields, images = self.read_form()
        # 처리 시간은 요청 본문을 다 받은 뒤부터 잰다. 업로드 시간은 넣지 않는다.
        received_at = time.monotonic()
        check_photos(images)
        prompt = check_prompt(fields.get("prompt"))
        return self.generator.generate(mock_outcome(prompt), received_at)

    def read_form(self) -> tuple[dict[str, str], list[bytes]]:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY_BYTES:
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
        self.send_json(error.status, {"code": error.code, "message": error.message}, headers)

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
