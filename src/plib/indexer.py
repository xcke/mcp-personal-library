from __future__ import annotations

import hashlib
import json
import logging
import multiprocessing
import os
import sqlite3
import time
from collections import deque
from concurrent.futures import FIRST_COMPLETED, Future, ProcessPoolExecutor, wait
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass
from pathlib import Path

import pathspec

from .config import INDEX_DIRNAME, Config
from .db import connect
from .extract_md import extract_md
from .extract_pdf import EncryptedPDF, extract_pdf
from .models import DocumentStatus, Extracted, IndexOutcome, IndexState
from .progress import IndexProgress
from .render import invalidate_renders

log = logging.getLogger("plib.indexer")


IGNORE_FILENAME = ".plibignore"


def load_ignore(root: Path) -> pathspec.GitIgnoreSpec:
    f = root / IGNORE_FILENAME
    lines = f.read_text(errors="replace").splitlines() if f.is_file() else []
    return pathspec.GitIgnoreSpec.from_lines(lines)


KINDS = {".pdf": "pdf", ".md": "md", ".markdown": "md"}


def document_kind(name: str) -> str | None:
    return KINDS.get(Path(name).suffix.lower())


def is_indexable(rel: str, ignore: pathspec.GitIgnoreSpec) -> bool:
    parts = rel.split("/")
    if any(part.startswith(".") for part in parts) or document_kind(parts[-1]) is None:
        return False
    return not ignore.match_file(rel)


def walk_library(root: Path):
    ignore = load_ignore(root)
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = Path(dirpath).relative_to(root).as_posix()
        prefix = "" if rel_dir == "." else rel_dir + "/"
        dirnames[:] = [
            d for d in dirnames
            if not d.startswith(".") and not ignore.match_file(f"{prefix}{d}/")
        ]
        for name in filenames:
            if is_indexable(prefix + name, ignore):
                yield Path(dirpath) / name


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


@dataclass
class IndexReport:
    indexed: int = 0
    unchanged: int = 0
    removed: int = 0
    failed: int = 0

    def record(self, outcome: IndexOutcome) -> None:
        match outcome:
            case IndexOutcome.INDEXED:
                self.indexed += 1
            case IndexOutcome.UNCHANGED:
                self.unchanged += 1
            case IndexOutcome.FAILED:
                self.failed += 1

    def summary(self) -> str:
        return (f"indexed {self.indexed}, unchanged {self.unchanged}, "
                f"removed {self.removed}, failed {self.failed}")


@dataclass
class Extraction:
    """What a worker process learned about one file; the writer thread stores it."""
    rel: str
    kind: str | None
    size: int
    mtime: float
    digest: str
    status: DocumentStatus
    error: str | None
    extracted: Extracted | None
    content_unchanged: bool = False


def default_workers() -> int:
    return max(1, (os.cpu_count() or 2) - 1)


def extract_document(root: str, rel: str, known_sha256: str | None) -> Extraction:
    """Runs in a worker process: pure with respect to the database."""
    file = Path(root) / rel
    kind = document_kind(file.name)
    try:
        stat = file.stat()
        digest = sha256_of(file)
    except OSError as e:
        return Extraction(rel, kind, 0, 0.0, "", DocumentStatus.ERROR,
                          f"{type(e).__name__}: {e.strerror}", None)
    if digest == known_sha256:
        return Extraction(rel, kind, stat.st_size, stat.st_mtime, digest, DocumentStatus.OK, None, None,
                          content_unchanged=True)
    status, error, extracted = DocumentStatus.OK, None, None
    try:
        extracted = extract_md(file) if kind == "md" else extract_pdf(file)
    except EncryptedPDF as e:
        status, error = DocumentStatus.ENCRYPTED, str(e)
    except Exception as e:  # corrupt or unreadable file must not stop indexing
        status, error = DocumentStatus.ERROR, f"{type(e).__name__}: {str(e).replace(str(file), rel)}"
        log.warning("Failed to index %s: %s", rel, error)
    return Extraction(rel, kind, stat.st_size, stat.st_mtime, digest, status, error, extracted)


def index_library(config: Config, workers: int | None = None,
                  progress: IndexProgress | None = None) -> IndexReport:
    progress = progress or IndexProgress()
    report = IndexReport()
    conn = connect(config.db_path)
    try:
        progress.set_state(IndexState.SCANNING)
        seen, work = _scan(conn, config, report)
        progress.set_state(IndexState.INDEXING)
        progress.set_pending(len(work))
        if work:
            _extract_and_store(conn, config, work, workers or default_workers(), progress, report)
        existing = [r["path"] for r in conn.execute("SELECT path FROM documents")]
        with conn:
            for rel in existing:
                if rel not in seen:
                    conn.execute("DELETE FROM documents WHERE path = ?", (rel,))
                    invalidate_renders(config.index_dir, rel)
                    report.removed += 1
    finally:
        conn.close()
        progress.finish()
    return report


