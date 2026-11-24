"""서빙 목업 API 서버.

진짜 모델(GPT Image 2, GPT6 LUNA) 대신 정해진 형식의 가짜 결과를 돌려준다.
모델이 완성되기 전에도 서비스 개발이 화면을 만들 수 있게 하는 것이 목적이다.
받은 사진과 요청 내용은 메모리에서만 쓰고 디스크에 저장하지 않는다.

실행 (저장소 루트에서): python -m src.pipelines.serving.mock.server --port 8004
"""

import argparse
import hashlib
import json
import re
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from src.pipelines.serving.mock import jobs
from src.pipelines.serving.mock.signed_url import check_signed_url, signed_url
from src.pipelines.serving.mock.validation import (
    MAX_FILE_BYTES,
    MAX_PHOTOS,
    ApiError,
    check_photo,
    check_photos,
    check_prompt,
)

# 외부에 포트를 열지 않는다. VM에서는 SSH 포트 전달로만 접속한다.
HOST = "127.0.0.1"
# base64는 원본보다 약 4/3배 커지므로, 사진 최대 장수 × 최대 크기를 base64로 감싼 크기에 여유 1MB를 더한다.
MAX_BODY_BYTES = MAX_PHOTOS * MAX_FILE_BYTES * 4 // 3 + 1024 * 1024
QUEUE_FULL_RETRY_AFTER_SECONDS = 10
JOB_PATH = re.compile(r"/api/jobs/([0-9a-f]{32})(?:/(cancel|retry))?")
RESULT_PATH = re.compile(r"/api/results/([0-9a-f]{32})/([0-9]+)")


def validate_generate(body: dict) -> None:
    check_photos(body.get("photos"))
    check_prompt(body.get("prompt"))


def validate_edit(body: dict) -> None:
    check_photo(body.get("image"), 1)
    check_prompt(body.get("prompt"))


# 프롬프트에 이 표시가 있으면 목업 처리가 그 상태로 끝난다. 화면의 실패·시간 초과 화면을 시험하는 용도.
MOCK_OUTCOME_MARKERS = {"[mock:failed]": jobs.FAILED, "[mock:timeout]": jobs.TIMEOUT}


def mock_outcome(prompt: str) -> str:
    for marker, status in MOCK_OUTCOME_MARKERS.items():
        if marker in prompt:
            return status
    return jobs.SUCCEEDED


# 주소 -> (작업 종류, 입력 검증, 결과 수)
CREATE_ROUTES = {
    "/api/jobs/generate": ("generate", validate_generate, 5),
    "/api/jobs/edit": ("edit", validate_edit, 1),
}


def ms_between(start: float | None, end: float | None) -> int | None:
    if start is None:
        return None
    return round(((end or time.monotonic()) - start) * 1000)


def job_view(job: jobs.Job) -> dict:
    view = {
        "job_id": job.id,
        "type": job.kind,
        "status": job.status,
        "retries": job.retries,
        "timing_ms": {
            "wait": ms_between(job.submitted_at, job.started_at or job.finished_at),
            "run": ms_between(job.started_at, job.finished_at),
            "total": ms_between(job.submitted_at, job.finished_at),
        },
    }
    if job.error:
        view["error"] = job.error
    if job.status == jobs.SUCCEEDED:
        view["results"] = [
            {"url": signed_url(f"/api/results/{job.id}/{number}"), "copy": result["copy"]}
            for number, result in enumerate(job.results, start=1)
        ]
        view["results_expire_in_seconds"] = round(
            job.finished_wall_time + jobs.RESULT_TTL_SECONDS - time.time()
        )
    return view


class MockHandler(BaseHTTPRequestHandler):
    store: jobs.JobStore

    def do_POST(self) -> None:
        try:
            self.route_post(urlsplit(self.path).path)
        except ApiError as error:
            self.send_error_json(error)

    def do_GET(self) -> None:
        try:
            self.route_get(urlsplit(self.path))
        except ApiError as error:
            self.send_error_json(error)

    def route_post(self, path: str) -> None:
        if path in CREATE_ROUTES:
            self.create_job(*CREATE_ROUTES[path])
            return
        match = JOB_PATH.fullmatch(path)
        if not match or not match[2]:
            raise ApiError("NOT_FOUND", "없는 주소입니다.", 404)
        action = self.store.cancel if match[2] == "cancel" else self.store.retry
        self.send_json(HTTPStatus.ACCEPTED, job_view(action(match[1])))

    def route_get(self, url) -> None:
        job_match = JOB_PATH.fullmatch(url.path)
        if job_match and not job_match[2]:
            self.send_json(HTTPStatus.OK, job_view(self.store.get(job_match[1])))
            return
        result_match = RESULT_PATH.fullmatch(url.path)
        if not result_match:
            raise ApiError("NOT_FOUND", "없는 주소입니다.", 404)
        check_signed_url(url.path, parse_qs(url.query))
        self.send_png(self.result_png(result_match[1], int(result_match[2])))

    def create_job(self, kind: str, validate, result_count: int) -> None:
        key = self.headers.get("Idempotency-Key")
        if not key:
            raise ApiError("MISSING_IDEMPOTENCY_KEY", "Idempotency-Key 헤더가 필요합니다.")
        body = self.read_json()
        validate(body)
        fingerprint = hashlib.sha256(kind.encode() + json.dumps(body, sort_keys=True).encode()).hexdigest()
        job, replayed = self.store.submit(key, fingerprint, kind, result_count, mock_outcome(body["prompt"]))
        self.send_json(HTTPStatus.OK if replayed else HTTPStatus.ACCEPTED, job_view(job))

    def result_png(self, job_id: str, number: int) -> bytes:
        job = self.store.get(job_id)
        if job.status != jobs.SUCCEEDED or not 1 <= number <= len(job.results):
            raise ApiError("RESULT_NOT_FOUND", "결과를 찾을 수 없습니다.", 404)
        return job.results[number - 1]["png"]

    def read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY_BYTES:
            # 본문을 읽지 않고 거절하므로 연결을 닫아 남은 데이터가 다음 요청으로 섞이지 않게 한다.
            self.close_connection = True
            raise ApiError("BODY_TOO_LARGE", "요청 크기가 너무 큽니다.", 413)
        if self.headers.get_content_type() != "application/json":
            raise ApiError("INVALID_JSON", "JSON 형식으로 보내 주세요.", 415)
        try:
            body = json.loads(self.rfile.read(length))
        except ValueError:
            body = None
        if not isinstance(body, dict):
            raise ApiError("INVALID_JSON", "요청 형식이 올바르지 않습니다.")
        return body

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
    MockHandler.store = jobs.JobStore()
    server = ThreadingHTTPServer((HOST, port), MockHandler)
    print(f"목업 API: http://{HOST}:{port}  (종료: Ctrl+C)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()


if __name__ == "__main__":
    main()
