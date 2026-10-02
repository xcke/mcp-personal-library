from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from .config import Config
from .indexer import document_kind, paths_under, sync_paths

log = logging.getLogger("plib.watcher")

TICK_SECONDS = 0.1
RELEVANT_EVENTS = {"created", "modified", "deleted", "moved"}


@dataclass(frozen=True)
class WatchTiming:
    debounce: float = 2.0
    recheck: float = 1.0


@dataclass
class _Pending:
    due: float
    is_directory: bool
    checked: bool = False
    signature: tuple[int, int] | None = None  # (size, mtime_ns); None while absent


class LibraryWatcher:
    """Watches the library root and keeps the index current.

    Observing can start before the initial index pass finishes (events are buffered);
    processing must wait for it, or the pass's cleanup would drop files added meanwhile.
    """

    def __init__(self, config: Config, timing: WatchTiming = WatchTiming(),
                 workers: int | None = None) -> None:
        self._config = config
        self._timing = timing
        self._workers = workers
        self._lock = threading.Lock()
        self._pending: dict[str, _Pending] = {}
        self._stopping = threading.Event()
        self._observer = Observer()
        self._processor = threading.Thread(target=self._process_loop, name="watcher", daemon=True)

    def start_observing(self) -> None:
        self._observer.schedule(_EventHandler(self), str(self._config.root), recursive=True)
        self._observer.start()

    def start_processing(self) -> None:
        self._processor.start()

    def stop(self) -> None:
        self._stopping.set()
        if self._observer.is_alive():
            self._observer.stop()
            self._observer.join(timeout=5)
        if self._processor.is_alive():
            self._processor.join(timeout=30)

    def touch(self, absolute_path: str, is_directory: bool) -> None:
        try:
            rel = Path(absolute_path).relative_to(self._config.root).as_posix()
        except ValueError:
            return
        parts = rel.split("/")
        if any(part.startswith(".") for part in parts):
            return
        if not is_directory and document_kind(parts[-1]) is None:
            return
        with self._lock:
            self._pending[rel] = _Pending(time.monotonic() + self._timing.debounce, is_directory)

    def _process_loop(self) -> None:
        while not self._stopping.wait(TICK_SECONDS):
            batch = self._collect_settled()
            if batch:
                self._sync(batch)

    def _collect_settled(self) -> set[str]:
        now = time.monotonic()
        with self._lock:
            due = [(rel, pending) for rel, pending in self._pending.items() if pending.due <= now]
        settled: dict[str, _Pending] = {}
        for rel, pending in due:
            if pending.is_directory:
                self._expand_directory(rel, pending)
            elif self._is_settled(rel, pending, now):
                settled[rel] = pending
        with self._lock:
            for rel, pending in settled.items():
                self._forget(rel, pending)
        return set(settled)

    def _forget(self, rel: str, pending: _Pending) -> None:
        """Drops the entry unless a newer event replaced it meanwhile. Caller holds the lock."""
        if self._pending.get(rel) is pending:
            del self._pending[rel]

    def _expand_directory(self, rel: str, pending: _Pending) -> None:
        contents = paths_under(self._config, rel)
        with self._lock:
            self._forget(rel, pending)
            for path in contents:
                self._pending.setdefault(path, _Pending(0.0, False))

    def _is_settled(self, rel: str, pending: _Pending, now: float) -> bool:
        """Two consecutive checks must see the same size and mtime (or the same absence)."""
        signature = self._signature(self._config.root / rel)
        if pending.checked and pending.signature == signature:
            return True
        pending.checked, pending.signature = True, signature
        pending.due = now + self._timing.recheck
        return False

    @staticmethod
    def _signature(file: Path) -> tuple[int, int] | None:
        try:
            stat = file.stat()
        except OSError:
            return None
        return stat.st_size, stat.st_mtime_ns

    def _sync(self, batch: set[str]) -> None:
        try:
            report = sync_paths(self._config, batch, self._workers)
            log.info("Watcher synced %d path(s): %s", len(batch), report.summary())
        except Exception:  # a failed batch must not end watching
            log.exception("Watcher failed to sync %s", sorted(batch))


class _EventHandler(FileSystemEventHandler):
    def __init__(self, watcher: LibraryWatcher) -> None:
        self._watcher = watcher

    def on_any_event(self, event: FileSystemEvent) -> None:
        if event.event_type not in RELEVANT_EVENTS:
            return
        for path in (event.src_path, getattr(event, "dest_path", "")):
            if path:
                self._watcher.touch(str(path), event.is_directory)
