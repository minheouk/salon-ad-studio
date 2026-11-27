"""진짜 모델 대신 돌려줄 가짜 결과(단색 PNG와 광고 문구).

레포에 이미지 파일을 두지 않으려고 PNG를 코드로 만든다.
"""

import struct
import zlib
from functools import lru_cache

PLACEHOLDER_SIDE = 1024
PLACEHOLDER_COLORS = ((236, 72, 153), (59, 130, 246), (16, 185, 129), (245, 158, 11), (139, 92, 246))
PRICE_TABLE_COLOR = (31, 41, 55)


@lru_cache(maxsize=len(PLACEHOLDER_COLORS) + 1)
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


def fake_ads(nail_tip_count: int) -> list[dict]:
    """가격표 이미지 1장을 맨 앞에 두고, 그 뒤에 네일 팁 이미지를 붙인다."""
    price_table = {"type": "price_table", "png": placeholder_png(PRICE_TABLE_COLOR)}
    nail_tips = [
        {"type": "nail_tip", "png": placeholder_png(PLACEHOLDER_COLORS[index % len(PLACEHOLDER_COLORS)])}
        for index in range(nail_tip_count)
    ]
    return [price_table, *nail_tips]


def fake_copy(store: dict[str, str]) -> dict:
    """API 명세대로 게시글 본문에 매장명, 인스타그램 계정명, 영업시간, 예약 링크를 모두 넣는다."""
    shop = store.get("shop", "")
    caption = "\n".join(
        [
            f"[목업] {shop} 이달의 아트",
            f"인스타그램 @{store.get('instagramId', '')}",
            f"영업시간 {store.get('businessHours', '')}",
            f"예약 {store.get('booking', '')}",
        ]
    )
    tags = ["#목업", "#이달의아트"] + ([f"#{shop.replace(' ', '')}"] if shop else [])
    hashtags = " ".join(tags)
    return {"caption": caption, "hashtags": hashtags}
