from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Unit:
    unit_no: int
    label: str | None
    text: str
    image_count: int = 0
    low_text: bool = False
    heading_path: str = ""
    line_start: int | None = None
    line_end: int | None = None


@dataclass
class OutlineEntry:
    level: int
    title: str
    unit_no: int | None


@dataclass
class Extracted:
    title: str | None
    author: str | None
    meta: dict
    units: list[Unit] = field(default_factory=list)
    outline: list[OutlineEntry] = field(default_factory=list)
