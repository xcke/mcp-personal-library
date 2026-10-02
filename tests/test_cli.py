from __future__ import annotations

import os
import time

from conftest import TOKEN, Server, make_pdf, run_cli


def index(library, *extra):
    r = run_cli("index", str(library), *extra)
    assert r.returncode == 0, r.stderr
    return r.stdout


def status(library):
    r = run_cli("status", str(library))
    assert r.returncode == 0, r.stderr
    return r.stdout


def test_index_builds_index_without_serving(library):
    out = index(library)
    assert (library / ".plib" / "index.sqlite").exists()
    assert "indexed 2" in out and "failed 2" in out


def test_second_run_with_no_changes_does_no_extraction(library):
    index(library)
    out = index(library)
    assert "indexed 0" in out and "unchanged 4" in out
    assert "failed 0" in out  # broken files are not retried


def test_changed_file_is_reindexed_and_deleted_file_removed(library):
    index(library)
    make_pdf(library / "networking.pdf", ["Completely different content about wombats"])
    (library / "books" / "gardening.pdf").unlink()
    out = index(library)
    assert "indexed 1" in out and "removed 1" in out
    assert "documents: 3" in status(library)  # networking + 2 failed remain


def test_touched_but_identical_file_is_not_reextracted(library):
    index(library)
    f = library / "networking.pdf"
    os.utime(f, (time.time() + 100, time.time() + 100))
    out = index(library)
    assert "indexed 0" in out and "unchanged 4" in out


def test_dotfiles_dotdirs_and_plibignore_are_skipped(library):
    (library / ".hidden").mkdir()
    make_pdf(library / ".hidden" / "a.pdf", ["hidden"])
    make_pdf(library / ".dot.pdf", ["hidden"])
    (library / "drafts").mkdir()
    make_pdf(library / "drafts" / "wip.pdf", ["draft"])
    make_pdf(library / "notes.tmp.pdf", ["tmp"])
    (library / ".plibignore").write_text("drafts/\n*.tmp.pdf\n")
    out = index(library)
    assert "indexed 2" in out and "unchanged 0" in out


def test_status_shows_totals_failed_reasons_and_pending(library):
    index(library)
    make_pdf(library / "new.pdf", ["fresh"])
    out = status(library)
    assert "indexed: 2" in out
    assert "failed: 2" in out
    assert "pending: 1" in out
    assert "secret.pdf" in out and "password" in out.lower()
    assert "corrupt.pdf" in out


def test_status_before_any_index_reports_everything_pending_and_creates_nothing(library):
    out = status(library)
    assert "pending: 4" in out
    assert not (library / ".plib").exists()


def test_failed_file_is_retried_only_when_it_changes(library):
    index(library)
    make_pdf(library / "corrupt.pdf", ["now valid text"])
    out = index(library)
    assert "indexed 1" in out and "failed 0" in out and "unchanged 3" in out
    assert "failed: 1" in status(library)


async def test_failed_documents_absent_others_searchable_via_mcp(server: Server):
    async with server.client() as session:
        ok = await session.call_tool("search", {"query": "router"})
        gone = await session.call_tool("search", {"query": "secret OR corrupt"})
    assert "networking.pdf" in ok.content[0].text
    assert "No matches" in gone.content[0].text


def test_serve_reconciles_at_startup(library):
    import socket
    import subprocess
    import sys
    index(library)
    make_pdf(library / "extra.pdf", ["startup reconcile marker"])
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    proc = subprocess.Popen([sys.executable, "-m", "plib.cli", "serve", str(library),
                             "--port", str(port)], env={**os.environ, "PLIB_TOKEN": TOKEN},
                            stdout=subprocess.PIPE, text=True)
    try:
        for line in proc.stdout:
            if line.startswith("MCP endpoint:"):
                break
    finally:
        proc.terminate()
        proc.wait(timeout=10)
    assert "pending: 0" in status(library)
