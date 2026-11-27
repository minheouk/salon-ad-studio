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


def encode_form(
    fields: list[tuple[str, str]], images: list[bytes], file_field: str = "images"
) -> tuple[bytes, str]:
    """같은 이름의 필드를 여러 번 보낼 수 있도록 (이름, 값) 목록을 받는다.

    file_field는 이미지를 담을 요청 필드 이름이다. /edit은 image를 쓴다.
    """
    boundary = uuid.uuid4().hex
    parts = [
        f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
        for name, value in fields
    ]
    for number, image in enumerate(images, start=1):
        header = (
            f"--{boundary}\r\nContent-Disposition: form-data; "
            f'name="{file_field}"; filename="nail-{number}.png"\r\nContent-Type: image/png\r\n\r\n'
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
    body, content_type = encode_form([*STORE_FIELDS.items(), ("prompt", prompt)], images)
    status, headers, raw = request(api + path, body, {"Content-Type": content_type})
    return status, headers, json.loads(raw)


def copy(api: str, images: list[bytes] | None = None):
    """웹 백엔드처럼 매장 정보와 게시할 이미지만 보낸다. 프롬프트는 보내지 않는다."""
    images = [make_png(1024, 1024)] if images is None else images
    body, content_type = encode_form(list(STORE_FIELDS.items()), images)
    status, headers, raw = request(api + "/copy", body, {"Content-Type": content_type})
    return status, headers, json.loads(raw)


def edit(
    api: str, fields: list[tuple[str, str]], images: list[bytes] | None = None, file_field: str = "image"
):
    images = [make_png(1024, 1024)] if images is None else images
    body, content_type = encode_form(fields, images, file_field)
    status, headers, raw = request(api + "/edit", body, {"Content-Type": content_type})
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
    ("prompt", "images", "code", "field"),
    [
        ("가" * 201, None, "PROMPT_TOO_LONG", "prompt"),
        (PROMPT, [], "PHOTO_COUNT", "images"),
        (PROMPT, [make_png(1024, 1024)] * 2, "PHOTO_COUNT", "images"),
        (PROMPT, [b"GIF89a"], "UNSUPPORTED_FORMAT", "images"),
    ],
)
def test_invalid_input_is_rejected_with_400_and_field(api, prompt, images, code, field):
    status, _, body = generate(api, prompt, images)
    assert (status, body["error"]["code"], body["error"]["field"]) == (400, code, field)


def test_200_char_prompt_is_accepted(api):
    assert generate(api, "가" * 200 + " ")[0] == 200


@pytest.mark.parametrize("prompt", ["", "   "])
def test_generate_still_requires_prompt(api, prompt):
    status, _, body = generate(api, prompt)
    assert (status, body["error"]["code"], body["error"]["field"]) == (400, "EMPTY_PROMPT", "prompt")


def test_copy_works_without_prompt(api):
    status, _, body = copy(api, images=[make_png(1024, 1024)] * 3)
    assert status == 200
    assert set(body) == {"timing_ms", "caption", "hashtags"}


@pytest.mark.parametrize("prompt", ["", "   ", "가" * 201])
def test_copy_does_not_check_prompt_even_if_sent(api, prompt):
    assert generate(api, prompt, path="/copy")[0] == 200


def test_copy_caption_includes_all_store_fields(api):
    caption = copy(api)[2]["caption"]
    for value in STORE_FIELDS.values():
        assert value in caption


@pytest.mark.parametrize("count", [0, 14])
def test_copy_rejects_wrong_image_count(api, count):
    status, _, body = copy(api, images=[make_png(1024, 1024)] * count)
    assert (status, body["error"]["code"], body["error"]["field"]) == (400, "PHOTO_COUNT", "images")


def test_edit_returns_one_downloadable_result(api):
    status, _, body = edit(api, [("commonPrompt", "배경을 더 밝게 바꿔 주세요.")])
    assert status == 200
    assert set(body) == {"generation_id", "timing_ms", "result", "results_expire_in_seconds"}

    status, headers, png = request(api + body["result"]["url"])
    assert (status, headers["Content-Type"]) == (200, "image/png")
    assert png.startswith(b"\x89PNG")


@pytest.mark.parametrize(
    "fields",
    [
        [("commonPrompt", "배경을 더 밝게")],
        [("imagePrompt", "글리터를 더 반짝이게")],
        [("commonPrompt", "밝게"), ("imagePrompt", "반짝이게")],
    ],
)
def test_edit_accepts_common_or_individual_prompt(api, fields):
    assert edit(api, fields)[0] == 200


