from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from .errors import QueryError
from .pages import resolve_spans, span_filter_sql
from .query import resolve_doc

LOW_TEXT_MARKER = "[low text — consider render_page]"
MIN_MAX_CHARS = 200
UNIT_SEPARATOR = "\n\n"


@dataclass
class ReadResult:
    text: str
    next_cursor: str | None


@dataclass
class _Position:
    unit_no: int
    offset: int


def _parse_cursor(cursor: str) -> _Position:
    unit_text, _, offset_text = cursor.partition(":")
    if not (unit_text.isdigit() and offset_text.isdigit()):
        raise QueryError(
            f"Invalid cursor {cursor!r}. Pass the next_cursor value from the previous "
            "read result, unchanged, with the same doc/from/to."
        )
    return _Position(int(unit_text), int(offset_text))


def _header(doc: sqlite3.Row, unit: sqlite3.Row, continued: bool) -> str:
    if doc["kind"] == "pdf":
        label = f" (label {unit['label']})" if unit["label"] else ""
        head = f"page {unit['unit_no']}{label}"
    else:
        head = f"section {unit['unit_no']}"
    line = head + (f" — {unit['heading_path']}" if unit["heading_path"] else "")
    if unit["line_start"] is not None:
        line += f" (lines {unit['line_start']}-{unit['line_end']})"
    if unit["low_text"]:
        line += f" {LOW_TEXT_MARKER}"
    if continued:
        line += " (continued)"
    return f"=== {line} ==="


def read_units(
    conn: sqlite3.Connection,
    doc: str | int,
    pages: str,
    *,
    max_chars: int,
    cursor: str | None,
) -> ReadResult:
    doc_row = resolve_doc(conn, doc)
    if doc_row["status"] != "ok":
        raise QueryError(
            f"{doc_row['path']!r} could not be indexed ({doc_row['status']}: "
            f"{doc_row['error']}), so it has no text to read."
        )
    spans = resolve_spans(conn, doc_row, pages)
    clause, bounds = span_filter_sql("unit_no", spans)
    units = conn.execute(
        f"SELECT * FROM units WHERE doc_id = ? AND {clause} ORDER BY unit_no",
        [doc_row["id"], *bounds],
    ).fetchall()
    position = _parse_cursor(cursor) if cursor else _Position(units[0]["unit_no"], 0)
    if cursor and position.unit_no not in {u["unit_no"] for u in units}:
        raise QueryError(
            f"Cursor {cursor!r} does not belong to this range. Reuse the same doc/from/to "
            "that produced it, or start again without a cursor."
        )
    return _render(doc_row, [u for u in units if u["unit_no"] >= position.unit_no],
                   position.offset, max(max_chars, MIN_MAX_CHARS))


def _render(doc_row: sqlite3.Row, units: list[sqlite3.Row], first_offset: int,
            max_chars: int) -> ReadResult:
    pieces: list[str] = []
    used = 0
    for index, unit in enumerate(units):
        offset = first_offset if index == 0 else 0
        body = unit["text"][offset:] if unit["text"].strip() else "(no text)"
        header = _header(doc_row, unit, continued=offset > 0)
        room = max_chars - used - len(header) - 1 - (len(UNIT_SEPARATOR) if pieces else 0)
        if room <= 0 and pieces:
            return ReadResult(UNIT_SEPARATOR.join(pieces), f"{unit['unit_no']}:{offset}")
        room = max(room, 1)
        if len(body) > room:
            pieces.append(f"{header}\n{body[:room]}")
            return ReadResult(UNIT_SEPARATOR.join(pieces), f"{unit['unit_no']}:{offset + room}")
        pieces.append(f"{header}\n{body}")
        used += len(pieces[-1]) + len(UNIT_SEPARATOR)
    return ReadResult(UNIT_SEPARATOR.join(pieces), None)
