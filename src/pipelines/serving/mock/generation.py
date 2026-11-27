"""목업 생성 처리.

- 요청 하나를 받으면 끝날 때까지 기다렸다가 결과를 돌려준다 (동기 방식).
- 생성은 한 번에 하나씩 순서대로 처리한다. 처리 중·대기 중인 요청이 가득 차면 새 요청을 거절한다.
- 결과 이미지는 다운로드할 수 있게 24시간 보관한다. 모두 메모리에만 둔다.
"""

import threading
import time
import uuid
from dataclasses import dataclass, field

from src.pipelines.serving.mock.fake_results import fake_ads, fake_copy, fake_edit
from src.pipelines.serving.mock.validation import ApiError

# 목업이 끝날 상태. 프롬프트의 표시로 실패·시간 초과를 일부러 만들 수 있다.
SUCCEEDED = "succeeded"
FAILED = "failed"
TIMEOUT = "timeout"

# 생성 종류. 이미지(/generate), 이미지 수정(/edit), 광고 문구(/copy)는 같은 대기열에서 차례로 처리한다.
IMAGES = "images"
EDIT = "edit"
COPY = "copy"

# 결과 이미지는 가격표 1장 + 네일 팁 최대 12장. 목업은 최대 장수로 만든다.
NAIL_TIP_COUNT = 12
# 요청을 받은 때부터 끝날 때까지의 서버 상한. 웹 호출 제한(70초)보다 짧아야 웹이 항상 서버의 응답을 받는다.
SERVER_TIMEOUT_SECONDS = 60
# 처리 중 1개 + 대기 2개. 순차 처리라 더 기다리게 하면 60초 안에 끝나기 어렵다.
MAX_PENDING_REQUESTS = 3
RESULT_TTL_SECONDS = 24 * 60 * 60
# 웹에서 "처리 중" 화면을 볼 수 있도록 결과를 바로 주지 않고 이만큼 기다린다.
MOCK_DELAY_SECONDS = 3

FAILED_MESSAGES = {
    IMAGES: "광고를 만들지 못했습니다. 다시 시도해 주세요.",
    EDIT: "광고를 수정하지 못했습니다. 다시 시도해 주세요.",
    COPY: "광고 문구를 만들지 못했습니다. 다시 시도해 주세요.",
}


@dataclass
class Generation:
    kind: str
    received_at: float
    started_at: float
    finished_at: float
    results: list[dict] = field(default_factory=list)
    copy: dict | None = None
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    finished_wall_time: float = field(default_factory=time.time)


def timeout_error() -> ApiError:
    # 서빙은 TIMEOUT을 보내고, 웹 백엔드가 사용자에게 GENERATION_TIMEOUT으로 바꿔 보낸다.
    return ApiError(
        "TIMEOUT", f"처리 시간이 {SERVER_TIMEOUT_SECONDS}초를 넘었습니다. 다시 시도해 주세요.", 504
    )


class Generator:
    def __init__(self) -> None:
        self.slots = threading.BoundedSemaphore(MAX_PENDING_REQUESTS)
        self.turn = threading.Lock()
        self.lock = threading.Lock()
        self.generations: dict[str, Generation] = {}

    def generate(self, kind: str, outcome: str, received_at: float, store: dict[str, str]) -> Generation:
        """자리가 없으면 바로 거절하고, 있으면 차례를 기다렸다가 생성한다."""
        if not self.slots.acquire(blocking=False):
            raise ApiError(
                "QUEUE_FULL", "지금 처리 중인 요청이 많습니다. 잠시 후 다시 시도해 주세요.", 429
            )
        try:
            return self.run_in_turn(kind, outcome, received_at, store)
        finally:
            self.slots.release()

    def run_in_turn(self, kind: str, outcome: str, received_at: float, store: dict[str, str]) -> Generation:
        deadline = received_at + SERVER_TIMEOUT_SECONDS
        if not self.turn.acquire(timeout=max(0.0, deadline - time.monotonic())):
            raise timeout_error()
        try:
            return self.run(kind, outcome, received_at, deadline, store)
        finally:
            self.turn.release()

    def run(
        self, kind: str, outcome: str, received_at: float, deadline: float, store: dict[str, str]
    ) -> Generation:
        """가짜 생성. 실제 서빙에서는 여기서 모델을 부른다."""
        started_at = time.monotonic()
        remaining = max(0.0, deadline - started_at)
        # 끝난 뒤 시계를 다시 보지 않고 미리 판단한다. Windows의 monotonic 시계는 해상도가 낮아 경계에서 어긋난다.
        timed_out = outcome == TIMEOUT or MOCK_DELAY_SECONDS >= remaining
        time.sleep(min(MOCK_DELAY_SECONDS, remaining))
        if timed_out:
            raise timeout_error()
        if outcome == FAILED:
            raise ApiError("GENERATION_FAILED", FAILED_MESSAGES[kind], 502)
        if kind == COPY:
            return Generation(kind, received_at, started_at, time.monotonic(), copy=fake_copy(store))
        results = fake_edit() if kind == EDIT else fake_ads(NAIL_TIP_COUNT)
        generation = Generation(kind, received_at, started_at, time.monotonic(), results=results)
        # 다운로드할 이미지가 있는 결과만 보관한다.
        with self.lock:
            self.remove_expired()
            self.generations[generation.id] = generation
        return generation

    def result_png(self, generation_id: str, number: int) -> bytes:
        with self.lock:
            self.remove_expired()
            generation = self.generations.get(generation_id)
        if generation is None or not 1 <= number <= len(generation.results):
            raise ApiError(
                "RESULT_NOT_FOUND", "결과를 찾을 수 없습니다. 보관 기간이 지났을 수 있습니다.", 404
            )
        return generation.results[number - 1]["png"]

    def remove_expired(self) -> None:
        now = time.time()
        self.generations = {
            generation_id: generation
            for generation_id, generation in self.generations.items()
            if now - generation.finished_wall_time < RESULT_TTL_SECONDS
        }
