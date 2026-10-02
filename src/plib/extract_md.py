from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .models import Extracted, OutlineEntry, Unit

MAX_SECTION_CHARS = 8000
UNTITLED = "(untitled)"
HEADING_RE = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+(.*?))?(?:[ \t]+#+)?[ \t]*$")
FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})")


@dataclass
class Section:
    level: int  # 0 for text before the first heading
    title: str
    first_line: int  # 1-based, in the original file
    lines: list[str] = field(default_factory=list)


@dataclass
class Chunk:
    lines: list[str]
    first_line: int
    last_line: int = 0

    def __post_init__(self) -> None:
        if not self.last_line:
            self.last_line = self.first_line + len(self.lines) - 1

    def append_paragraph(self, other: Chunk) -> None:
        self.lines.extend(["", *other.lines])
        self.last_line = other.last_line

    @property
    def length(self) -> int:
        return len("\n".join(self.lines))


def extract_md(path) -> Extracted:
    raw = Path(path).read_text(encoding="utf-8", errors="replace")
    lines = raw.splitlines()
    meta, body_start = _split_front_matter(lines)
    sections = _split_sections(lines, body_start)

    first_h1 = next((s.title for s in sections if s.level == 1), None)
    title = str(meta["title"]) if meta.get("title") else first_h1 or Path(path).stem
    author = meta.get("author")

    units: list[Unit] = []
    outline: list[OutlineEntry] = []
    heading_stack: list[tuple[int, str]] = []
    for section in sections:
        if section.level:
            while heading_stack and heading_stack[-1][0] >= section.level:
                heading_stack.pop()
            heading_stack.append((section.level, section.title))
            outline.append(OutlineEntry(section.level, section.title, len(units) + 1))
        base_label = section.title if section.level else title
        heading_path = " > ".join(heading for _, heading in heading_stack)
        pieces = _split_oversized(section)
        for part, piece in enumerate(pieces, 1):
            label = f"{base_label} ({part}/{len(pieces)})" if len(pieces) > 1 else base_label
            units.append(Unit(
                unit_no=len(units) + 1,
                label=label,
                text="\n".join(piece.lines),
                heading_path=heading_path,
                line_start=piece.first_line,
                line_end=piece.last_line,
            ))
    return Extracted(
        title=title,
        author=str(author) if isinstance(author, (str, int)) else None,
        meta=meta,
        units=units,
        outline=outline,
    )


def _split_front_matter(lines: list[str]) -> tuple[dict, int]:
    """Returns (metadata, index of the first body line). Malformed YAML yields no metadata."""
    if not lines or lines[0].strip() != "---":
        return {}, 0
    for end in range(1, len(lines)):
        if lines[end].strip() in ("---", "..."):
            try:
                data = yaml.safe_load("\n".join(lines[1:end]))
            except yaml.YAMLError:
                data = None
            return (data if isinstance(data, dict) else {}), end + 1
    return {}, 0


def _closes_fence(line: str, fence_char: str, fence_len: int) -> bool:
    match = FENCE_RE.match(line)
    if not match:
        return False
    marker = match.group(1)
    rest = line.strip()[len(marker):].strip()
    return marker[0] == fence_char and len(marker) >= fence_len and not rest


def _split_sections(lines: list[str], body_start: int) -> list[Section]:
    sections: list[Section] = []
    current = Section(level=0, title="", first_line=body_start + 1)
    open_fence: tuple[str, int] | None = None
    for index in range(body_start, len(lines)):
        line = lines[index]
        fence = FENCE_RE.match(line)
        if open_fence is not None:
            if _closes_fence(line, *open_fence):
                open_fence = None
        elif fence:
            open_fence = (fence.group(1)[0], len(fence.group(1)))
        elif heading := HEADING_RE.match(line):
            sections.append(current)
            title = (heading.group(2) or "").strip() or UNTITLED
            current = Section(level=len(heading.group(1)), title=title, first_line=index + 1)
        current.lines.append(line)
    sections.append(current)
    return [s for s in sections if s.level or "".join(s.lines).strip()]


def _trim_trailing_blanks(lines: list[str]) -> list[str]:
    end = len(lines)
    while end > 1 and not lines[end - 1].strip():
        end -= 1
    return lines[:end]


def _paragraphs(section: Section) -> list[Chunk]:
    paragraphs: list[Chunk] = []
    run: list[str] = []
    run_first = 0
    for offset, line in enumerate(section.lines + [""]):
        if line.strip():
            if not run:
                run_first = section.first_line + offset
            run.append(line)
        elif run:
            paragraphs.append(Chunk(run, run_first))
            run = []
    return paragraphs


def _fit_to_limit(paragraph: Chunk) -> list[Chunk]:
    """A paragraph over the limit is cut line by line, then by characters."""
    if paragraph.length <= MAX_SECTION_CHARS:
        return [paragraph]
    parts = []
    for offset, line in enumerate(paragraph.lines):
        for start in range(0, max(len(line), 1), MAX_SECTION_CHARS):
            parts.append(Chunk([line[start:start + MAX_SECTION_CHARS]], paragraph.first_line + offset))
    return parts


def _split_oversized(section: Section) -> list[Chunk]:
    whole = Chunk(_trim_trailing_blanks(section.lines), section.first_line)
    if len("\n".join(section.lines)) <= MAX_SECTION_CHARS:
        return [whole]

    pieces: list[Chunk] = []
    current: Chunk | None = None
    for atom in (a for p in _paragraphs(section) for a in _fit_to_limit(p)):
        if current is not None and current.length + 2 + atom.length > MAX_SECTION_CHARS:
            pieces.append(current)
            current = None
        if current is None:
            current = Chunk(list(atom.lines), atom.first_line, atom.last_line)
        else:
            current.append_paragraph(atom)
    if current is not None:
        pieces.append(current)
    return pieces
