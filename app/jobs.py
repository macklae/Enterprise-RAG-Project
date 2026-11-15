"""Background ingestion jobs with live progress. One worker, so embedding runs one file at a time."""
from __future__ import annotations

import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Optional


@dataclass
class Job:
    id: str
    filename: str
    status: str = "queued"  # queued | running | done | error
    stage: str = "queued"
    progress: int = 0
    message: str = "Waiting"
    result: Optional[dict[str, Any]] = None
    error: Optional[str] = None
    created: float = field(default_factory=time.time)

    def view(self) -> dict[str, Any]:
        return asdict(self)


class JobManager:
    def __init__(self):
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ingest")

    def submit(self, filename: str, fn: Callable[[Callable[[str, int, str], None]], Any]) -> Job:
        job = Job(id=uuid.uuid4().hex[:12], filename=filename)
        with self._lock:
            self._jobs[job.id] = job
            for old in sorted(self._jobs.values(), key=lambda j: j.created)[:-50]:
                self._jobs.pop(old.id, None)

        def progress(stage: str, pct: int, message: str) -> None:
            job.stage, job.progress, job.message = stage, pct, message

        def run():
            job.status = "running"
            try:
                res = fn(progress)
                job.result = res if isinstance(res, dict) else getattr(res, "__dict__", None)
                job.status, job.stage, job.progress, job.message = "done", "done", 100, "Finished"
            except Exception as exc:  # message is shown in the UI
                job.status, job.stage = "error", "error"
                job.error = str(exc) or type(exc).__name__
                job.message = job.error

        self._pool.submit(run)
        return job

    def get(self, job_id: str) -> Optional[Job]:
        return self._jobs.get(job_id)

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        jobs = sorted(self._jobs.values(), key=lambda j: j.created, reverse=True)[:limit]
        return [j.view() for j in jobs]
