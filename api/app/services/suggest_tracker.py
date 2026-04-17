"""In-process /suggest progress registry for Finding #18 heartbeat.

Design note: thoughts/shared/designs/suggest-heartbeat-2026-04-17.md (iOS repo).
Option 1 of 3: polling endpoint with a per-process dict + lock. Revisit with
a shared backend (Redis) when we scale beyond a single uvicorn worker.
"""
from dataclasses import dataclass
from threading import Lock
from time import monotonic, time
from typing import Optional


TERMINAL_TTL_SECONDS = 300


@dataclass
class JobEntry:
    state: str                      # "running" | "done" | "failed"
    stage: Optional[str]            # "vision" | "embedding_match" | "final" | None
    started_at: float               # wall-clock epoch seconds (for display)
    started_monotonic: float        # monotonic seconds (for elapsed math)
    terminal_monotonic: Optional[float]
    error_code: Optional[str]


class SuggestTracker:
    def __init__(self) -> None:
        self._jobs: dict[int, JobEntry] = {}
        self._lock = Lock()

    def start(self, photo_id: int) -> None:
        with self._lock:
            self._prune_locked()
            self._jobs[photo_id] = JobEntry(
                state="running",
                stage="vision",
                started_at=time(),
                started_monotonic=monotonic(),
                terminal_monotonic=None,
                error_code=None,
            )

    def update_stage(self, photo_id: int, stage: str) -> None:
        with self._lock:
            entry = self._jobs.get(photo_id)
            if entry is not None and entry.state == "running":
                entry.stage = stage

    def mark_done(self, photo_id: int) -> None:
        with self._lock:
            entry = self._jobs.get(photo_id)
            if entry is not None:
                entry.state = "done"
                entry.stage = "final"
                entry.terminal_monotonic = monotonic()

    def mark_failed(self, photo_id: int, error_code: str) -> None:
        with self._lock:
            entry = self._jobs.get(photo_id)
            if entry is not None:
                entry.state = "failed"
                entry.error_code = error_code
                entry.terminal_monotonic = monotonic()

    def get(self, photo_id: int) -> Optional[JobEntry]:
        with self._lock:
            self._prune_locked()
            return self._jobs.get(photo_id)

    def elapsed_ms(self, entry: JobEntry) -> int:
        end = entry.terminal_monotonic if entry.terminal_monotonic is not None else monotonic()
        return int((end - entry.started_monotonic) * 1000)

    def _prune_locked(self) -> None:
        now = monotonic()
        stale = [
            pid for pid, e in self._jobs.items()
            if e.terminal_monotonic is not None
            and (now - e.terminal_monotonic) > TERMINAL_TTL_SECONDS
        ]
        for pid in stale:
            del self._jobs[pid]


_tracker = SuggestTracker()


def get_tracker() -> SuggestTracker:
    return _tracker
