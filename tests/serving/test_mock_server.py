"""목업 API 서버를 실제로 띄워서 작업 흐름(등록 → 상태 조회 → 결과 다운로드)을 확인하는 테스트."""

import json
import threading
import time
import urllib.error
import urllib.request
import uuid
from http.server import ThreadingHTTPServer

import pytest

from src.pipelines.serving.mock import jobs, signed_url
from src.pipelines.serving.mock.server import MockHandler
from tests.serving.test_mock_validation import make_png, to_data_url

PHOTO = to_data_url(make_png(1024, 1024))


@pytest.fixture(scope="module")
def server_url():
    server = ThreadingHTTPServer(("127.0.0.1", 0), MockHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


@pytest.fixture
def api(server_url, monkeypatch):
    """테스트마다 빈 작업 저장소를 쓰고, 목업 대기 시간은 0초로 줄인다."""
    monkeypatch.setattr(jobs, "MOCK_DELAY_SECONDS", 0)
    MockHandler.store = jobs.JobStore()
    return server_url


def request(url: str, body: dict | None = None, headers: dict | None = None):
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req) as response:
            return response.status, response.headers, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.headers, error.read()


def call(url: str, body: dict | None = None, headers: dict | None = None) -> tuple[int, dict]:
    status, _, raw = request(url, body, headers)
    return status, json.loads(raw)


def create(api: str, body: dict | None = None, key: str | None = None) -> tuple[int, dict]:
    body = body or {"photos": [PHOTO], "prompt": "봄 시즌 펌 할인 광고"}
    return call(f"{api}/api/jobs/generate", body, {"Idempotency-Key": key or uuid.uuid4().hex})


def wait_until_finished(api: str, job_id: str) -> dict:
    for _ in range(100):
        _, job = call(f"{api}/api/jobs/{job_id}")
        if job["status"] != jobs.PROCESSING:
            return job
        time.sleep(0.05)
    raise AssertionError("작업이 끝나지 않았습니다.")


def test_generate_job_succeeds_with_five_downloadable_results(api):
    status, job = create(api)
    assert (status, job["status"]) == (202, "processing")

    finished = wait_until_finished(api, job["job_id"])
    assert finished["status"] == "succeeded"
    assert len(finished["results"]) == 5
    assert set(finished["timing_ms"]) == {"wait", "run", "total"}

    status, headers, png = request(api + finished["results"][0]["url"])
    assert (status, headers["Content-Type"]) == (200, "image/png")
    assert png.startswith(b"\x89PNG")


def test_edit_job_returns_one_result(api):
    _, job = call(
        f"{api}/api/jobs/edit",
        {"image": PHOTO, "prompt": "배경을 밝은 파란색으로 바꿔 주세요"},
        {"Idempotency-Key": uuid.uuid4().hex},
    )
    assert len(wait_until_finished(api, job["job_id"])["results"]) == 1


def test_validation_error_is_returned_before_a_job_is_made(api):
    status, error = create(api, {"photos": [PHOTO], "prompt": ""})
    assert (status, error) == (400, {"code": "EMPTY_PROMPT", "message": "요청 내용을 입력해 주세요."})


def test_idempotency_key_is_required(api):
    status, error = call(f"{api}/api/jobs/generate", {"photos": [PHOTO], "prompt": "광고"})
    assert (status, error["code"]) == (400, "MISSING_IDEMPOTENCY_KEY")


def test_same_idempotency_key_returns_the_first_job(api):
    _, first = create(api, key="same-key")
    status, again = create(api, key="same-key")
    assert (status, again["job_id"]) == (200, first["job_id"])


def test_same_key_with_different_body_is_rejected(api):
    create(api, key="same-key")
    status, error = create(api, {"photos": [PHOTO], "prompt": "다른 내용"}, key="same-key")
    assert (status, error["code"]) == (409, "IDEMPOTENCY_CONFLICT")


def test_new_request_is_rejected_when_queue_is_full(api, monkeypatch):
    monkeypatch.setattr(jobs, "MOCK_DELAY_SECONDS", 5)
    for _ in range(jobs.MAX_PENDING_JOBS):
        assert create(api)[0] == 202

    status, headers, raw = request(
        f"{api}/api/jobs/generate",
        {"photos": [PHOTO], "prompt": "광고"},
        {"Idempotency-Key": uuid.uuid4().hex},
    )
    assert (status, json.loads(raw)["code"], headers["Retry-After"]) == (429, "QUEUE_FULL", "10")


