from __future__ import annotations

import re
import sqlite3

from .errors import QueryError

LABEL_PREFIX = "label:"
PHYSICAL_ITEM = re.compile(r"^(\d+)(?:\s*-\s*(\d+))?$")

# Inclusive (first, last) physical unit numbers.
Span = tuple[int, int]

RANGE_SYNTAX_HINT = "Use physical numbers like '10-20,35' or printed labels like 'label:xii-xv'."


def is_label_spec(spec: str) -> bool:
    return spec.strip().lower().startswith(LABEL_PREFIX)


def physical_spans(spec: str) -> list[Span]:
    """Parses '10-20,35' without knowing the document (no upper-bound check)."""
    spans = []
    for item in spec.split(","):
        match = PHYSICAL_ITEM.match(item.strip())
        if not match:
            raise QueryError(f"Invalid page range {item.strip()!r} in {spec!r}. {RANGE_SYNTAX_HINT}")
        first = int(match.group(1))
        last = int(match.group(2) or first)
        if first < 1 or last < first:
            raise QueryError(
                f"Invalid page range {item.strip()!r}: pages start at 1 and ranges must "
                f"ascend. {RANGE_SYNTAX_HINT}"
            )
        spans.append((first, last))
    return spans


def resolve_spans(conn: sqlite3.Connection, doc: sqlite3.Row, spec: str) -> list[Span]:
    """Resolves a range spec against one document, validating it exists there."""
    if is_label_spec(spec):
        if doc["kind"] != "pdf":
            raise QueryError(
                f"{doc['path']!r} is Markdown; printed labels exist only for PDFs. "
                "Address its section numbers instead, e.g. '3' or '2-4' (see get_outline)."
            )
        spans = _label_spans(conn, doc, spec.strip()[len(LABEL_PREFIX):])
    else:
        spans = physical_spans(spec)
    noun = "pages" if doc["kind"] == "pdf" else "sections"
    for first, last in spans:
        if last > doc["unit_count"]:
            raise QueryError(
                f"{noun.capitalize()} {first}-{last} out of range: {doc['path']!r} has "
                f"{doc['unit_count']} {noun} (valid: 1-{doc['unit_count']})."
            )
    return spans


def _label_spans(conn: sqlite3.Connection, doc: sqlite3.Row, body: str) -> list[Span]:
    first_unit_by_label: dict[str, int] = {}
    for row in conn.execute(
        "SELECT unit_no, label FROM units WHERE doc_id = ? AND label IS NOT NULL "
        "ORDER BY unit_no", (doc["id"],)
    ):
        first_unit_by_label.setdefault(row["label"], row["unit_no"])

    def unknown(label: str) -> QueryError:
        return QueryError(
            f"Unknown page label {label!r} (or label range) in {doc['path']!r}. See doc_info for its "
            "printed-label ranges, or use physical page numbers."
        )

    spans = []
    for item in (part.strip() for part in body.split(",")):
        if item in first_unit_by_label:
            unit = first_unit_by_label[item]
            spans.append((unit, unit))
            continue
        # Labels may themselves contain '-', so try every split point.
        for index in (i for i, ch in enumerate(item) if ch == "-"):
            start_label, end_label = item[:index].strip(), item[index + 1:].strip()
            if start_label in first_unit_by_label and end_label in first_unit_by_label:
                start = first_unit_by_label[start_label]
                end = first_unit_by_label[end_label]
                if end < start:
                    raise QueryError(
                        f"Label range {item!r} runs backwards (page {start} to page {end}). "
                        "Put the earlier label first."
                    )
                spans.append((start, end))
                break
        else:
            raise unknown(item)
    return spans


def span_filter_sql(column: str, spans: list[Span]) -> tuple[str, list[int]]:
    clause = " OR ".join(f"{column} BETWEEN ? AND ?" for _ in spans)
    return f"({clause})", [bound for span in spans for bound in span]
