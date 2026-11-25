"""목업 API 서버를 실제로 띄워서 생성 요청 → 결과 다운로드 흐름을 확인하는 테스트."""

import json
import threading
import time
import urllib.error
import urllib.request
import uuid
from http.server import ThreadingHTTPServer

import pytest

from src.pipelines.serving.mock import generation, signed_url
from src.pipelines.serving.mock.server import MockHandler
from tests.serving.test_mock_validation import make_jpeg, make_png

STORE_FIELDS = {
    "shop": "오라네일",
    "instagramId": "ora_nail",
    "businessHours": "화~일 11:00–20:00 / 월 휴무",
    "booking": "https://example.com/reserve",
}
PROMPT = "겨울 분위기의 12월 이달의 아트를 만들어 주세요."


@pytest.fixture(scope="module")
def server_url():
    server = ThreadingHTTPServer(("127.0.0.1", 0), MockHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


@pytest.fixture
def api(server_url, monkeypatch):
    """테스트마다 빈 결과 저장소를 쓰고, 목업 대기 시간은 0초로 줄인다."""
    monkeypatch.setattr(generation, "MOCK_DELAY_SECONDS", 0)
    MockHandler.generator = generation.Generator()
    return server_url


def encode_form(fields: dict[str, str], images: list[bytes]) -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    parts = [
        f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
        for name, value in fields.items()
    ]
    for number, image in enumerate(images, start=1):
        header = (
            f"--{boundary}\r\nContent-Disposition: form-data; "
            f'name="images"; filename="nail-{number}.png"\r\nContent-Type: image/png\r\n\r\n'
        )
        parts.append(header.encode() + image + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def request(url: str, data: bytes | None = None, headers: dict | None = None):
    req = urllib.request.Request(url, data=data, headers=headers or {})
    try:
        with urllib.request.urlopen(req) as response:
            return response.status, response.headers, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.headers, error.read()


def generate(api: str, prompt: str = PROMPT, images: list[bytes] | None = None, path: str = "/generate"):
    images = [make_png(1024, 1024)] if images is None else images
    body, content_type = encode_form({**STORE_FIELDS, "prompt": prompt}, images)
    status, headers, raw = request(api + path, body, {"Content-Type": content_type})
    return status, headers, json.loads(raw)


def test_generate_returns_price_table_first_then_nail_tips(api):
    status, _, body = generate(api, images=[make_jpeg(4032, 3024)])
    assert status == 200
    assert [result["type"] for result in body["results"]] == ["price_table"] + ["nail_tip"] * 12
    assert set(body["timing_ms"]) == {"wait", "run", "total"}

    status, headers, png = request(api + body["results"][0]["url"])
    assert (status, headers["Content-Type"]) == (200, "image/png")
    assert png.startswith(b"\x89PNG")


@pytest.mark.parametrize(
    ("prompt", "images", "code"),
    [
        ("", None, "EMPTY_PROMPT"),
        (PROMPT, [], "PHOTO_COUNT"),
        (PROMPT, [make_png(1024, 1024)] * 2, "PHOTO_COUNT"),
        (PROMPT, [b"GIF89a"], "UNSUPPORTED_FORMAT"),
    ],
)
def test_invalid_input_is_rejected_with_400(api, prompt, images, code):
    status, _, body = generate(api, prompt, images)
    assert (status, body["code"]) == (400, code)


@pytest.mark.parametrize("path", ["/generate", "/copy"])
def test_200_char_prompt_with_trailing_space_is_accepted(api, path):
    assert generate(api, "가" * 200 + " ", path=path)[0] == 200


def test_copy_returns_caption_and_hashtags_in_the_same_response(api):
    status, _, body = generate(api, path="/copy")
    assert status == 200
    assert set(body) == {"timing_ms", "caption", "hashtags"}


@pytest.mark.parametrize(
    ("path", "marker", "status", "body"),
    [
        ("/generate", "[mock:failed]", 502, {"code": "GENERATION_FAILED", "message": "광고를 만들지 못했습니다. 다시 시도해 주세요."}),
        ("/copy", "[mock:failed]", 502, {"code": "GENERATION_FAILED", "message": "광고 문구를 만들지 못했습니다. 다시 시도해 주세요."}),
        ("/generate", "[mock:timeout]", 504, {"code": "TIMEOUT", "message": "처리 시간이 60초를 넘었습니다. 다시 시도해 주세요."}),
    ],
)
def test_prompt_marker_forces_failure(api, path, marker, status, body):
    result = generate(api, f"{PROMPT} {marker}", path=path)
    assert (result[0], result[2]) == (status, body)


def test_generation_over_the_server_limit_times_out(api, monkeypatch):
    monkeypatch.setattr(generation, "MOCK_DELAY_SECONDS", 1)
    monkeypatch.setattr(generation, "SERVER_TIMEOUT_SECONDS", 0.05)
    status, _, body = generate(api)
    assert (status, body["code"]) == (504, "TIMEOUT")


def test_new_request_is_rejected_when_queue_is_full(api, monkeypatch):
    monkeypatch.setattr(generation, "MOCK_DELAY_SECONDS", 1)
    waiting = [threading.Thread(target=generate, args=(api,)) for _ in range(generation.MAX_PENDING_REQUESTS)]
    for thread in waiting:
        thread.start()
    time.sleep(0.3)

    status, headers, body = generate(api)
    assert (status, body["code"], headers["Retry-After"]) == (429, "QUEUE_FULL", "10")
    for thread in waiting:
        thread.join()


def test_download_url_expires_and_rejects_tampering(api, monkeypatch):
    url = generate(api)[2]["results"][0]["url"]
    status, _, raw = request(api + url.replace("signature=", "signature=0"))
    assert (status, json.loads(raw)["code"]) == (403, "INVALID_SIGNATURE")

    monkeypatch.setattr(signed_url, "URL_TTL_SECONDS", -1)
    expired_url = generate(api)[2]["results"][0]["url"]
    status, _, raw = request(api + expired_url)
    assert (status, json.loads(raw)["code"]) == (403, "URL_EXPIRED")


def test_result_is_removed_after_retention(api, monkeypatch):
    url = generate(api)[2]["results"][0]["url"]
    monkeypatch.setattr(generation, "RESULT_TTL_SECONDS", 0)
    status, _, raw = request(api + url)
    assert (status, json.loads(raw)["code"]) == (404, "RESULT_NOT_FOUND")


def test_json_body_is_rejected(api):
    status, _, raw = request(f"{api}/generate", b"{}", {"Content-Type": "application/json"})
    assert (status, json.loads(raw)["code"]) == (415, "INVALID_FORM")


def test_oversized_body_is_rejected_before_reading(api):
    headers = {"Content-Type": "multipart/form-data; boundary=x", "Content-Length": str(10**9)}
    status, _, raw = request(f"{api}/generate", b"--x--", headers)
    assert (status, json.loads(raw)["code"]) == (413, "BODY_TOO_LARGE")


@pytest.mark.parametrize("path", ["/api/unknown", "/api/jobs/generate"])
def test_unknown_path_returns_404(api, path):
    status, _, raw = request(api + path, b"")
    assert (status, json.loads(raw)["code"]) == (404, "NOT_FOUND")
