from __future__ import annotations

import pytest

from conftest import Server

GUIDE = """---
title: Guide Title
author: Ada
tags: [zebrameta]
---
Intro text before any heading.

# Install

Install overview.

## Linux

Run the apt installer.

```bash
# fakeheading comment inside a fence
echo hi
```

Still in Linux after the fence.

## macOS

Use brew.

# Usage

Usage text.
"""

OVERSIZED = "# Big\n\n" + "\n\n".join(
    f"para{i:03d} " + ("lorem ipsum " * 60) for i in range(30)
) + "\n"


@pytest.fixture
def markdown_files(library):
    (library / "notes").mkdir()
    (library / "notes" / "guide.md").write_text(GUIDE)
    (library / "notes" / "h1only.markdown").write_text("# Real Title\n\nbody about quokkas\n")
    (library / "plain.md").write_text("Just prose about axolotls.\n\nSecond paragraph.\n")
    (library / "big.md").write_text(OVERSIZED)


async def search(server: Server, query: str) -> str:
    async with server.client() as session:
        return (await session.call_tool("search", {"query": query})).content[0].text


async def test_nested_heading_has_path_label_and_line_range(markdown_files, server):
    text = await search(server, "apt")
    assert "notes/guide.md" in text
    assert "label: 'Linux'" in text
    assert "section: Install > Linux" in text
    assert "lines 12-21" in text


async def test_hash_lines_in_fenced_code_do_not_start_sections(markdown_files, server):
    text = await search(server, "fakeheading")
    assert "section: Install > Linux" in text
    assert "label: 'Linux'" in text
    after_fence = await search(server, "fence")
    assert "section: Install > Linux" in after_fence


async def test_front_matter_excluded_from_text_and_used_for_title(markdown_files, server):
    assert "No matches" in await search(server, "zebrameta")
    assert "Guide Title" in await search(server, "brew")


async def test_title_falls_back_to_first_h1_then_filename(markdown_files, server):
    assert "Real Title" in await search(server, "quokkas")
    assert "1. plain —" in await search(server, "axolotls")


async def test_text_before_first_heading_is_searchable(markdown_files, server):
    text = await search(server, "before")
    assert "notes/guide.md" in text and "section:" not in text.split("\n")[1]


async def test_headingless_file_is_one_unit_labelled_by_title(markdown_files, server):
    text = await search(server, "axolotls OR paragraph")
    assert text.count("locator") == 1
    assert "unit: 1" in text and "label: 'plain'" in text


async def test_oversized_section_is_split_at_paragraphs(markdown_files, server):
    first = await search(server, "para000")
    last = await search(server, "para029")
    assert "label: 'Big (1/" in first
    n = int(first.split("label: 'Big (1/")[1].split(")")[0])
    assert n > 1
    assert f"label: 'Big ({n}/{n})'" in last
    assert "para000" not in last.replace("[[para029]]", "")


async def test_markdown_and_pdf_are_searched_together(markdown_files, server):
    text = await search(server, "installer OR router")
    assert "notes/guide.md" in text and "networking.pdf" in text
