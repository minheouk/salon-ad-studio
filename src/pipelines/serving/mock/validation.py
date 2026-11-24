"""목업 API의 입력 검증.

실제 서빙 API도 같은 규칙으로 검사하도록, 검증만 따로 모아 둔다.
이미지는 Pillow 없이 파일 구조(시그니처, 청크, 마커)만 읽어서 손상 여부와 해상도를 확인한다.
"""

import base64
import binascii
import struct
import zlib
from dataclasses import dataclass

MAX_PHOTOS = 5
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_PROMPT_CHARS = 100
# 짧은 변이 512px보다 작으면 광고 이미지로 쓰기에 화질이 부족하다.
MIN_SIDE = 512
# 48MP 이상 원본처럼 긴 변이 8192px을 넘는 사진은 받지 않는다. 일반 휴대폰 사진(12MP, 4032px)은 통과한다.
MAX_SIDE = 8192

DATA_URL_PREFIXES = (
    "data:image/png;base64,",
    "data:image/jpeg;base64,",
    "data:image/jpg;base64,",
)
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
JPEG_SIGNATURE = b"\xff\xd8\xff"
# 해상도가 들어 있는 JPEG SOF 마커 (C4, C8, CC는 다른 용도라 제외)
JPEG_SOF_MARKERS = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
JPEG_SOS_MARKER = 0xDA


class ApiError(Exception):
    """API가 돌려주는 오류. code는 화면이 분기할 때 쓰고, message는 사용자에게 그대로 보여 준다. status는 HTTP 상태 번호."""

    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


@dataclass(frozen=True)
class Photo:
    format: str
    width: int
    height: int


def check_prompt(prompt: object) -> str:
    if not isinstance(prompt, str) or not prompt.strip():
        raise ApiError("EMPTY_PROMPT", "요청 내용을 입력해 주세요.")
    if len(prompt) > MAX_PROMPT_CHARS:
        raise ApiError(
            "PROMPT_TOO_LONG", f"요청 내용은 {MAX_PROMPT_CHARS}자 이하로 입력해 주세요."
        )
    return prompt.strip()


def check_photos(photos: object) -> list[Photo]:
    if not isinstance(photos, list) or not 1 <= len(photos) <= MAX_PHOTOS:
        raise ApiError("PHOTO_COUNT", f"사진은 1장에서 {MAX_PHOTOS}장까지 올릴 수 있습니다.")
    return [check_photo(photo, number) for number, photo in enumerate(photos, start=1)]


def check_photo(data_url: object, number: int) -> Photo:
    data = decode_data_url(data_url, number)
    if len(data) > MAX_FILE_BYTES:
        raise ApiError(
            "FILE_TOO_LARGE",
            f"{number}번째 사진이 {MAX_FILE_BYTES // (1024 * 1024)}MB를 넘습니다.",
        )
    photo = read_photo(data)
    if photo is None:
        raise ApiError(
            "BROKEN_IMAGE", f"{number}번째 사진 파일이 손상되었습니다. 다른 사진을 올려 주세요."
        )
    check_resolution(photo, number)
    return photo


def decode_data_url(data_url: object, number: int) -> bytes:
    if not isinstance(data_url, str) or not data_url.startswith(DATA_URL_PREFIXES):
        raise ApiError(
            "UNSUPPORTED_FORMAT", f"{number}번째 사진은 PNG 또는 JPG 파일만 올릴 수 있습니다."
        )
    encoded = data_url.partition(",")[2]
    try:
        return base64.b64decode(encoded, validate=True)
    except binascii.Error:
        raise ApiError(
            "BROKEN_IMAGE", f"{number}번째 사진 파일이 손상되었습니다. 다른 사진을 올려 주세요."
        ) from None


def read_photo(data: bytes) -> Photo | None:
    """파일 내용으로 형식을 판별한다. 확장자나 data URL의 형식 표기는 믿지 않는다."""
    if data.startswith(PNG_SIGNATURE):
        size = read_png_size(data)
        fmt = "png"
    elif data.startswith(JPEG_SIGNATURE):
        size = read_jpeg_size(data)
        fmt = "jpeg"
    else:
        return None
    if size is None:
        return None
    return Photo(fmt, *size)


def read_png_size(data: bytes) -> tuple[int, int] | None:
    """모든 청크의 CRC를 확인하고 IEND까지 읽어야 정상 파일로 본다. 중간에 잘린 업로드도 여기서 걸린다."""
    pos = len(PNG_SIGNATURE)
    size = None
    while pos + 12 <= len(data):
        length, kind = struct.unpack(">I4s", data[pos : pos + 8])
        end = pos + 12 + length
        if end > len(data):
            return None
        body = data[pos + 8 : end - 4]
        if zlib.crc32(kind + body) != struct.unpack(">I", data[end - 4 : end])[0]:
            return None
        if kind == b"IHDR":
            size = struct.unpack(">II", body[:8])
        if kind == b"IEND":
            return size
        pos = end
    return None


def read_jpeg_size(data: bytes) -> tuple[int, int] | None:
    """끝 표시(EOI)가 있어야 하고, 본문(SOS) 전에 해상도 마커(SOF)가 나와야 정상 파일로 본다."""
    if not data.rstrip(b"\x00").endswith(b"\xff\xd9"):
        return None
    pos = len(JPEG_SIGNATURE) - 1
    while pos + 4 <= len(data):
        if data[pos] != 0xFF:
            return None
        marker = data[pos + 1]
        if marker in JPEG_SOF_MARKERS and pos + 9 <= len(data):
            height, width = struct.unpack(">HH", data[pos + 5 : pos + 9])
            return width, height
        if marker == JPEG_SOS_MARKER:
            return None
        pos += 2 + struct.unpack(">H", data[pos + 2 : pos + 4])[0]
    return None


def check_resolution(photo: Photo, number: int) -> None:
    if min(photo.width, photo.height) < MIN_SIDE:
        raise ApiError(
            "RESOLUTION_TOO_LOW",
            f"{number}번째 사진의 해상도가 너무 낮습니다. 짧은 변이 {MIN_SIDE}px 이상인 사진을 올려 주세요.",
        )
    if max(photo.width, photo.height) > MAX_SIDE:
        raise ApiError(
            "RESOLUTION_TOO_HIGH",
            f"{number}번째 사진의 해상도가 너무 높습니다. 긴 변이 {MAX_SIDE}px 이하인 사진을 올려 주세요.",
        )
