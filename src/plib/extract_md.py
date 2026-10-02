from __future__ import annotations

import re
from pathlib import Path

import yaml

from .models import Extracted, OutlineEntry, Unit

MAX_SECTION_CHARS = 8000
HEADING_RE = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+(.*?))?(?:[ \t]+#+)?[ \t]*$")
FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})")


def extract_md(path) -> Extracted:
    raw = Path(path).read_text(encoding="utf-8", errors="replace")
    lines = raw.splitlines()
    meta, body_start = _front_matter(lines)

    sections = _split_sections(lines, body_start)
    fallback_title = Path(path).stem
    first_h1 = next((s["title"] for s in sections if s["level"] == 1 and s["title"]), None)
    fm_title = meta.get("title")
    title = str(fm_title) if fm_title else first_h1 or fallback_title
    author = meta.get("author")

    units: list[Unit] = []
    outline: list[OutlineEntry] = []
    stack: list[tuple[int, str]] = []
    for sec in sections:
        if sec["level"]:
            while stack and stack[-1][0] >= sec["level"]:
                stack.pop()
            stack.append((sec["level"], sec["title"]))
            outline.append(OutlineEntry(sec["level"], sec["title"], len(units) + 1))
        heading_path = " > ".join(t for _, t in stack)
        label = sec["title"] if sec["level"] else title
        pieces = _split_oversized(sec["lines"], sec["start"])
        for n, (text, start, end) in enumerate(pieces, 1):
            units.append(Unit(
                unit_no=len(units) + 1,
                label=f"{label} ({n}/{len(pieces)})" if len(pieces) > 1 else label,
                text=text,
                heading_path=heading_path,
                line_start=start,
                line_end=end,
            ))
    return Extracted(
        title=title,
        author=str(author) if isinstance(author, (str, int)) else None,
        meta=meta,
        units=units,
        outline=outline,
    )


def _front_matter(lines: list[str]) -> tuple[dict, int]:
    if not lines or lines[0].strip() != "---":
        return {}, 0
    for i in range(1, len(lines)):
        if lines[i].strip() in ("---", "..."):
            try:
                data = yaml.safe_load("\n".join(lines[1:i]))
            except yaml.YAMLError:
                return {}, 0
            return (data if isinstance(data, dict) else {}), i + 1
    return {}, 0


def _split_sections(lines: list[str], body_start: int) -> list[dict]:
    """Sections as {level, title, start (1-based line), lines}; level 0 is pre-heading text."""
    sections: list[dict] = []
    current = {"level": 0, "title": "", "start": body_start + 1, "lines": []}
    fence: tuple[str, int] | None = None
    for idx in range(body_start, len(lines)):
        line = lines[idx]
        m = FENCE_RE.match(line)
        if m:
            marker = m.group(1)
            if fence is None:
                fence = (marker[0], len(marker))
            elif marker[0] == fence[0] and len(marker) >= fence[1] and not line.strip()[len(marker):].strip():
                fence = None
        elif fence is None and (h := HEADING_RE.match(line)):
            sections.append(current)
            current = {"level": len(h.group(1)), "title": (h.group(2) or "").strip(),
                       "start": idx + 1, "lines": []}
        current["lines"].append(line)
    sections.append(current)
    return [s for s in sections if s["level"] or "".join(s["lines"]).strip()]


def _split_oversized(lines: list[str], start: int) -> list[tuple[str, int, int]]:
    """Pack paragraphs into pieces of at most MAX_SECTION_CHARS; returns (text, first, last line)."""
    def trimmed(seg: list[str], first: int) -> tuple[str, int, int]:
        while seg and not seg[-1].strip():
            seg = seg[:-1]
        return "\n".join(seg), first, first + max(len(seg), 1) - 1

    whole = "\n".join(lines)
    if len(whole) <= MAX_SECTION_CHARS:
        return [trimmed(lines, start)]

    paragraphs: list[tuple[int, list[str]]] = []
    buf: list[str] = []
    buf_start = start
    for offset, line in enumerate(lines):
        if not line.strip() and buf:
            paragraphs.append((buf_start, buf))
            buf = []
        if line.strip():
            if not buf:
                buf_start = start + offset
            buf.append(line)
    if buf:
        paragraphs.append((buf_start, buf))

    atoms: list[tuple[int, list[str]]] = []
    for pstart, plines in paragraphs:
        if len("\n".join(plines)) <= MAX_SECTION_CHARS:
            atoms.append((pstart, plines))
            continue
        for k, line in enumerate(plines):
            for c in range(0, max(len(line), 1), MAX_SECTION_CHARS):
                atoms.append((pstart + k, [line[c:c + MAX_SECTION_CHARS]]))

    pieces: list[tuple[str, int, int]] = []
    cur: list[str] = []
    cur_first = cur_last = 0
    for astart, alines in atoms:
        alen = len("\n".join(alines))
        if cur and len("\n".join(cur)) + 2 + alen > MAX_SECTION_CHARS:
            pieces.append(("\n".join(cur), cur_first, cur_last))
            cur = []
        if not cur:
            cur_first = astart
        else:
            cur.append("")
        cur.extend(alines)
        cur_last = astart + len(alines) - 1
    if cur:
        pieces.append(("\n".join(cur), cur_first, cur_last))
    return pieces
