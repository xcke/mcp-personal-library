from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from .query import resolve_doc


@dataclass
class DocumentSummary:
    id: int
    path: str
    kind: str
    title: str
    unit_count: int
    status: str
    error: str | None


@dataclass
class DocumentListing:
    documents: list[DocumentSummary]
    total: int
    offset: int


@dataclass
class DocumentInfo:
    summary: DocumentSummary
    author: str | None
    metadata: dict
    page_labels: list[tuple[int, int, str, str]]  # (first unit, last unit, first label, last label)
    low_text_units: list[int]


@dataclass
class OutlineItem:
    level: int
    title: str
    unit: int | None


def _summary(row: sqlite3.Row) -> DocumentSummary:
    return DocumentSummary(row["id"], row["path"], row["kind"], row["title"] or row["path"],
                           row["unit_count"], row["status"], row["error"])


def list_documents(conn: sqlite3.Connection, *, glob: str | None, kind: str | None,
                   limit: int, offset: int) -> DocumentListing:
    where, params = ["1=1"], []
    if glob:
        where.append("path GLOB ?")
        params.append(glob)
    if kind:
        where.append("kind = ?")
        params.append(kind)
    clause = " AND ".join(where)
    total = conn.execute(f"SELECT COUNT(*) FROM documents WHERE {clause}", params).fetchone()[0]
    rows = conn.execute(
        f"SELECT * FROM documents WHERE {clause} ORDER BY path LIMIT ? OFFSET ?",
        [*params, max(1, min(limit, 500)), max(0, offset)],
    ).fetchall()
    return DocumentListing([_summary(r) for r in rows], total, max(0, offset))


def doc_info(conn: sqlite3.Connection, doc: str | int) -> DocumentInfo:
    row = resolve_doc(conn, doc)
    units = conn.execute(
        "SELECT unit_no, label, low_text FROM units WHERE doc_id = ? ORDER BY unit_no",
        (row["id"],),
    ).fetchall()
    return DocumentInfo(
        summary=_summary(row),
        author=row["author"],
        metadata=json.loads(row["meta_json"]) if row["meta_json"] else {},
        page_labels=_label_runs(units) if row["kind"] == "pdf" else [],
        low_text_units=[u["unit_no"] for u in units if u["low_text"]],
    )


def _label_shape(label: str | None) -> str:
    if not label:
        return "none"
    if label.isdigit():
        return "arabic"
    if label.isalpha() and label.islower() and set(label) <= set("ivxlcdm"):
        return "roman-lower"
    if label.isalpha() and label.isupper() and set(label) <= set("IVXLCDM"):
        return "roman-upper"
    return "other"


def _label_runs(units: list[sqlite3.Row]) -> list[tuple[int, int, str, str]]:
    """Consecutive pages sharing a label shape, as (first page, last page, first label, last label)."""
    runs: list[tuple[int, int, str, str]] = []
    previous_shape = None
    for unit in units:
        shape = _label_shape(unit["label"])
        label = unit["label"] or ""
        if runs and shape == previous_shape:
            first, _, first_label, _ = runs[-1]
            runs[-1] = (first, unit["unit_no"], first_label, label)
        else:
            runs.append((unit["unit_no"], unit["unit_no"], label, label))
        previous_shape = shape
    return runs


def get_outline(conn: sqlite3.Connection, doc: str | int, max_level: int | None) -> list[OutlineItem]:
    row = resolve_doc(conn, doc)
    rows = conn.execute(
        "SELECT level, title, unit_no FROM outline WHERE doc_id = ? ORDER BY ord", (row["id"],)
    ).fetchall()
    return [OutlineItem(r["level"], r["title"], r["unit_no"])
            for r in rows if max_level is None or r["level"] <= max_level]
