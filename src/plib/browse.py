from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from enum import Enum

from .errors import QueryError
from .query import resolve_doc

DOCUMENT_KINDS = ("pdf", "md")


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


class LabelShape(Enum):
    NONE = "none"
    ARABIC = "arabic"
    ROMAN_LOWER = "roman-lower"
    ROMAN_UPPER = "roman-upper"
    OTHER = "other"


@dataclass
class LabelRun:
    """Consecutive pages whose printed labels share a numbering style."""
    first_page: int
    last_page: int
    first_label: str
    last_label: str
    shape: LabelShape


@dataclass
class DocumentInfo:
    summary: DocumentSummary
    author: str | None
    metadata: dict
    page_labels: list[LabelRun]
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
    if kind and kind not in DOCUMENT_KINDS:
        raise QueryError(f"Unknown kind {kind!r}. Use one of: {', '.join(DOCUMENT_KINDS)}.")
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


def _label_shape(label: str | None) -> LabelShape:
    if not label:
        return LabelShape.NONE
    if label.isdigit():
        return LabelShape.ARABIC
    if label.isalpha() and label.islower() and set(label) <= set("ivxlcdm"):
        return LabelShape.ROMAN_LOWER
    if label.isalpha() and label.isupper() and set(label) <= set("IVXLCDM"):
        return LabelShape.ROMAN_UPPER
    return LabelShape.OTHER


def _label_runs(units: list[sqlite3.Row]) -> list[LabelRun]:
    runs: list[LabelRun] = []
    for unit in units:
        shape = _label_shape(unit["label"])
        label = unit["label"] or ""
        if runs and runs[-1].shape == shape:
            runs[-1].last_page = unit["unit_no"]
            runs[-1].last_label = label
        else:
            runs.append(LabelRun(unit["unit_no"], unit["unit_no"], label, label, shape))
    return runs


def get_outline(conn: sqlite3.Connection, doc: str | int, max_level: int | None) -> list[OutlineItem]:
    row = resolve_doc(conn, doc)
    rows = conn.execute(
        "SELECT level, title, unit_no FROM outline WHERE doc_id = ? ORDER BY ord", (row["id"],)
    ).fetchall()
    return [OutlineItem(r["level"], r["title"], r["unit_no"])
            for r in rows if max_level is None or r["level"] <= max_level]


@dataclass
class FailedDocument:
    path: str
    reason: str


@dataclass
class IndexCounts:
    indexed: int
    failures: list[FailedDocument]

    @property
    def failed(self) -> int:
        return len(self.failures)


def index_counts(conn: sqlite3.Connection) -> IndexCounts:
    indexed = conn.execute("SELECT COUNT(*) FROM documents WHERE status = 'ok'").fetchone()[0]
    failures = [
        FailedDocument(r["path"], r["error"] or r["status"])
        for r in conn.execute(
            "SELECT path, status, error FROM documents WHERE status != 'ok' ORDER BY path")
    ]
    return IndexCounts(indexed, failures)
