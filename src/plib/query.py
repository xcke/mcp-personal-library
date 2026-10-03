from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass

from .errors import QueryError
from .pages import Span, resolve_spans, physical_spans, is_label_spec, span_filter_sql

HIT_OPEN, HIT_CLOSE = "[[", "]]"

# A complete "quoted phrase" is kept as-is; an unterminated quote matches neither
# alternative's quote handling and is left for FTS5 to reject.
_QUOTED_OR_HYPHENATED = re.compile(r'"(?:[^"]|"")*"|\b\w+(?:-\w+)+(\*?)')


def quote_hyphenated_terms(query: str) -> str:
    """Turn bare `FG-80F` / `leaf-0*` into phrases so FTS5 doesn't read `-` as an operator."""
    def quote(match: re.Match[str]) -> str:
        if match.group(0).startswith('"'):
            return match.group(0)
        prefix_star = match.group(1)
        return f'"{match.group(0).removesuffix(prefix_star)}"{prefix_star}'
    return _QUOTED_OR_HYPHENATED.sub(quote, query)


@dataclass
class Hit:
    doc: str
    unit: int
    label: str | None
    title: str
    snippet: str
    low_text: bool
    heading_path: str
    line_start: int | None
    line_end: int | None
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


def unit_filters(
    conn: sqlite3.Connection,
    *,
    doc: str | int | None,
    path_glob: str | None,
    pages: str | None,
) -> tuple[list[str], list[str | int]]:
    """SQL conditions (over aliases d=documents, u=units) shared by search and grep."""
    where: list[str] = []
    params: list[str | int] = []
    doc_row = resolve_doc(conn, doc) if doc is not None else None
    if doc_row is not None:
        where.append("d.id = ?")
        params.append(doc_row["id"])
    if pages:
        clause, bounds = span_filter_sql("u.unit_no", _search_spans(conn, doc_row, pages))
        where.append(clause)
        params.extend(bounds)
    if path_glob:
        where.append("d.path GLOB ?")
        params.append(path_glob)
    return where, params


def search(
    conn: sqlite3.Connection,
    query: str,
    *,
    path_glob: str | None = None,
    doc: str | int | None = None,
    pages: str | None = None,
    limit: int = 20,
) -> list[Hit]:
    if not query.strip():
        raise QueryError("Empty query. Provide search terms, e.g. 'configuring AND server'.")
    filters, filter_params = unit_filters(conn, doc=doc, path_glob=path_glob, pages=pages)
    where = ["units_fts MATCH ?", *filters]
    params: list[str | int] = [quote_hyphenated_terms(query), *filter_params]
    sql = f"""
        SELECT d.path, u.unit_no, u.label, d.title, u.low_text,
               u.heading_path, u.line_start, u.line_end,
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
            "containing other punctuation, e.g. \"foo.bar\"."
        ) from e
    return [
        Hit(r["path"], r["unit_no"], r["label"], r["title"], " ".join(r["snippet"].split()),
            bool(r["low_text"]), r["heading_path"], r["line_start"], r["line_end"],
            r["score"])
        for r in rows
    ]


def _search_spans(conn: sqlite3.Connection, doc_row: sqlite3.Row | None, pages: str) -> list[Span]:
    if doc_row is not None:
        return resolve_spans(conn, doc_row, pages)
    if is_label_spec(pages):
        raise QueryError(
            "Printed-label ranges differ per document. Pass doc= together with "
            "pages='label:...', or use physical page numbers."
        )
    return physical_spans(pages)