def paths_under(config: Config, rel_dir: str) -> set[str]:
    """Every path inside a directory that is on disk or still indexed; used when a folder changes."""
    prefix = rel_dir.rstrip("/") + "/"
    conn = connect(config.db_path)
    try:
        indexed = {r["path"] for r in conn.execute(
            "SELECT path FROM documents WHERE substr(path, 1, ?) = ?", (len(prefix), prefix))}
    finally:
        conn.close()
    on_disk = {f.relative_to(config.root).as_posix()
               for f in walk_library(config.root) if f.relative_to(config.root).as_posix().startswith(prefix)}
    return indexed | on_disk


def sync_paths(config: Config, rels: set[str], workers: int | None = None) -> IndexReport:
    """Brings the given paths up to date: indexes new or changed files, drops vanished ones,
    and carries an existing document over to a new path when the content hash matches."""
    report = IndexReport()
    ignore = load_ignore(config.root)
    present = {r for r in rels if is_indexable(r, ignore) and (config.root / r).is_file()}
    conn = connect(config.db_path)
    try:
        known = {r["path"]: r for r in conn.execute("SELECT path, size, mtime, sha256 FROM documents")}
        work = _changed_files(conn, config, sorted(present), known, report)
        if work:
            _extract_and_store(conn, config, work, workers or default_workers(), IndexProgress(), report)
        with conn:
            for rel in rels - present:
                if rel in known:
                    conn.execute("DELETE FROM documents WHERE path = ?", (rel,))
                    invalidate_renders(config.index_dir, rel)
                    report.removed += 1
    finally:
        conn.close()
    return report


def _changed_files(conn: sqlite3.Connection, config: Config, present: list[str],
                   known: dict[str, sqlite3.Row], report: IndexReport) -> list[tuple[str, str | None]]:
    work: list[tuple[str, str | None]] = []
    for rel in present:
        row, stat = known.get(rel), (config.root / rel).stat()
        if row and row["size"] == stat.st_size and row["mtime"] == stat.st_mtime:
            report.unchanged += 1
        elif row is None and _adopt_moved_document(conn, config, rel, stat, known):
            report.unchanged += 1
        else:
            work.append((rel, row["sha256"] if row else None))
    return work


def _adopt_moved_document(conn: sqlite3.Connection, config: Config, rel: str,
                          stat: os.stat_result, known: dict[str, sqlite3.Row]) -> bool:
    """A new path whose content matches an indexed file that left disk is that file, renamed."""
    digest = sha256_of(config.root / rel)
    for old_rel, row in known.items():
        if row["sha256"] == digest and not (config.root / old_rel).exists():
            with conn:
                conn.execute("UPDATE documents SET path = ?, size = ?, mtime = ? WHERE path = ?",
                             (rel, stat.st_size, stat.st_mtime, old_rel))
            invalidate_renders(config.index_dir, old_rel)
            del known[old_rel]
            return True
    return False


def _scan(conn: sqlite3.Connection, config: Config,
          report: IndexReport) -> tuple[set[str], list[tuple[str, str | None]]]:
    """Cheap stat-only pass: returns every path on disk and the files needing extraction."""
    known = {r["path"]: r for r in conn.execute("SELECT path, size, mtime, sha256 FROM documents")}
    seen: set[str] = set()
    work: list[tuple[str, str | None]] = []
    for file in walk_library(config.root):
        rel = file.relative_to(config.root).as_posix()
        seen.add(rel)
        row = known.get(rel)
        stat = file.stat()
        if row and row["size"] == stat.st_size and row["mtime"] == stat.st_mtime:
            report.unchanged += 1
        else:
            work.append((rel, row["sha256"] if row else None))
    return seen, work


def _extract_and_store(conn: sqlite3.Connection, config: Config,
                       work: list[tuple[str, str | None]], workers: int,
                       progress: IndexProgress, report: IndexReport) -> None:
    queue = deque(work)
    while queue:
        suspects = _drain_with_pool(conn, config, queue, min(workers, len(queue)), progress,
                                    report)
        for item in suspects:
            _extract_in_isolation(conn, config, item, report)
        progress.set_pending(len(queue))


