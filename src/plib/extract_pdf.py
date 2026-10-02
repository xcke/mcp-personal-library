from __future__ import annotations

import pymupdf

from .models import Extracted, OutlineEntry, Unit

LOW_TEXT_CHARS = 50


class EncryptedPDF(Exception):
    pass


def extract_pdf(path) -> Extracted:
    with pymupdf.open(path) as doc:
        if doc.needs_pass:
            raise EncryptedPDF("PDF is password-protected")
        meta = {k: v for k, v in (doc.metadata or {}).items() if v}
        outline = [
            OutlineEntry(level=lvl, title=title.strip(), unit_no=page if page > 0 else None)
            for lvl, title, page in doc.get_toc(simple=True)
        ]
        units = []
        for page in doc:
            text = page.get_text("text")
            images = len(page.get_images(full=True))
            has_graphics = images > 0 or len(page.get_drawings()) > 0
            low = len(text.strip()) < LOW_TEXT_CHARS and has_graphics
            units.append(
                Unit(
                    unit_no=page.number + 1,
                    label=_page_label(page),
                    text=text,
                    image_count=images,
                    low_text=low,
                )
            )
    _assign_heading_paths(units, outline)
    return Extracted(
        title=meta.get("title"),
        author=meta.get("author"),
        meta=meta,
        units=units,
        outline=outline,
    )


def _page_label(page: pymupdf.Page) -> str | None:
    try:
        return page.get_label() or None
    except (IndexError, ValueError, RuntimeError):  # malformed label tables must not fail the document
        return None


def _assign_heading_paths(units: list[Unit], outline: list[OutlineEntry]) -> None:
    entries = [e for e in outline if e.unit_no is not None]
    for unit in units:
        stack: list[OutlineEntry] = []
        for e in entries:
            if e.unit_no > unit.unit_no:
                continue
            while stack and stack[-1].level >= e.level:
                stack.pop()
            stack.append(e)
        unit.heading_path = " > ".join(e.title for e in stack)
