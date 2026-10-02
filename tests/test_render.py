from __future__ import annotations

import base64
import struct

from conftest import Server, make_pdf, start_server
from test_read import call, error



def png_size(content) -> tuple[int, int]:
    image = next(c for c in content if c.type == "image")
    assert image.mimeType == "image/png"
    raw = base64.b64decode(image.data)
    assert raw[:8] == b"\x89PNG\r\n\x1a\n"
    return struct.unpack(">II", raw[16:24])


def summary(content) -> str:
    return next(c.text for c in content if c.type == "text")


async def render(server: Server, **args):
    result = await call(server, "render_page", **args)
    assert not result.isError, result.content[0].text
    return result.content


def cache_files(root):
    return sorted((root / ".plib" / "renders").rglob("*.png"))


async def test_render_returns_png_at_requested_dpi_and_reports_page_size(server):
    content = await render(server, doc="books/gardening.pdf", page="1", dpi=72)
    assert png_size(content) == (595, 842)
    assert "page size 595x842 pt" in summary(content)
    assert "image 595x842 px" in summary(content)


async def test_default_dpi_is_110(server):
    width, height = png_size(await render(server, doc="networking.pdf", page="1"))
    assert (width, height) == (910, 1287)


async def test_clip_in_points(server):
    content = await render(server, doc="networking.pdf", page="1", dpi=72, clip="0,0,200,100")
    assert png_size(content) == (200, 100)


async def test_clip_as_page_fractions(server):
    content = await render(server, doc="networking.pdf", page="1", dpi=72, clip="0,0,0.5,0.5")
    assert png_size(content) == (298, 421)


async def test_clip_can_be_rendered_at_higher_detail(server):
    content = await render(server, doc="networking.pdf", page="1", dpi=144, clip="0,0,100,50")
    assert png_size(content) == (200, 100)


async def test_output_is_capped_with_clip_hint(server):
    content = await render(server, doc="networking.pdf", page="1", dpi=300)
    width, height = png_size(content)
    assert max(width, height) <= 2000 and max(width, height) >= 1990
    assert "clip" in summary(content)
    assert "reduced" not in summary(await render(server, doc="networking.pdf", page="1", dpi=72))


async def test_page_by_printed_label(server):
    by_label = await render(server, doc="books/gardening.pdf", page="label:ii", dpi=72,
                            clip="0,0,100,100")
    assert png_size(by_label) == (100, 100)


async def test_markdown_is_rejected_pointing_to_read(markdown_files, server):
    message = await error(server, "render_page", doc="notes/guide.md", page="1")
    assert "Markdown" in message and "read" in message


async def test_bad_inputs_give_actionable_errors(server):
    assert "out of range" in await error(server, "render_page", doc="networking.pdf", page="9")
    assert "one page" in await error(server, "render_page", doc="networking.pdf", page="1-2")
    assert "Invalid clip" in await error(server, "render_page", doc="networking.pdf", page="1",
                                         clip="a,b")
    assert "outside the page" in await error(server, "render_page", doc="networking.pdf",
                                             page="1", clip="700,0,800,100")
    assert "dpi" in await error(server, "render_page", doc="networking.pdf", page="1", dpi=5000)
    assert "Unknown document" in await error(server, "render_page", doc="x.pdf", page="1")


async def test_repeat_render_is_served_from_cache(server):
    await render(server, doc="networking.pdf", page="1", dpi=72)
    (cached,) = cache_files(server.root)
    first_write = cached.stat().st_mtime_ns
    await render(server, doc="networking.pdf", page="1", dpi=72)
    assert cache_files(server.root) == [cached]
    assert cached.stat().st_mtime_ns == first_write
    await render(server, doc="networking.pdf", page="1", dpi=72, clip="0,0,0.5,0.5")
    await render(server, doc="networking.pdf", page="2", dpi=72)
    assert len(cache_files(server.root)) == 3


async def test_cache_is_cleared_when_document_is_reindexed_or_removed(library):
    with start_server(library) as running:
        await render(running, doc="networking.pdf", page="1", dpi=72)
        await render(running, doc="books/gardening.pdf", page="1", dpi=72)
        assert len(cache_files(library)) == 2
    make_pdf(library / "networking.pdf", ["Changed"], )
    with start_server(library) as running:
        assert len(cache_files(library)) == 1
        content = await render(running, doc="networking.pdf", page="1", dpi=72)
        assert png_size(content) == (595, 842) and len(cache_files(library)) == 2
    (library / "books" / "gardening.pdf").unlink()
    with start_server(library):
        assert len(cache_files(library)) == 1
