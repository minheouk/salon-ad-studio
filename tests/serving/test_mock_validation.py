"""목업 API 입력 검증 규칙 테스트."""

import base64
import struct
import zlib

import pytest

from src.pipelines.serving.mock.validation import (
    MAX_FILE_BYTES,
    ApiError,
    check_photo,
    check_photos,
    check_prompt,
)


def make_png(width: int, height: int) -> bytes:
    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))

    header = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    pixels = zlib.compress((b"\x00" + b"\x80" * width) * height)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", pixels) + chunk(b"IEND", b"")


def make_jpeg(width: int, height: int) -> bytes:
    """해상도 마커(SOF0)만 있는 최소 구조의 JPEG."""
    sof = struct.pack(">HBHHB", 11, 8, height, width, 1) + b"\x01\x11\x00"
    return b"\xff\xd8" + b"\xff\xc0" + sof + b"\xff\xd9"


def to_data_url(data: bytes, mime: str = "image/png") -> str:
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def error_code(call) -> str:
    with pytest.raises(ApiError) as caught:
        call()
    return caught.value.code


def test_png_and_jpeg_pass_with_their_size():
    png = check_photo(to_data_url(make_png(1024, 768)), 1)
    jpeg = check_photo(to_data_url(make_jpeg(4032, 3024), "image/jpeg"), 1)

    assert (png.format, png.width, png.height) == ("png", 1024, 768)
    assert (jpeg.format, jpeg.width, jpeg.height) == ("jpeg", 4032, 3024)


@pytest.mark.parametrize("prompt", ["", "   ", None])
def test_empty_prompt_is_rejected(prompt):
    assert error_code(lambda: check_prompt(prompt)) == "EMPTY_PROMPT"


def test_prompt_limit_is_100_chars():
    assert check_prompt("가" * 100) == "가" * 100
    assert error_code(lambda: check_prompt("가" * 101)) == "PROMPT_TOO_LONG"


@pytest.mark.parametrize("count", [0, 6])
def test_photo_count_must_be_1_to_5(count):
    photos = [to_data_url(make_png(600, 600))] * count
    assert error_code(lambda: check_photos(photos)) == "PHOTO_COUNT"


def test_only_png_and_jpg_are_accepted():
    gif = "data:image/gif;base64," + base64.b64encode(b"GIF89a").decode("ascii")
    assert error_code(lambda: check_photo(gif, 1)) == "UNSUPPORTED_FORMAT"


def test_file_over_10mb_is_rejected():
    big = make_png(600, 600) + b"\x00" * MAX_FILE_BYTES
    assert error_code(lambda: check_photo(to_data_url(big), 1)) == "FILE_TOO_LARGE"


@pytest.mark.parametrize(
    "data",
    [
        make_png(1024, 1024)[:-20],  # 업로드 중 잘린 PNG
        make_png(1024, 1024).replace(b"IDAT", b"IDAX"),  # 청크가 깨진 PNG (CRC 불일치)
        make_jpeg(1024, 1024)[:-2],  # 끝 표시가 없는 JPEG
        b"\x89PNG\r\n\x1a\n" + b"not a png",
        b"plain text pretending to be an image",
    ],
)
def test_broken_files_are_rejected(data):
    assert error_code(lambda: check_photo(to_data_url(data), 1)) == "BROKEN_IMAGE"


def test_invalid_base64_is_rejected():
    assert error_code(lambda: check_photo("data:image/png;base64,@@@", 1)) == "BROKEN_IMAGE"


@pytest.mark.parametrize(
    ("width", "height", "code"),
    [(511, 1024, "RESOLUTION_TOO_LOW"), (8193, 1024, "RESOLUTION_TOO_HIGH")],
)
def test_resolution_limits(width, height, code):
    photo = to_data_url(make_jpeg(width, height), "image/jpeg")
    assert error_code(lambda: check_photo(photo, 1)) == code
