from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY,
    path TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL CHECK (kind IN ('pdf', 'md')),
    size INTEGER NOT NULL,
    mtime REAL NOT NULL,
    sha256 TEXT NOT NULL,
    title TEXT,
    author TEXT,
    unit_count INTEGER NOT NULL DEFAULT 0,
    meta_json TEXT,
    status TEXT NOT NULL CHECK (status IN ('ok', 'error', 'encrypted')),
    error TEXT,
    indexed_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS units (
    id INTEGER PRIMARY KEY,
    doc_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    unit_no INTEGER NOT NULL,
    label TEXT,
    heading_path TEXT,
    line_start INTEGER,
    line_end INTEGER,
    text TEXT NOT NULL,
    char_count INTEGER NOT NULL,
    image_count INTEGER NOT NULL DEFAULT 0,
    low_text INTEGER NOT NULL DEFAULT 0,
    UNIQUE (doc_id, unit_no)
);

CREATE TABLE IF NOT EXISTS outline (
    doc_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    ord INTEGER NOT NULL,
    level INTEGER NOT NULL,
    title TEXT NOT NULL,
    unit_no INTEGER,
    PRIMARY KEY (doc_id, ord)
);

CREATE VIRTUAL TABLE IF NOT EXISTS units_fts USING fts5(
    text, content='units', content_rowid='id', tokenize='porter unicode61'
);

CREATE VIRTUAL TABLE IF NOT EXISTS units_tri USING fts5(
    text, content='units', content_rowid='id', tokenize='trigram'
);

CREATE TRIGGER IF NOT EXISTS units_ai AFTER INSERT ON units BEGIN
    INSERT INTO units_fts(rowid, text) VALUES (new.id, new.text);
    INSERT INTO units_tri(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS units_ad AFTER DELETE ON units BEGIN
    INSERT INTO units_fts(units_fts, rowid, text) VALUES ('delete', old.id, old.text);
    INSERT INTO units_tri(units_tri, rowid, text) VALUES ('delete', old.id, old.text);
END;
CREATE TRIGGER IF NOT EXISTS units_au AFTER UPDATE ON units BEGIN
    INSERT INTO units_fts(units_fts, rowid, text) VALUES ('delete', old.id, old.text);
    INSERT INTO units_tri(units_tri, rowid, text) VALUES ('delete', old.id, old.text);
    INSERT INTO units_fts(rowid, text) VALUES (new.id, new.text);
    INSERT INTO units_tri(rowid, text) VALUES (new.id, new.text);
END;
"""


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    had_trigram_index = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name = 'units_tri'").fetchone() is not None
    if not had_trigram_index:
        # Triggers from before grep existed don't feed the trigram index; replace them once.
        conn.executescript("DROP TRIGGER IF EXISTS units_ai; DROP TRIGGER IF EXISTS units_ad; "
                           "DROP TRIGGER IF EXISTS units_au;")
    conn.executescript(SCHEMA)
    if not had_trigram_index:
        conn.execute("INSERT INTO units_tri(units_tri) VALUES ('rebuild')")
        conn.commit()
    return conn