@pytest.mark.parametrize(
    ("fields", "images", "code", "field"),
    [
        ([], None, "EMPTY_PROMPT", "commonPrompt"),
        ([("commonPrompt", "밝게"), ("imagePrompt", " ")], None, "EMPTY_PROMPT", "imagePrompt"),
        ([("imagePrompt", "가" * 201)], None, "PROMPT_TOO_LONG", "imagePrompt"),
        ([("commonPrompt", "밝게")], [make_png(1024, 1024)] * 2, "PHOTO_COUNT", "image"),
        ([("commonPrompt", "밝게")], [b"GIF89a"], "UNSUPPORTED_FORMAT", "image"),
    ],
)
def test_edit_rejects_invalid_input(api, fields, images, code, field):
    status, _, body = edit(api, fields, images)
    assert (status, body["error"]["code"], body["error"]["field"]) == (400, code, field)


def test_edit_takes_image_not_images(api):
    """/generate·/copy의 images로 보내면 수정할 이미지가 없는 것으로 본다."""
    status, _, body = edit(api, [("commonPrompt", "밝게")], file_field="images")
    assert (status, body["error"]["code"], body["error"]["field"]) == (400, "PHOTO_COUNT", "image")


@pytest.mark.parametrize("name", ["commonPrompt", "imagePrompt"])
def test_edit_marker_forces_failure_in_either_prompt(api, name):
    status, _, body = edit(api, [(name, "밝게 [mock:failed]")])
    message = "광고를 수정하지 못했습니다. 다시 시도해 주세요."
    assert (status, body) == (502, {"error": {"code": "GENERATION_FAILED", "field": None, "message": message}})


@pytest.mark.parametrize(
    ("path", "marker", "status", "body"),
    [
        ("/generate", "[mock:failed]", 502, ("GENERATION_FAILED", "광고를 만들지 못했습니다. 다시 시도해 주세요.")),
        ("/copy", "[mock:failed]", 502, ("GENERATION_FAILED", "광고 문구를 만들지 못했습니다. 다시 시도해 주세요.")),
        ("/generate", "[mock:timeout]", 504, ("TIMEOUT", "처리 시간이 60초를 넘었습니다. 다시 시도해 주세요.")),
    ],
)
def test_prompt_marker_forces_failure(api, path, marker, status, body):
    code, message = body
    result = generate(api, f"{PROMPT} {marker}", path=path)
    assert (result[0], result[2]) == (status, {"error": {"code": code, "field": None, "message": message}})


def test_generation_over_the_server_limit_times_out(api, monkeypatch):
    monkeypatch.setattr(generation, "MOCK_DELAY_SECONDS", 1)
    monkeypatch.setattr(generation, "SERVER_TIMEOUT_SECONDS", 0.05)
    status, _, body = generate(api)
    assert (status, body["error"]["code"]) == (504, "TIMEOUT")


def test_new_request_is_rejected_when_queue_is_full(api, monkeypatch):
    monkeypatch.setattr(generation, "MOCK_DELAY_SECONDS", 1)
    waiting = [threading.Thread(target=generate, args=(api,)) for _ in range(generation.MAX_PENDING_REQUESTS)]
    for thread in waiting:
        thread.start()
    time.sleep(0.3)

    status, headers, body = generate(api)
    assert (status, body["error"]["code"], headers["Retry-After"]) == (429, "QUEUE_FULL", "10")
    for thread in waiting:
        thread.join()


def test_download_url_expires_and_rejects_tampering(api, monkeypatch):
    url = generate(api)[2]["results"][0]["url"]
    status, _, raw = request(api + url.replace("signature=", "signature=0"))
    assert (status, json.loads(raw)["error"]["code"]) == (403, "INVALID_SIGNATURE")

    monkeypatch.setattr(signed_url, "URL_TTL_SECONDS", -1)
    expired_url = generate(api)[2]["results"][0]["url"]
    status, _, raw = request(api + expired_url)
    assert (status, json.loads(raw)["error"]["code"]) == (403, "URL_EXPIRED")


def test_result_is_removed_after_retention(api, monkeypatch):
    url = generate(api)[2]["results"][0]["url"]
    monkeypatch.setattr(generation, "RESULT_TTL_SECONDS", 0)
    status, _, raw = request(api + url)
    assert (status, json.loads(raw)["error"]["code"]) == (404, "RESULT_NOT_FOUND")


def test_json_body_is_rejected(api):
    status, _, raw = request(f"{api}/generate", b"{}", {"Content-Type": "application/json"})
    assert (status, json.loads(raw)["error"]["code"]) == (415, "INVALID_FORM")


def test_oversized_body_is_rejected_before_reading(api):
    headers = {"Content-Type": "multipart/form-data; boundary=x", "Content-Length": str(10**9)}
    status, _, raw = request(f"{api}/generate", b"--x--", headers)
    assert (status, json.loads(raw)["error"]["code"]) == (413, "BODY_TOO_LARGE")


@pytest.mark.parametrize("path", ["/api/unknown", "/api/jobs/generate"])
def test_unknown_path_returns_404(api, path):
    status, _, raw = request(api + path, b"")
    assert (status, json.loads(raw)["error"]["code"]) == (404, "NOT_FOUND")
