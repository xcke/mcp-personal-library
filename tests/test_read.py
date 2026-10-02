from __future__ import annotations

import re

import pymupdf

from conftest import Server, start_server

LOW_TEXT_MARKER = "[low text — consider render_page]"


async def call(server: Server, tool: str, **args):
    async with server.client() as session:
        return await session.call_tool(tool, args)


async def text(server: Server, tool: str, **args) -> str:
    result = await call(server, tool, **args)
    assert not result.isError, result.content[0].text
    return result.content[0].text


async def error(server: Server, tool: str, **args) -> str:
    result = await call(server, tool, **args)
    assert result.isError
    return result.content[0].text


def headers(out: str) -> list[str]:
    return [line for line in out.splitlines() if line.startswith("=== ")]


async def test_read_physical_range_shows_headers_with_page_label_and_heading(server):
    out = await text(server, "read", doc="books/gardening.pdf", **{"from": "2-3"})
    first, second = headers(out)
    assert "page 2" in first and "label ii" in first and "Basics > Irrigation" in first
    assert "page 3" in second and "label 1" in second
    assert "Configuring the irrigation system" in out
    assert "Tomatoes need full sun" in out
    assert "Introduction to gardening" not in out


async def test_read_accepts_comma_separated_physical_pages(server):
    out = await text(server, "read", doc="books/gardening.pdf", **{"from": "1,3"})
    assert len(headers(out)) == 2
    assert "Introduction to gardening" in out and "Tomatoes" in out
    assert "irrigation" not in out


async def test_read_to_extends_a_single_start_page(server):
    out = await text(server, "read", doc="books/gardening.pdf", **{"from": "1", "to": "2"})
    assert len(headers(out)) == 2


async def test_read_by_printed_label_range(server):
    out = await text(server, "read", doc="books/gardening.pdf", **{"from": "label:ii-1"})
    first, second = headers(out)
    assert "page 2" in first and "page 3" in second


async def test_read_by_single_label_with_to(server):
    out = await text(server, "read", doc="books/gardening.pdf",
                     **{"from": "label:i", "to": "ii"})
    assert [("page 1" in h, "page 2" in h) for h in headers(out)] == [(True, False), (False, True)]


async def test_read_accepts_numeric_doc_id(server):
    listing = await text(server, "list_documents", glob="networking.pdf")
    doc_id = listing.split("|")[0].strip().removeprefix("id ").strip()
    assert "Firewall rules" in await text(server, "read", doc=doc_id, **{"from": "2"})


async def test_read_markdown_addresses_section_numbers(markdown_files, server):
    out = await text(server, "read", doc="notes/guide.md", **{"from": "3"})
    (header,) = headers(out)
    assert "section 3" in header and "Install > Linux" in header and "lines" in header
    assert "Run the apt installer." in out
    assert "Use brew." not in out


async def test_read_markdown_rejects_labels(markdown_files, server):
    message = await error(server, "read", doc="notes/guide.md", **{"from": "label:ii"})
    assert "PDF" in message and "section numbers" in message


async def test_out_of_range_page_error_says_valid_range(server):
    message = await error(server, "read", doc="books/gardening.pdf", **{"from": "3-9"})
    assert "out of range" in message and "1-4" in message


async def test_unknown_label_error_points_to_doc_info(server):
    message = await error(server, "read", doc="books/gardening.pdf", **{"from": "label:xx"})
    assert "xx" in message and "doc_info" in message


async def test_malformed_and_reversed_ranges_are_actionable(server):
    for spec in ("abc", "4-2", "0"):
        message = await error(server, "read", doc="books/gardening.pdf", **{"from": spec})
        assert "10-20,35" in message or "label:" in message, spec


async def test_long_read_paginates_with_exact_resumption(markdown_files, server):
    whole = await text(server, "read", doc="big.md", **{"from": "1-3"}, max_chars=200_000)
    assert "next_cursor" not in whole

    chunks, cursor = [], None
    for _ in range(100):
        args = {"from": "1-3", "max_chars": 1500}
        if cursor:
            args["cursor"] = cursor
        out = await text(server, "read", doc="big.md", **args)
        assert len(out) <= 1500 + 300  # budget plus the trailing cursor hint
        chunks.append(out)
        match = re.search(r"next_cursor: (\S+)", out)
        if not match:
            break
        cursor = match.group(1)
    else:
        raise AssertionError("pagination never finished")

    def body(out: str) -> str:
        kept = [l for l in out.splitlines() if not l.startswith("=== ") and "next_cursor" not in l]
        return "".join("".join(kept).split())

    assert len(chunks) > 2
    assert "".join(body(c) for c in chunks) == body(whole)


async def test_bad_cursor_is_rejected(server):
    message = await error(server, "read", doc="networking.pdf", **{"from": "1-2"},
                          cursor="garbage")
    assert "cursor" in message.lower() and "next_cursor" in message


async def _low_text_library(library):
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 100), "faint caption", fontsize=11)
    pixmap = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 200, 120))
    pixmap.set_rect(pixmap.irect, (30, 30, 200))
    page.insert_image(pymupdf.Rect(72, 150, 372, 330), pixmap=pixmap)
    doc.save(library / "scan.pdf")
    doc.close()


async def test_low_text_pages_are_marked_in_read_and_search(library):
    await _low_text_library(library)
    with start_server(library) as running:
        read_out = await text(running, "read", doc="scan.pdf", **{"from": "1"})
        search_out = await text(running, "search", query="caption")
        normal = await text(running, "read", doc="networking.pdf", **{"from": "1"})
    assert LOW_TEXT_MARKER in headers(read_out)[0]
    assert LOW_TEXT_MARKER in search_out
    assert LOW_TEXT_MARKER not in normal


async def test_search_pages_filter_by_physical_numbers(server):
    everywhere = await text(server, "search", query="configure OR tomatoes OR firewall")
    assert "networking.pdf" in everywhere and "gardening.pdf" in everywhere
    only_page_one = await text(server, "search", query="configure OR tomatoes OR firewall",
                               pages="1")
    assert "unit: 1" in only_page_one and "unit: 2" not in only_page_one
    assert "unit: 3" not in only_page_one


async def test_search_pages_filter_by_label_needs_a_doc(server):
    hit = await text(server, "search", query="configuring OR tomatoes",
                     doc="books/gardening.pdf", pages="label:1-2")
    assert "unit: 3" in hit and "unit: 2" not in hit
    message = await error(server, "search", query="tomatoes", pages="label:1")
    assert "doc" in message


async def test_search_pages_out_of_range_for_a_doc_is_an_error(server):
    message = await error(server, "search", query="tomatoes", doc="books/gardening.pdf",
                          pages="9")
    assert "out of range" in message
