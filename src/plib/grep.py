from __future__ import annotations

import re
import re._parser as regex_parser
import sqlite3
from dataclasses import dataclass, field

from .errors import QueryError
from .query import HIT_CLOSE, HIT_OPEN, unit_filters

MIN_TRIGRAM_LENGTH = 3
MAX_HITS_CEILING = 500


@dataclass
class GrepMatch:
    doc: str
    unit: int
    label: str | None
    low_text: bool
    heading_path: str
    line_start: int | None
    line_end: int | None
    excerpt: str


@dataclass
class GrepResult:
    matches: list[GrepMatch] = field(default_factory=list)
    hits_per_doc: dict[str, int] = field(default_factory=dict)

    @property
    def total_hits(self) -> int:
        return sum(self.hits_per_doc.values())


def required_literal(pattern: str) -> str | None:
    """Longest literal run every match must contain, or None if there is no safe one."""
    try:
        parsed = regex_parser.parse(pattern)
    except re.error:
        return None
    runs: list[str] = [""]
    for opcode, argument in parsed:
        if opcode is regex_parser.LITERAL:
            runs[-1] += chr(argument)
        elif opcode is regex_parser.BRANCH:
            return None
        else:
            runs.append("")
    best = max(runs, key=len)
    return best if len(best) >= MIN_TRIGRAM_LENGTH else None


def _compile(pattern: str, *, regex: bool, ignore_case: bool) -> re.Pattern[str]:
    if not pattern:
        raise QueryError("Empty pattern. Provide the text or regex to look for.")
    flags = re.IGNORECASE if ignore_case else 0
    try:
        return re.compile(pattern if regex else re.escape(pattern), flags)
    except re.error as e:
        raise QueryError(
            f"Invalid regex {pattern!r}: {e}. Patterns use Python `re` syntax; "
            "escape special characters with a backslash, or set regex=false for a literal search."
        ) from e


def _candidate_units(
    conn: sqlite3.Connection,
    prefilter: str | None,
    filters: list[str],
    params: list[str | int],
) -> sqlite3.Cursor:
    where = list(filters)
    values = list(params)
    source = "units u"
    if prefilter is not None:
        source = "units_tri JOIN units u ON u.id = units_tri.rowid"
        where.insert(0, "units_tri MATCH ?")
        values.insert(0, '"' + prefilter.replace('"', '""') + '"')
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    return conn.execute(
        f"""
        SELECT d.path, u.unit_no, u.label, u.low_text, u.heading_path,
               u.line_start, u.line_end, u.text
        FROM {source} JOIN documents d ON d.id = u.doc_id
        {clause}
        ORDER BY d.path, u.unit_no
        """,
        values,
    )


def _excerpt(text: str, match: re.Match[str], context: int) -> str:
    before = text[max(0, match.start() - context):match.start()]
    after = text[match.end():match.end() + context]
    return " ".join(f"{before}{HIT_OPEN}{match.group(0)}{HIT_CLOSE}{after}".split())


def grep(
    conn: sqlite3.Connection,
    pattern: str,
    *,
    regex: bool = False,
    ignore_case: bool = True,
    context: int = 200,
    path_glob: str | None = None,
    doc: str | int | None = None,
    pages: str | None = None,
    max_hits: int = 50,
) -> GrepResult:
    compiled = _compile(pattern, regex=regex, ignore_case=ignore_case)
    filters, params = unit_filters(conn, doc=doc, path_glob=path_glob, pages=pages)
    literal = required_literal(pattern) if regex else (
        pattern if len(pattern) >= MIN_TRIGRAM_LENGTH else None)
    keep = max(1, min(max_hits, MAX_HITS_CEILING))
    context = max(0, context)
    result = GrepResult()
    for row in _candidate_units(conn, literal, filters, params):
        for match in compiled.finditer(row["text"]):
            if match.end() == match.start():
                continue
            result.hits_per_doc[row["path"]] = result.hits_per_doc.get(row["path"], 0) + 1
            if len(result.matches) < keep:
                result.matches.append(GrepMatch(
                    row["path"], row["unit_no"], row["label"], bool(row["low_text"]),
                    row["heading_path"], row["line_start"], row["line_end"],
                    _excerpt(row["text"], match, context)))
    return result
