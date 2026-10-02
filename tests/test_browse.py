from __future__ import annotations

from conftest import Server


async def call(server: Server, tool: str, **args):
    async with server.client() as session:
        return await session.call_tool(tool, args)


async def text(server: Server, tool: str, **args) -> str:
    result = await call(server, tool, **args)
    assert not result.isError, result.content[0].text
    return result.content[0].text


async def test_list_documents_shows_path_kind_title_count_and_status(markdown_files, server):
    out = await text(server, "list_documents")
    gardening = next(line for line in out.splitlines() if "books/gardening.pdf" in line)
    assert "pdf" in gardening and "4 pages" in gardening and "ok" in gardening
    guide = next(line for line in out.splitlines() if "notes/guide.md" in line)
    assert "md" in guide and "Guide Title" in guide and "5 sections" in guide
    assert "secret.pdf" in out and "encrypted" in out
    assert "corrupt.pdf" in out and "error" in out


async def test_list_documents_filters_by_glob_and_kind(markdown_files, server):
    only_books = await text(server, "list_documents", glob="books/*")
    assert "gardening.pdf" in only_books and "networking.pdf" not in only_books
    only_md = await text(server, "list_documents", kind="md")
    assert "guide.md" in only_md and ".pdf" not in only_md


async def test_list_documents_paginates_with_hint(markdown_files, server):
    first = await text(server, "list_documents", limit=2)
    assert "more documents" in first and "offset=2" in first
    rest = await text(server, "list_documents", limit=100, offset=2)
    assert "more documents" not in rest
    all_paths = await text(server, "list_documents", limit=100)
    for path in ("books/gardening.pdf", "networking.pdf", "notes/guide.md"):
        assert path in all_paths


async def test_doc_info_for_pdf_has_labels_and_low_text_pages(server):
    out = await text(server, "doc_info", doc="books/gardening.pdf")
    assert "pages: 4" in out
    assert "low-text pages: 4" in out
    assert "i-ii" in out  # roman-numeral front matter
    assert "1-2" in out  # decimal numbering restarts at physical page 3


async def test_doc_info_for_pdf_without_labels_or_low_text(server):
    out = await text(server, "doc_info", doc="networking.pdf")
    assert "pages: 2" in out
    assert "low-text pages: none" in out


async def test_doc_info_for_markdown_shows_front_matter(markdown_files, server):
    out = await text(server, "doc_info", doc="notes/guide.md")
    assert "sections: 5" in out
    assert "author: Ada" in out and "zebrameta" in out


async def test_doc_info_for_failed_document_shows_status_and_reason(server):
    out = await text(server, "doc_info", doc="secret.pdf")
    assert "status: encrypted" in out and "password" in out


async def test_get_outline_for_pdf_is_a_tree_with_units(server):
    out = await text(server, "get_outline", doc="books/gardening.pdf")
    lines = out.splitlines()
    basics = next(l for l in lines if "Basics" in l)
    irrigation = next(l for l in lines if "Irrigation" in l)
    assert "unit 1" in basics and "unit 2" in irrigation
    assert len(irrigation) - len(irrigation.lstrip()) > len(basics) - len(basics.lstrip())
    assert "Diagrams" in out and "unit 4" in out


async def test_get_outline_max_level_prunes_deeper_entries(server):
    out = await text(server, "get_outline", doc="books/gardening.pdf", max_level=1)
    assert "Basics" in out and "Irrigation" not in out


async def test_get_outline_for_markdown_is_the_heading_tree(markdown_files, server):
    out = await text(server, "get_outline", doc="notes/guide.md")
    for title in ("Install", "Linux", "macOS", "Usage"):
        assert title in out
    linux = next(l for l in out.splitlines() if "Linux" in l)
    assert "unit 3" in linux


async def test_get_outline_without_outline_says_so(server):
    out = await text(server, "get_outline", doc="networking.pdf")
    assert "no outline" in out.lower()


async def test_pdf_pages_carry_deepest_outline_heading_in_hits(server):
    out = await text(server, "search", query="tomatoes")
    assert "section: Basics > Irrigation" in out  # page 3 sits under Irrigation


async def test_all_tools_accept_numeric_id(server):
    listing = await text(server, "list_documents")
    gardening_line = next(l for l in listing.splitlines() if "books/gardening.pdf" in l)
    doc_id = gardening_line.split("|")[0].strip().removeprefix("id ").strip()
    assert "pages: 4" in await text(server, "doc_info", doc=doc_id)
    assert "Basics" in await text(server, "get_outline", doc=doc_id)


async def test_unknown_document_gives_clear_error(server):
    for tool in ("doc_info", "get_outline"):
        result = await call(server, tool, doc="missing.pdf")
        assert result.isError and "Unknown document" in result.content[0].text
