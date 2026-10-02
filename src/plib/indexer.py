from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

import pathspec

from .config import INDEX_DIRNAME, Config
from .db import connect
from .extract_md import extract_md
from .render import invalidate_renders
from .extract_pdf import EncryptedPDF, extract_pdf

log = logging.getLogger("plib.indexer")


IGNORE_FILENAME = ".plibignore"


def load_ignore(root: Path) -> pathspec.GitIgnoreSpec:
    f = root / IGNORE_FILENAME
    lines = f.read_text(errors="replace").splitlines() if f.is_file() else []
    return pathspec.GitIgnoreSpec.from_lines(lines)


KINDS = {".pdf": "pdf", ".md": "md", ".markdown": "md"}


def document_kind(name: str) -> str | None:
    return KINDS.get(Path(name).suffix.lower())


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
            if name.startswith(".") or document_kind(name) is None:
                continue
            if ignore.match_file(prefix + name):
                continue
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

    def summary(self) -> str:
        return (f"indexed {self.indexed}, unchanged {self.unchanged}, "
                f"removed {self.removed}, failed {self.failed}")


def index_library(config: Config) -> IndexReport:
    report = IndexReport()
    conn = connect(config.db_path)
    try:
        seen: set[str] = set()
        for file in walk_library(config.root):
            rel = file.relative_to(config.root).as_posix()
            seen.add(rel)
            outcome = _index_file(conn, file, rel)
            if outcome in ("indexed", "failed"):
                invalidate_renders(config.index_dir, rel)
            setattr(report, outcome, getattr(report, outcome) + 1)
        existing = [r["path"] for r in conn.execute("SELECT path FROM documents")]
        with conn:
            for rel in existing:
                if rel not in seen:
                    conn.execute("DELETE FROM documents WHERE path = ?", (rel,))
                    invalidate_renders(config.index_dir, rel)
                    report.removed += 1
    finally:
        conn.close()
    return report


def _index_file(conn: sqlite3.Connection, file: Path, rel: str) -> str:
    stat = file.stat()
    row = conn.execute(
        "SELECT id, size, mtime, sha256 FROM documents WHERE path = ?", (rel,)
    ).fetchone()
    if row and row["size"] == stat.st_size and row["mtime"] == stat.st_mtime:
        return "unchanged"
    digest = sha256_of(file)
    if row and row["sha256"] == digest:
        with conn:
            conn.execute(
                "UPDATE documents SET size = ?, mtime = ? WHERE id = ?",
                (stat.st_size, stat.st_mtime, row["id"]),
            )
        return "unchanged"

    kind = document_kind(file.name)
    status, error, extracted = "ok", None, None
    try:
        extracted = extract_md(file) if kind == "md" else extract_pdf(file)
    except EncryptedPDF as e:
        status, error = "encrypted", str(e)
    except Exception as e:  # corrupt or unreadable file must not stop indexing
        status, error = "error", f"{type(e).__name__}: {str(e).replace(str(file), rel)}"
        log.warning("Failed to index %s: %s", rel, error)

    with conn:
        if row:
            conn.execute("DELETE FROM documents WHERE id = ?", (row["id"],))
        cur = conn.execute(
            """INSERT INTO documents
               (path, kind, size, mtime, sha256, title, author, unit_count,
                meta_json, status, error, indexed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                rel,
                kind,
                stat.st_size,
                stat.st_mtime,
                digest,
                (extracted.title if extracted and extracted.title else Path(rel).stem),
                extracted.author if extracted else None,
                len(extracted.units) if extracted else 0,
                json.dumps(extracted.meta) if extracted else None,
                status,
                error,
                time.time(),
            ),
        )
        if extracted is None:
            return "failed"
        doc_id = cur.lastrowid
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
    return "indexed"


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
