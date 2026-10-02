from __future__ import annotations

import threading

from .models import IndexState, ProgressSnapshot


class IndexProgress:
    """Thread-safe view of a running index pass, read by the index_status tool."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state = IndexState.IDLE
        self._pending = 0
        self._current: str | None = None

    def set_state(self, state: IndexState) -> None:
        with self._lock:
            self._state = state

    def set_pending(self, pending: int) -> None:
        with self._lock:
            self._pending = pending

    def set_current(self, current: str | None) -> None:
        with self._lock:
            self._current = current

    def finish(self) -> None:
        with self._lock:
            self._state, self._pending, self._current = IndexState.IDLE, 0, None

    def snapshot(self) -> ProgressSnapshot:
        with self._lock:
            return ProgressSnapshot(self._state, self._pending, self._current)