def _drain_with_pool(conn: sqlite3.Connection, config: Config,
                     queue: deque[tuple[str, str | None]], pool_size: int,
                     progress: IndexProgress, report: IndexReport) -> list[tuple[str, str | None]]:
    """Extracts queued files until done; returns the in-flight files if a worker process died."""
    in_flight: dict[Future[Extraction], tuple[str, str | None]] = {}
    # spawn: forking a process that already runs server threads can deadlock.
    with ProcessPoolExecutor(pool_size, mp_context=multiprocessing.get_context("spawn")) as pool:
        def fill() -> None:
            while len(in_flight) < pool_size * 2 and queue:
                item = queue.popleft()
                in_flight[pool.submit(extract_document, str(config.root), *item)] = item
            first = next(iter(in_flight.values()), None)
            progress.set_current(first[0] if first else None)

        fill()
        while in_flight:
            finished, _ = wait(in_flight, return_when=FIRST_COMPLETED)
            for future in finished:
                try:
                    extraction = future.result()
                except BrokenProcessPool:
                    return list(in_flight.values())
                del in_flight[future]
                report.record(_store(conn, config, extraction))
            progress.set_pending(len(queue) + len(in_flight))
            fill()
    return []


def _extract_in_isolation(conn: sqlite3.Connection, config: Config,
                          item: tuple[str, str | None], report: IndexReport) -> None:
    """Re-runs one file of a crashed batch alone, so the culprit is recorded as failed."""
    rel, _ = item
    with ProcessPoolExecutor(1, mp_context=multiprocessing.get_context("spawn")) as pool:
        try:
            extraction = pool.submit(extract_document, str(config.root), *item).result()
        except BrokenProcessPool:
            log.warning("Extraction process crashed on %s", rel)
            stat = (config.root / rel).stat()
            extraction = Extraction(rel, document_kind(rel), stat.st_size, stat.st_mtime,
                                    sha256_of(config.root / rel), DocumentStatus.ERROR,
                                    "extraction process crashed on this file", None)
    report.record(_store(conn, config, extraction))


def _store(conn: sqlite3.Connection, config: Config, extraction: Extraction) -> IndexOutcome:
    """Single-writer step: one document, one transaction."""
    row = conn.execute("SELECT id FROM documents WHERE path = ?", (extraction.rel,)).fetchone()
    if extraction.content_unchanged:
        with conn:
            conn.execute("UPDATE documents SET size = ?, mtime = ? WHERE id = ?",
                         (extraction.size, extraction.mtime, row["id"]))
        return IndexOutcome.UNCHANGED
    extracted = extraction.extracted
    with conn:
        if row:
            conn.execute("DELETE FROM documents WHERE id = ?", (row["id"],))
        cur = conn.execute(
            """INSERT INTO documents
               (path, kind, size, mtime, sha256, title, author, unit_count,
                meta_json, status, error, indexed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                extraction.rel,
                extraction.kind,
                extraction.size,
                extraction.mtime,
                extraction.digest,
                (extracted.title if extracted and extracted.title else Path(extraction.rel).stem),
                extracted.author if extracted else None,
                len(extracted.units) if extracted else 0,
                json.dumps(extracted.meta) if extracted else None,
                extraction.status.value,
                extraction.error,
                time.time(),
            ),
        )
        if extracted is not None:
            _store_units(conn, cur.lastrowid, extracted)
    invalidate_renders(config.index_dir, extraction.rel)
    return IndexOutcome.INDEXED if extracted is not None else IndexOutcome.FAILED


def _store_units(conn: sqlite3.Connection, doc_id: int, extracted: Extracted) -> None:
    conn.executemany(
        """INSERT INTO units
           (doc_id, unit_no, label, heading_path, line_start, line_end, text,
            char_count, image_count, low_text)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            (doc_id, u.unit_no, u.label, u.heading_path, u.line_start, u.line_end,
             u.text, len(u.text), u.image_count, int(u.low_text))
            for u in extracted.units
        ],
    )
    conn.executemany(
        "INSERT INTO outline (doc_id, ord, level, title, unit_no) VALUES (?, ?, ?, ?, ?)",
        [(doc_id, i, e.level, e.title, e.unit_no) for i, e in enumerate(extracted.outline)],
    )


def library_status(config: Config) -> dict:
    """Read-only snapshot; never creates the index."""
    known: dict[str, sqlite3.Row] = {}
    failed: list[tuple[str, str]] = []
    if config.db_path.exists():
        conn = sqlite3.connect(f"file:{config.db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            for r in conn.execute("SELECT path, size, mtime, status, error FROM documents"):
                known[r["path"]] = r
                if r["status"] != "ok":
                    failed.append((r["path"], r["error"] or r["status"]))
        finally:
            conn.close()
    pending, on_disk = 0, set()
    for file in walk_library(config.root):
        rel = file.relative_to(config.root).as_posix()
        on_disk.add(rel)
        st = file.stat()
        row = known.get(rel)
        if row is None or row["size"] != st.st_size or row["mtime"] != st.st_mtime:
            pending += 1
    stale = [p for p in known if p not in on_disk]
    return {
        "documents": len(known),
        "indexed": sum(1 for r in known.values() if r["status"] == "ok"),
        "failed": sorted(failed),
        "pending": pending,
        "removed_on_disk": len(stale),
    }
