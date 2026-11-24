"""목업 작업(job) 관리.

- 작업은 한 번에 하나씩 순서대로 처리한다. 처리 중·대기 중인 작업이 가득 차면 새 요청을 거절한다.
- 상태는 processing에서 시작해 succeeded / failed / timeout / canceled 중 하나로 끝난다.
- 같은 Idempotency-Key로 다시 보내면 새 작업을 만들지 않고 처음 작업을 돌려준다.
- 결과는 succeeded가 된 시점부터 받을 수 있고, 끝난 작업은 24시간 뒤 지운다. 모두 메모리에만 둔다.
"""

import queue
import threading
import time
import uuid
from dataclasses import dataclass, field

from src.pipelines.serving.mock.fake_results import fake_ads
from src.pipelines.serving.mock.validation import ApiError

PROCESSING = "processing"
SUCCEEDED = "succeeded"
FAILED = "failed"
TIMEOUT = "timeout"
CANCELED = "canceled"
RETRYABLE = (FAILED, TIMEOUT)

# 작업 등록부터 끝날 때까지의 서버 상한. 웹 호출 제한(70초)보다 짧아야 화면이 항상 최종 상태를 받는다.
SERVER_TIMEOUT_SECONDS = 60
# 처리 중 1개 + 대기 2개. 순차 처리라 더 기다리게 하면 60초 안에 끝나기 어렵다.
MAX_PENDING_JOBS = 3
MAX_RETRIES = 5
RESULT_TTL_SECONDS = 24 * 60 * 60
# 화면이 "처리 중" 상태를 볼 수 있도록 결과를 바로 주지 않고 이만큼 기다린다.
MOCK_DELAY_SECONDS = 3
CHECK_INTERVAL_SECONDS = 0.05


@dataclass
class Job:
    kind: str
    result_count: int
    # 화면에서 실패·시간 초과를 시험할 수 있게, 목업 처리가 끝났을 때 이 상태로 끝낸다.
    mock_outcome: str = SUCCEEDED
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    status: str = PROCESSING
    retries: int = 0
    submitted_at: float = field(default_factory=time.monotonic)
    started_at: float | None = None
    finished_at: float | None = None
    finished_wall_time: float | None = None
    error: dict | None = None
    results: list[dict] = field(default_factory=list)

    def reset_for_retry(self) -> None:
        self.status = PROCESSING
        self.retries += 1
        self.submitted_at = time.monotonic()
        self.started_at = self.finished_at = self.finished_wall_time = None
        self.error = None


class JobStore:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.jobs: dict[str, Job] = {}
        self.keys: dict[str, tuple[str, str]] = {}  # Idempotency-Key -> (요청 지문, 작업 id)
        self.waiting: queue.Queue[Job] = queue.Queue()
        threading.Thread(target=self.work, daemon=True).start()

    def submit(
        self, key: str, fingerprint: str, kind: str, result_count: int, mock_outcome: str
    ) -> tuple[Job, bool]:
        """새 작업을 등록한다. 이미 쓴 키면 (처음 작업, True)를 돌려준다."""
        with self.lock:
            self.remove_expired()
            if key in self.keys:
                return self.replay(key, fingerprint), True
            self.ensure_capacity()
            job = Job(kind, result_count, mock_outcome)
            self.jobs[job.id] = job
            self.keys[key] = (fingerprint, job.id)
        self.waiting.put(job)
        return job, False

    def replay(self, key: str, fingerprint: str) -> Job:
        known_fingerprint, job_id = self.keys[key]
        if known_fingerprint != fingerprint:
            raise ApiError(
                "IDEMPOTENCY_CONFLICT", "같은 Idempotency-Key로 다른 내용을 보낼 수 없습니다.", 409
            )
        return self.jobs[job_id]

    def ensure_capacity(self) -> None:
        pending = sum(job.status == PROCESSING for job in self.jobs.values())
        if pending >= MAX_PENDING_JOBS:
            raise ApiError(
                "QUEUE_FULL", "지금 처리 중인 요청이 많습니다. 잠시 후 다시 시도해 주세요.", 429
            )

    def get(self, job_id: str) -> Job:
        with self.lock:
            self.remove_expired()
            if job_id not in self.jobs:
                raise ApiError("JOB_NOT_FOUND", "작업을 찾을 수 없습니다. 보관 기간이 지났을 수 있습니다.", 404)
            return self.jobs[job_id]

    def cancel(self, job_id: str) -> Job:
        job = self.get(job_id)
        with self.lock:
            if job.status != PROCESSING:
                raise ApiError("JOB_FINISHED", "이미 끝난 작업은 취소할 수 없습니다.", 409)
            self.finish(job, CANCELED)
        return job

    def retry(self, job_id: str) -> Job:
        job = self.get(job_id)
        with self.lock:
            if job.status not in RETRYABLE:
                raise ApiError("NOT_RETRYABLE", "실패하거나 시간이 초과된 작업만 다시 시도할 수 있습니다.", 409)
            if job.retries >= MAX_RETRIES:
                raise ApiError("RETRY_LIMIT", f"재시도는 {MAX_RETRIES}번까지만 할 수 있습니다.", 409)
            self.ensure_capacity()
            job.reset_for_retry()
        self.waiting.put(job)
        return job

    def work(self) -> None:
        while True:
            job = self.waiting.get()
            outcome = self.run(job)
            with self.lock:
                if job.status == PROCESSING:
                    self.finish(job, outcome)

    def run(self, job: Job) -> str:
        """가짜 생성. 실제 서빙에서는 여기서 모델을 부른다. 기다리는 동안 취소와 시간 초과를 확인한다."""
        job.started_at = time.monotonic()
        deadline = job.submitted_at + SERVER_TIMEOUT_SECONDS
        done_at = job.started_at + MOCK_DELAY_SECONDS
        while time.monotonic() < min(done_at, deadline):
            if job.status != PROCESSING:
                return job.status
            time.sleep(CHECK_INTERVAL_SECONDS)
        return job.mock_outcome if time.monotonic() < deadline else TIMEOUT

    def finish(self, job: Job, status: str) -> None:
        job.status = status
        job.finished_at = time.monotonic()
        job.finished_wall_time = time.time()
        if status == SUCCEEDED:
            job.results = fake_ads(job.result_count)
        if status == FAILED:
            job.error = {"code": "GENERATION_FAILED", "message": "광고를 만들지 못했습니다. 다시 시도해 주세요."}
        if status == TIMEOUT:
            job.error = {
                "code": "TIMEOUT",
                "message": f"처리 시간이 {SERVER_TIMEOUT_SECONDS}초를 넘었습니다. 다시 시도해 주세요.",
            }

    def remove_expired(self) -> None:
        now = time.time()
        expired = {
            job_id
            for job_id, job in self.jobs.items()
            if job.finished_wall_time is not None and now - job.finished_wall_time >= RESULT_TTL_SECONDS
        }
        for job_id in expired:
            del self.jobs[job_id]
        self.keys = {key: value for key, value in self.keys.items() if value[1] not in expired}
