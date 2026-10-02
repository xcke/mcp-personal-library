from __future__ import annotations

import hashlib
import shutil
import sqlite3
import struct
from dataclasses import dataclass
from pathlib import Path

import pymupdf

from .errors import QueryError
from .pages import resolve_spans
from .query import resolve_doc

MAX_PIXELS = 2000
MIN_DPI, MAX_DPI = 20, 600
RENDER_DIRNAME = "renders"
PNG_IHDR_OFFSET = 16

CLIP_SYNTAX_HINT = (
    "clip is 'x0,y0,x1,y1' with the origin at the page's top-left: either PDF points "
    "(e.g. '72,100,300,220') or, when every value is between 0 and 1, fractions of the page "
    "(e.g. '0,0.5,1,1' for the bottom half)."
)


@dataclass
class RenderedPage:
    png: bytes
    page_width_pt: float
    page_height_pt: float
    width_px: int
    height_px: int
    capped: bool


def _document_cache_dir(index_dir: Path, doc_path: str) -> Path:
    return index_dir / RENDER_DIRNAME / hashlib.sha256(doc_path.encode()).hexdigest()[:24]


def invalidate_renders(index_dir: Path, doc_path: str) -> None:
    shutil.rmtree(_document_cache_dir(index_dir, doc_path), ignore_errors=True)


def _parse_clip(clip: str, page_rect: pymupdf.Rect) -> pymupdf.Rect:
    try:
        x0, y0, x1, y1 = (float(part) for part in clip.split(","))
    except ValueError:
        raise QueryError(f"Invalid clip {clip!r}. {CLIP_SYNTAX_HINT}") from None
    if all(0 <= value <= 1 for value in (x0, y0, x1, y1)):
        x0, x1 = page_rect.x0 + x0 * page_rect.width, page_rect.x0 + x1 * page_rect.width
        y0, y1 = page_rect.y0 + y0 * page_rect.height, page_rect.y0 + y1 * page_rect.height
    region = pymupdf.Rect(x0, y0, x1, y1) & page_rect
    if x1 <= x0 or y1 <= y0 or region.is_empty:
        raise QueryError(
            f"Clip {clip!r} is empty or outside the page ({page_rect.width:.0f}x"
            f"{page_rect.height:.0f} pt). {CLIP_SYNTAX_HINT}"
        )
    return region


def _single_page(conn: sqlite3.Connection, doc_row: sqlite3.Row, page: str) -> int:
    first, last = resolve_spans(conn, doc_row, page)[0]
    if "," in page or first != last:
        raise QueryError(
            f"render_page takes one page, got {page!r}. Pass a single physical number "
            "like '12' or a printed label like 'label:xii'."
        )
    return first


def _png_size(png: bytes) -> tuple[int, int]:
    return struct.unpack(">II", png[PNG_IHDR_OFFSET:PNG_IHDR_OFFSET + 8])


def _rasterize(page: pymupdf.Page, region: pymupdf.Rect, dpi: int) -> bytes:
    scale = dpi / 72
    while True:
        pixmap = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), clip=region)
        if max(pixmap.width, pixmap.height) <= MAX_PIXELS:
            return pixmap.tobytes("png")
        scale *= MAX_PIXELS / max(pixmap.width, pixmap.height) * 0.995


def render_page(
    conn: sqlite3.Connection,
    library_root: Path,
    index_dir: Path,
    doc: str | int,
    page: str,
    *,
    dpi: int,
    clip: str | None,
) -> RenderedPage:
    doc_row = resolve_doc(conn, doc)
    if doc_row["kind"] != "pdf":
        raise QueryError(
            f"{doc_row['path']!r} is Markdown and has no page images. "
            "Use the read tool to get its text instead."
        )
    if not MIN_DPI <= dpi <= MAX_DPI:
        raise QueryError(f"dpi {dpi} is out of range. Use a value from {MIN_DPI} to {MAX_DPI}.")
    page_no = _single_page(conn, doc_row, page)
    with pymupdf.open(library_root / doc_row["path"]) as pdf:
        pdf_page = pdf[page_no - 1]
        page_rect = pdf_page.rect
        region = _parse_clip(clip, page_rect) if clip else page_rect
        cache_file = _document_cache_dir(index_dir, doc_row["path"]) / (
            f"{page_no}-" + hashlib.sha256(f"{dpi}|{clip or ''}".encode()).hexdigest()[:16] + ".png")
        if cache_file.is_file():
            png = cache_file.read_bytes()
        else:
            png = _rasterize(pdf_page, region, dpi)
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_bytes(png)
    width_px, height_px = _png_size(png)
    capped = max(region.width, region.height) * dpi / 72 > MAX_PIXELS
    return RenderedPage(png, page_rect.width, page_rect.height, width_px, height_px, capped)
