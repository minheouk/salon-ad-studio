"""진짜 모델 대신 돌려줄 가짜 결과(단색 PNG와 광고 문구).

레포에 이미지 파일을 두지 않으려고 PNG를 코드로 만든다.
"""

import struct
import zlib
from functools import lru_cache

PLACEHOLDER_SIDE = 1024
PLACEHOLDER_COLORS = ((236, 72, 153), (59, 130, 246), (16, 185, 129), (245, 158, 11), (139, 92, 246))


@lru_cache(maxsize=len(PLACEHOLDER_COLORS))
def placeholder_png(color: tuple[int, int, int]) -> bytes:
    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))

    row = b"\x00" + bytes(color) * PLACEHOLDER_SIDE
    header = struct.pack(">IIBBBBB", PLACEHOLDER_SIDE, PLACEHOLDER_SIDE, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(row * PLACEHOLDER_SIDE))
        + chunk(b"IEND", b"")
    )


def fake_ads(count: int) -> list[dict]:
    return [
        {
            "png": placeholder_png(PLACEHOLDER_COLORS[index % len(PLACEHOLDER_COLORS)]),
            "copy": f"[목업] 광고 문구 {index + 1}",
        }
        for index in range(count)
    ]
