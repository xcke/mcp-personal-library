from __future__ import annotations

import sqlite3
from dataclasses import dataclass

HIT_OPEN, HIT_CLOSE = "[[", "]]"


class QueryError(Exception):
    """A problem with the caller's input, phrased so an agent can correct it."""


@dataclass
class Hit:
    doc: str
    unit: int
    label: str | None
    title: str
    snippet: str
    low_text: bool
    score: float


def resolve_doc(conn: sqlite3.Connection, doc: str | int) -> sqlite3.Row:
    if isinstance(doc, int) or str(doc).isdigit():
        row = conn.execute("SELECT * FROM documents WHERE id = ?", (int(doc),)).fetchone()
    else:
        row = conn.execute("SELECT * FROM documents WHERE path = ?", (str(doc),)).fetchone()
    if row is None:
        raise QueryError(
            f"Unknown document {doc!r}. Use a path relative to the library root "
            "(as shown in search results) or a numeric document id."
        )
    return row


def search(
    conn: sqlite3.Connection,
    query: str,
    *,
    path_glob: str | None = None,
    doc: str | int | None = None,
    limit: int = 20,
) -> list[Hit]:
    if not query.strip():
        raise QueryError("Empty query. Provide search terms, e.g. 'configuring AND server'.")
    where, params = ["units_fts MATCH ?"], [query]
    if doc is not None:
        where.append("d.id = ?")
        params.append(resolve_doc(conn, doc)["id"])
    if path_glob:
        where.append("d.path GLOB ?")
        params.append(path_glob)
    sql = f"""
        SELECT d.path, u.unit_no, u.label, d.title, u.low_text,
               snippet(units_fts, 0, '{HIT_OPEN}', '{HIT_CLOSE}', '…', 24) AS snippet,
               bm25(units_fts) AS score
        FROM units_fts
        JOIN units u ON u.id = units_fts.rowid
        JOIN documents d ON d.id = u.doc_id
        WHERE {' AND '.join(where)}
        ORDER BY score
        LIMIT ?
    """
    params.append(max(1, min(limit, 100)))
    try:
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError as e:
        raise QueryError(
            f"Invalid search query ({e}). Queries use FTS5 syntax: bare words, "
            "\"exact phrases\", AND / OR / NOT, and prefix* terms. Quote terms "
            "containing punctuation, e.g. \"foo-bar\"."
        ) from e
    return [
        Hit(r["path"], r["unit_no"], r["label"], r["title"], " ".join(r["snippet"].split()),
            bool(r["low_text"]), r["score"])
        for r in rows
    ]
