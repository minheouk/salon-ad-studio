"""목업 API의 입력 검증.

실제 서빙 API도 같은 규칙으로 검사하도록, 검증만 따로 모아 둔다.
이미지는 Pillow 없이 파일 구조(시그니처, 청크, 마커)만 읽어서 손상 여부와 해상도를 확인한다.
"""

import struct
import zlib
from dataclasses import dataclass

# /generate: 웹은 네일 팁 세트 사진 1장을 보낸다.
MAX_PHOTOS = 1
# /copy: 게시 미리보기에서 고른 최종 이미지. 생성 결과 최대 장수(가격표 1 + 네일 팁 12)로 임시로 정했다.
# 장수·용량 한도는 아직 정해지지 않았다. 최종 작업 후 서비스팀과 맞춘다.
MAX_COPY_IMAGES = 13
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_PROMPT_CHARS = 200
# 짧은 변이 512px보다 작으면 광고 이미지로 쓰기에 화질이 부족하다.
MIN_SIDE = 512
# 48MP 이상 원본처럼 긴 변이 8192px을 넘는 사진은 받지 않는다. 일반 휴대폰 사진(12MP, 4032px)은 통과한다.
MAX_SIDE = 8192

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
JPEG_SIGNATURE = b"\xff\xd8\xff"
# 해상도가 들어 있는 JPEG SOF 마커 (C4, C8, CC는 다른 용도라 제외)
JPEG_SOF_MARKERS = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
JPEG_SOS_MARKER = 0xDA


class ApiError(Exception):
    """API가 돌려주는 오류. code는 화면이 분기할 때 쓰고, message는 사용자에게 그대로 보여 준다.

    status는 HTTP 상태 번호, field는 문제가 된 요청 필드 이름(필드와 관계없는 오류는 None).
    """

    def __init__(self, code: str, message: str, status: int = 400, field: str | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.field = field


@dataclass(frozen=True)
class Photo:
    format: str
    width: int
    height: int


def check_prompt(prompt: object, field: str = "prompt") -> str:
    """앞뒤 공백을 뺀 값으로 비었는지와 길이를 검사한다. 목업은 빈 값을 받지 않는 실제 서비스 규칙을 따른다."""
    text = prompt.strip() if isinstance(prompt, str) else ""
    if not text:
        raise ApiError("EMPTY_PROMPT", "요청 내용을 입력해 주세요.", field=field)
    if len(text) > MAX_PROMPT_CHARS:
        raise ApiError(
            "PROMPT_TOO_LONG",
            f"요청 내용은 {MAX_PROMPT_CHARS}자 이하로 입력해 주세요.",
            field=field,
        )
    return text


def check_edit_prompts(prompts: list[str]) -> list[str]:
    """수정 단계에서 쓴 수정 프롬프트. 수정하지 않았으면 없어도 되고, 보낸 값은 prompt와 같은 규칙으로 검사한다."""
    return [check_prompt(prompt, field="editPrompts") for prompt in prompts]


def check_edit_instructions(common: str | None, individual: str | None) -> str:
    """/edit: 공통 프롬프트(prompt)와 개별 프롬프트(imagePrompt) 중 하나 이상.

    보낸 값은 prompt와 같은 규칙으로 검사한다. 둘 다 없으면 prompt가 비었다고 알린다.
    """
    sent = {
        name: value
        for name, value in (("prompt", common), ("imagePrompt", individual))
        if value is not None
    }
    if not sent:
        raise ApiError("EMPTY_PROMPT", "요청 내용을 입력해 주세요.", field="prompt")
    return " ".join(check_prompt(value, field=name) for name, value in sent.items())


def check_photos(photos: list[bytes]) -> list[Photo]:
    """/generate·/edit: 이미지 정확히 1장."""
    if len(photos) != MAX_PHOTOS:
        raise ApiError("PHOTO_COUNT", f"사진은 {MAX_PHOTOS}장만 올릴 수 있습니다.", field="images")
    return [check_photo(photo, number) for number, photo in enumerate(photos, start=1)]


def check_copy_images(images: list[bytes]) -> list[Photo]:
    """/copy: 게시할 최종 이미지 1장 이상, 게시 순서대로."""
    if not 1 <= len(images) <= MAX_COPY_IMAGES:
        raise ApiError(
            "PHOTO_COUNT", f"게시할 이미지는 1~{MAX_COPY_IMAGES}장을 보내 주세요.", field="images"
        )
    return [check_photo(image, number) for number, image in enumerate(images, start=1)]


def check_photo(data: bytes, number: int) -> Photo:
    """파일 내용으로 형식을 판별한다. 파일 이름이나 Content-Type 표기는 믿지 않는다."""
    if len(data) > MAX_FILE_BYTES:
        raise ApiError(
            "FILE_TOO_LARGE",
            f"{number}번째 사진이 {MAX_FILE_BYTES // (1024 * 1024)}MB를 넘습니다.",
            field="images",
        )
    if data.startswith(PNG_SIGNATURE):
        fmt, size = "png", read_png_size(data)
    elif data.startswith(JPEG_SIGNATURE):
        fmt, size = "jpeg", read_jpeg_size(data)
    else:
        raise ApiError(
            "UNSUPPORTED_FORMAT",
            f"{number}번째 사진은 PNG 또는 JPG 파일만 올릴 수 있습니다.",
            field="images",
        )
    if size is None:
        raise ApiError(
            "BROKEN_IMAGE",
            f"{number}번째 사진 파일이 손상되었습니다. 다른 사진을 올려 주세요.",
            field="images",
        )
    photo = Photo(fmt, *size)
    check_resolution(photo, number)
    return photo


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
            field="images",
        )
    if max(photo.width, photo.height) > MAX_SIDE:
        raise ApiError(
            "RESOLUTION_TOO_HIGH",
            f"{number}번째 사진의 해상도가 너무 높습니다. 긴 변이 {MAX_SIDE}px 이하인 사진을 올려 주세요.",
            field="images",
        )
