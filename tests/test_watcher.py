from __future__ import annotations

import asyncio
import os
import shutil
import socket
import subprocess
import sys
import time

import pytest

from conftest import TOKEN, Server, make_pdf, start_server
from plib.watcher import WatchTiming
from test_read import text

FAST = WatchTiming(debounce=0.2, recheck=0.2)


async def eventually(check, timeout=20.0) -> str:
    """Polls an async check returning (done, output) until done or the timeout; returns the last output."""
    deadline = time.time() + timeout
    while True:
        done, out = await check()
        if done or time.time() > deadline:
            return out
        await asyncio.sleep(0.1)


async def search_shows(server: Server, query: str, expected: str | None) -> str:
    """Waits until a search names `expected` (or, when None, finds nothing)."""
    async def check():
        out = await text(server, "search", query=query)
        return (expected in out if expected else "No matches" in out), out
    return await eventually(check)


@pytest.fixture
def watched(library):
    with start_server(library, watch=FAST) as running:
        yield running


async def test_added_file_becomes_searchable(watched):
    make_pdf(watched.root / "books" / "cooking.pdf", ["Braising needs patience"])
    out = await search_shows(watched, "braising", "books/cooking.pdf")
    assert "books/cooking.pdf" in out
    assert "total: 5" in await text(watched, "index_status")


async def test_added_markdown_file_becomes_searchable(watched):
    (watched.root / "todo.md").write_text("Remember the quokkas\n")
    assert "todo.md" in await search_shows(watched, "quokkas", "todo.md")


async def test_modified_file_is_reindexed(watched):
    assert "networking.pdf" in await text(watched, "search", query="router")
    make_pdf(watched.root / "networking.pdf", ["Configure the switch"])
    assert "networking.pdf" in await search_shows(watched, "switch", "networking.pdf")
    assert "No matches" in await search_shows(watched, "router", None)


async def test_deleted_file_is_removed(watched):
    (watched.root / "networking.pdf").unlink()
    assert "No matches" in await search_shows(watched, "router", None)
    assert "networking.pdf" not in await text(watched, "list_documents")


async def test_renamed_file_keeps_its_content_under_the_new_path(watched):
    (watched.root / "moved").mkdir()
    os.rename(watched.root / "networking.pdf", watched.root / "moved" / "net.pdf")
    assert "moved/net.pdf" in await search_shows(watched, "router", "moved/net.pdf")
    listing = await text(watched, "list_documents")
    assert "moved/net.pdf" in listing and "networking.pdf" not in listing


async def test_renamed_folder_moves_its_documents(watched):
    shutil.move(watched.root / "books", watched.root / "shelf")
    assert "shelf/gardening.pdf" in await search_shows(watched, "tomatoes", "shelf/gardening.pdf")
    assert "books/gardening.pdf" not in await text(watched, "list_documents")


async def test_file_still_being_copied_is_not_indexed_half_written(watched):
    target = watched.root / "slow-copy.md"
    with open(target, "w") as handle:
        for part in range(8):
            handle.write(f"line {part} about pangolins\n")
            handle.flush()
            await asyncio.sleep(0.1)
            assert "No matches" in await text(watched, "search", query="pangolins")
    assert "slow-copy.md" in await search_shows(watched, "pangolins", "slow-copy.md")
    assert "line 7" in await text(watched, "read", doc="slow-copy.md", **{"from": "1"})


async def test_ignored_paths_are_not_indexed(library):
    (library / ".plibignore").write_text("drafts/\n*.skip.md\n")
    (library / "drafts").mkdir()
    (library / ".hidden").mkdir()
    with start_server(library, watch=FAST) as server:
        (library / "drafts" / "a.md").write_text("ignoredword one\n")
        (library / "note.skip.md").write_text("ignoredword two\n")
        (library / ".hidden" / "b.md").write_text("ignoredword three\n")
        (library / ".dot.md").write_text("ignoredword four\n")
        (library / "kept.md").write_text("keptword\n")
        assert "kept.md" in await search_shows(server, "keptword", "kept.md")
        assert "No matches" in await text(server, "search", query="ignoredword")


async def test_cli_serve_keeps_the_index_current(library):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    env = {**os.environ, "PLIB_TOKEN": TOKEN}
    proc = subprocess.Popen([sys.executable, "-m", "plib.cli", "serve", str(library),
                             "--port", str(port)], env=env, stdout=subprocess.PIPE, text=True)
    try:
        for line in proc.stdout:
            if line.startswith("Index complete"):
                break
        server = Server(root=library, base_url=f"http://127.0.0.1:{port}", token=TOKEN)
        make_pdf(library / "late.pdf", ["Arrived while serving"])
        assert "late.pdf" in await search_shows(server, "serving", "late.pdf")
    finally:
        proc.terminate()
        proc.wait(timeout=10)
