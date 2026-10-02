from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import time
from pathlib import Path

from .config import INDEX_DIRNAME, Config
from .db import connect
from .extract_pdf import EncryptedPDF, extract_pdf

log = logging.getLogger("plib.indexer")


def walk_library(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and d != INDEX_DIRNAME]
        for name in filenames:
            if name.startswith(".") or not name.lower().endswith(".pdf"):
                continue
            yield Path(dirpath) / name


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def index_library(config: Config) -> None:
    conn = connect(config.db_path)
    try:
        seen: set[str] = set()
        for file in walk_library(config.root):
            rel = file.relative_to(config.root).as_posix()
            seen.add(rel)
            _index_file(conn, file, rel)
        existing = [r["path"] for r in conn.execute("SELECT path FROM documents")]
        with conn:
            for rel in existing:
                if rel not in seen:
                    conn.execute("DELETE FROM documents WHERE path = ?", (rel,))
    finally:
        conn.close()


def _index_file(conn: sqlite3.Connection, file: Path, rel: str) -> None:
    stat = file.stat()
    row = conn.execute(
        "SELECT id, size, mtime, sha256 FROM documents WHERE path = ?", (rel,)
    ).fetchone()
    if row and row["size"] == stat.st_size and row["mtime"] == stat.st_mtime:
        return
    digest = sha256_of(file)
    if row and row["sha256"] == digest:
        with conn:
            conn.execute(
                "UPDATE documents SET size = ?, mtime = ? WHERE id = ?",
                (stat.st_size, stat.st_mtime, row["id"]),
            )
        return

    status, error, extracted = "ok", None, None
    try:
        extracted = extract_pdf(file)
    except EncryptedPDF as e:
        status, error = "encrypted", str(e)
    except Exception as e:  # corrupt or unreadable file must not stop indexing
        status, error = "error", f"{type(e).__name__}: {e}"
        log.warning("Failed to index %s: %s", rel, error)

    with conn:
        if row:
            conn.execute("DELETE FROM documents WHERE id = ?", (row["id"],))
        cur = conn.execute(
            """INSERT INTO documents
               (path, kind, size, mtime, sha256, title, author, unit_count,
                meta_json, status, error, indexed_at)
               VALUES (?, 'pdf', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                rel,
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
            return
        doc_id = cur.lastrowid
        conn.executemany(
            """INSERT INTO units
               (doc_id, unit_no, label, heading_path, text, char_count, image_count, low_text)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (doc_id, u.unit_no, u.label, u.heading_path, u.text, len(u.text),
                 u.image_count, int(u.low_text))
                for u in extracted.units
            ],
        )
        conn.executemany(
            "INSERT INTO outline (doc_id, ord, level, title, unit_no) VALUES (?, ?, ?, ?, ?)",
            [(doc_id, i, e.level, e.title, e.unit_no) for i, e in enumerate(extracted.outline)],
        )
