from __future__ import annotations

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
