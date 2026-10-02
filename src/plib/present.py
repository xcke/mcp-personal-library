from __future__ import annotations

from .grep import GrepMatch, GrepResult
from .read import LOW_TEXT_MARKER
from .browse import (DocumentInfo, DocumentListing, DocumentSummary, LabelRun, LabelShape,
                     OutlineItem)

MAX_LISTED_LOW_TEXT_PAGES = 200


def _unit_noun(kind: str) -> str:
    return "pages" if kind == "pdf" else "sections"


def _within_budget(lines: list[str], budget: int) -> tuple[list[str], int]:
    """Keeps whole lines up to the budget; returns (kept lines, number dropped)."""
    kept, used = [], 0
    for line in lines:
        if used + len(line) + 1 > budget and kept:
            break
        kept.append(line)
        used += len(line) + 1
    return kept, len(lines) - len(kept)


def format_listing(listing: DocumentListing, budget: int) -> str:
    if not listing.documents:
        return "No documents match. Check glob/kind, or run `plib status` to see indexing progress."
    lines = [_listing_line(d) for d in listing.documents]
    kept, dropped = _within_budget(lines, budget)
    shown_end = listing.offset + len(kept)
    out = "\n".join(kept)
    if shown_end < listing.total:
        reason = "output budget reached; " if dropped else ""
        out += (f"\n\n[{listing.total - shown_end} more documents ({reason}{listing.total} total). "
                f"Continue with offset={shown_end}, or narrow with glob/kind.]")
    return out


def _listing_line(d: DocumentSummary) -> str:
    status = d.status if d.status == "ok" else f"{d.status}: {d.error}"
    return (f"id {d.id} | {d.path} | {d.kind} | {d.title} | "
            f"{d.unit_count} {_unit_noun(d.kind)} | {status}")


def _label_lines(runs: list[LabelRun]) -> list[str]:
    if all(run.shape is LabelShape.NONE for run in runs):
        return ["  none (printed labels equal physical numbers)"]
    lines = []
    for run in runs:
        pages = f"page {run.first_page}" if run.first_page == run.last_page else f"pages {run.first_page}-{run.last_page}"
        if run.shape is LabelShape.NONE:
            lines.append(f"  {pages} -> (no label)")
        elif run.first_page == run.last_page:
            lines.append(f"  {pages} -> {run.first_label}")
        else:
            lines.append(f"  {pages} -> {run.first_label}-{run.last_label}")
    return lines


def format_doc_info(info: DocumentInfo, budget: int) -> str:
    s = info.summary
    lines = [f"path: {s.path}", f"id: {s.id}", f"kind: {s.kind}", f"title: {s.title}"]
    if info.author:
        lines.append(f"author: {info.author}")
    lines.append(f"status: {s.status}" + (f" ({s.error})" if s.error else ""))
    lines.append(f"{_unit_noun(s.kind)}: {s.unit_count}")
    if s.kind == "pdf":
        lines.append("page labels (physical pages -> printed labels):")
        lines += _label_lines(info.page_labels)
        if info.low_text_units:
            shown = ", ".join(str(n) for n in info.low_text_units[:MAX_LISTED_LOW_TEXT_PAGES])
            extra = len(info.low_text_units) - MAX_LISTED_LOW_TEXT_PAGES
            lines.append(f"low-text pages: {shown}" + (f" (+{extra} more)" if extra > 0 else "")
                         + "  [text is unreliable here; use render_page]")
        else:
            lines.append("low-text pages: none")
    extra_meta = {k: v for k, v in info.metadata.items() if k not in ("title", "author")}
    if info.metadata.get("author") and not info.author:
        extra_meta["author"] = info.metadata["author"]
    if extra_meta:
        lines.append("metadata:")
        lines += [f"  {k}: {v}" for k, v in extra_meta.items()]
    kept, dropped = _within_budget(lines, budget)
    return "\n".join(kept) + (f"\n[truncated {dropped} lines]" if dropped else "")


def format_outline(items: list[OutlineItem], max_level: int | None, budget: int) -> str:
    if not items:
        return "No outline: this document has no bookmarks or headings" + (
            f" at level <= {max_level}." if max_level else ".")
    lines = []
    for item in items:
        unit = f"unit {item.unit}" if item.unit is not None else "unit ?"
        lines.append(f"{'  ' * (item.level - 1)}{item.title}  (level {item.level}, {unit})")
    kept, dropped = _within_budget(lines, budget)
    out = "\n".join(kept)
    if dropped:
        out += (f"\n\n[truncated: {dropped} more entries. Use max_level to show only "
                "top-level entries.]")
    return out


def format_locator(doc: str, unit: int, label: str | None, low_text: bool) -> str:
    return (f"locator {{doc: {doc!r}, unit: {unit}, label: {label or '-'!r}}}"
            + (f"  {LOW_TEXT_MARKER}" if low_text else ""))


def format_section_info(heading_path: str, line_start: int | None, line_end: int | None) -> str:
    parts = []
    if heading_path:
        parts.append(f"section: {heading_path}")
    if line_start is not None:
        parts.append(f"lines {line_start}-{line_end}")
    return f"\n   ({', '.join(parts)})" if parts else ""


def _match_entry(index: int, match: GrepMatch) -> str:
    head = format_locator(match.doc, match.unit, match.label, match.low_text)
    section = format_section_info(match.heading_path, match.line_start, match.line_end)
    return f"{index}. {head}{section}\n   {match.excerpt}"


def format_grep(result: GrepResult, budget: int) -> str:
    if not result.total_hits:
        return ("No matches. Check spelling, try ignore_case=true or a shorter pattern, "
                "or widen path_glob/doc/pages.")
    counts = [f"  {path}: {count}" for path, count in result.hits_per_doc.items()]
    count_lines, dropped_counts = _within_budget(counts, budget // 4)
    summary = [f"{result.total_hits} hits in {len(result.hits_per_doc)} documents:", *count_lines]
    if dropped_counts:
        summary.append(f"  [{dropped_counts} more documents not listed]")
    entries = [_match_entry(i, m) for i, m in enumerate(result.matches, 1)]
    kept, _ = _within_budget(entries, budget - sum(len(line) + 1 for line in summary))
    out = "\n".join(summary) + "\n\n" + "\n".join(kept)
    shown_docs = {m.doc for m in result.matches[:len(kept)]}
    unseen_hits = result.total_hits - len(kept)
    if unseen_hits:
        unseen_docs = len(result.hits_per_doc) - len(shown_docs)
        out += (f"\n\n[showing {len(kept)} of {result.total_hits} hits; {unseen_hits} more hits "
                f"in {unseen_docs} documents not shown. Narrow with path_glob, doc or pages, "
                "use a more specific pattern or less context, or raise max_hits.]")
    return out