def test_processing_job_can_be_canceled(api, monkeypatch):
    monkeypatch.setattr(jobs, "MOCK_DELAY_SECONDS", 5)
    _, job = create(api)

    status, canceled = call(f"{api}/api/jobs/{job['job_id']}/cancel", {})
    assert (status, canceled["status"]) == (202, "canceled")
    status, error = call(f"{api}/api/jobs/{job['job_id']}/cancel", {})
    assert (status, error["code"]) == (409, "JOB_FINISHED")


def test_timeout_can_be_retried_up_to_five_times(api, monkeypatch):
    monkeypatch.setattr(jobs, "MOCK_DELAY_SECONDS", 1)
    monkeypatch.setattr(jobs, "SERVER_TIMEOUT_SECONDS", 0.05)
    _, job = create(api)
    job_id = job["job_id"]
    assert wait_until_finished(api, job_id)["error"]["code"] == "TIMEOUT"

    for attempt in range(1, jobs.MAX_RETRIES + 1):
        status, retried = call(f"{api}/api/jobs/{job_id}/retry", {})
        assert (status, retried["status"], retried["retries"]) == (202, "processing", attempt)
        assert wait_until_finished(api, job_id)["status"] == "timeout"

    status, error = call(f"{api}/api/jobs/{job_id}/retry", {})
    assert (status, error["code"]) == (409, "RETRY_LIMIT")


@pytest.mark.parametrize(
    ("prompt", "status", "code"),
    [
        ("펌 할인 광고 [mock:failed]", "failed", "GENERATION_FAILED"),
        ("펌 할인 광고 [mock:timeout]", "timeout", "TIMEOUT"),
    ],
)
def test_prompt_marker_forces_failure_that_can_be_retried(api, prompt, status, code):
    _, job = create(api, {"photos": [PHOTO], "prompt": prompt})
    finished = wait_until_finished(api, job["job_id"])
    assert (finished["status"], finished["error"]["code"]) == (status, code)
    assert "results" not in finished

    retry_status, retried = call(f"{api}/api/jobs/{job['job_id']}/retry", {})
    assert (retry_status, retried["retries"]) == (202, 1)


def test_succeeded_job_cannot_be_retried(api):
    _, job = create(api)
    wait_until_finished(api, job["job_id"])
    status, error = call(f"{api}/api/jobs/{job['job_id']}/retry", {})
    assert (status, error["code"]) == (409, "NOT_RETRYABLE")


def test_download_url_expires_and_rejects_tampering(api, monkeypatch):
    _, job = create(api)
    url = wait_until_finished(api, job["job_id"])["results"][0]["url"]
    status, error = call(api + url.replace("signature=", "signature=0"))
    assert (status, error["code"]) == (403, "INVALID_SIGNATURE")

    monkeypatch.setattr(signed_url, "URL_TTL_SECONDS", -1)
    expired_url = call(f"{api}/api/jobs/{job['job_id']}")[1]["results"][0]["url"]
    status, error = call(api + expired_url)
    assert (status, error["code"]) == (403, "URL_EXPIRED")


def test_finished_job_is_removed_after_retention(api, monkeypatch):
    _, job = create(api)
    wait_until_finished(api, job["job_id"])

    monkeypatch.setattr(jobs, "RESULT_TTL_SECONDS", 0)
    status, error = call(f"{api}/api/jobs/{job['job_id']}")
    assert (status, error["code"]) == (404, "JOB_NOT_FOUND")


def test_invalid_json_is_rejected(api):
    req = urllib.request.Request(
        f"{api}/api/jobs/generate",
        data=b"{not json",
        headers={"Content-Type": "application/json", "Idempotency-Key": "k"},
    )
    with pytest.raises(urllib.error.HTTPError) as caught:
        urllib.request.urlopen(req)
    assert (caught.value.code, json.load(caught.value)["code"]) == (400, "INVALID_JSON")


def test_oversized_body_is_rejected_before_reading(api):
    req = urllib.request.Request(
        f"{api}/api/jobs/generate",
        data=b"{}",
        headers={"Content-Type": "application/json", "Content-Length": str(10**9), "Idempotency-Key": "k"},
    )
    with pytest.raises(urllib.error.HTTPError) as caught:
        urllib.request.urlopen(req)
    assert (caught.value.code, json.load(caught.value)["code"]) == (413, "BODY_TOO_LARGE")


def test_unknown_path_returns_404(api):
    status, error = call(f"{api}/api/unknown")
    assert (status, error["code"]) == (404, "NOT_FOUND")
