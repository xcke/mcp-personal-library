from __future__ import annotations

import logging
import os
import subprocess
import sys

import httpx

from conftest import TOKEN, run_cli


async def call(server, tool, **args):
    async with server.client() as session:
        return await session.call_tool(tool, args)


def text_of(result) -> str:
    return "\n".join(c.text for c in result.content)


async def test_correct_token_lists_search_tool(server):
    async with server.client() as session:
        tools = await session.list_tools()
    assert "search" in [t.name for t in tools.tools]


def test_wrong_or_missing_token_gets_empty_404(server):
    for path in ["/mcp", "/wrong-token/mcp", f"/{TOKEN}x/mcp", "/", f"/{TOKEN[:-1]}/mcp"]:
        r = httpx.post(server.base_url + path, json={})
        assert r.status_code == 404, path
        assert r.content == b""


def test_correct_token_wrong_subpath_is_not_leaked_as_ok(server):
    r = httpx.get(f"{server.base_url}/{TOKEN}/other")
    assert r.status_code == 404


async def test_search_finds_text_with_stemming_and_locator(server):
    text = text_of(await call(server, "search", query="configuring"))
    assert "books/gardening.pdf" in text
    assert "unit: 2" in text
    assert "label: 'ii'" in text  # roman-numeral front matter
    assert "networking.pdf" in text  # "Configure" stems to the same term
    assert "[[" in text and "]]" in text


async def test_search_bm25_ranks_best_first_and_respects_limit(server):
    text = text_of(await call(server, "search", query="configure OR tomatoes", limit=1))
    assert text.count("locator") == 1
    assert "limit reached" in text


async def test_search_fts5_syntax(server):
    phrase = text_of(await call(server, "search", query='"firewall rules"'))
    assert "networking.pdf" in phrase
    boolean = text_of(await call(server, "search", query="configure NOT router"))
    assert "networking.pdf" not in boolean and "gardening.pdf" in boolean
    prefix = text_of(await call(server, "search", query="tomat*"))
    assert "gardening.pdf" in prefix


async def test_search_accepts_bare_hyphenated_terms(server):
    bare = text_of(await call(server, "search", query="FG-80F"))
    quoted = text_of(await call(server, "search", query='"FG-80F"'))
    assert not quoted.startswith("Invalid") and "networking.pdf" in bare
    assert bare == quoted
    combined = text_of(await call(server, "search", query="MTU mismatch leaf-07"))
    assert "networking.pdf" in combined and "unit: 4" in combined
    mixed = text_of(await call(server, "search", query='"firewall rules" OR leaf-07 NOT FG-80F'))
    assert "unit: 4" in mixed and "unit: 3" not in mixed
    prefix = text_of(await call(server, "search", query="leaf-0*"))
    assert "unit: 4" in prefix


async def test_search_filters_by_glob_and_doc(server):
    only_books = text_of(await call(server, "search", query="configure", path_glob="books/*"))
    assert "gardening.pdf" in only_books and "networking.pdf" not in only_books
    one_doc = text_of(await call(server, "search", query="configure", doc="networking.pdf"))
    assert "networking.pdf" in one_doc and "gardening.pdf" not in one_doc


async def test_search_unknown_doc_and_invalid_query_are_actionable_errors(server):
    bad_doc = await call(server, "search", query="x", doc="nope.pdf")
    assert bad_doc.isError and "Unknown document" in text_of(bad_doc)
    bad_query = await call(server, "search", query='"unterminated')
    assert bad_query.isError and "FTS5" in text_of(bad_query)


async def test_search_no_match_gives_hint(server):
    text = text_of(await call(server, "search", query="zzzzqqq"))
    assert "No matches" in text


async def test_failed_files_do_not_break_search_or_get_indexed(server):
    text = text_of(await call(server, "search", query="secret OR plans OR corrupt"))
    assert "No matches" in text
    text = text_of(await call(server, "search", query="router"))
    assert "networking.pdf" in text


async def test_output_is_truncated_with_narrowing_hint(server, monkeypatch):
    from plib import server as srv
    monkeypatch.setattr(srv, "DEFAULT_CHAR_BUDGET", 150)
    text = text_of(await call(server, "search", query="configure OR configuring OR tomatoes"))
    assert "[truncated" in text and "Narrow" in text


async def test_token_never_appears_in_logs(server, caplog):
    caplog.set_level(logging.DEBUG)
    await call(server, "search", query="router")
    httpx.get(f"{server.base_url}/{TOKEN}/other")
    httpx.get(f"{server.base_url}/wrong/mcp")
    logging.getLogger("plib.test").warning("endpoint %s/mcp", TOKEN)
    assert TOKEN not in caplog.text
    assert TOKEN not in "".join(r.getMessage() for r in caplog.records)


def test_serve_refuses_without_token_and_suggests_one(library):
    r = run_cli("serve", str(library))
    assert r.returncode != 0
    assert "PLIB_TOKEN" in r.stderr and "Suggested" in r.stderr
    assert not (library / ".plib").exists()


def test_serve_refuses_short_token(library):
    r = run_cli("serve", str(library), env={"PLIB_TOKEN": "short"})
    assert r.returncode != 0 and "too short" in r.stderr and "Suggested" in r.stderr


def test_serve_prints_endpoint_url_and_index_lives_in_root(library):
    import socket
    import time
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    env = {**os.environ, "PLIB_TOKEN": TOKEN}
    proc = subprocess.Popen([sys.executable, "-m", "plib.cli", "serve", str(library),
                             "--port", str(port)], env=env, stdout=subprocess.PIPE, text=True)
    try:
        url = None
        for line in proc.stdout:
            if line.startswith("MCP endpoint:"):
                url = line.split(": ", 1)[1].strip()
                break
        assert url == f"http://127.0.0.1:{port}/{TOKEN}/mcp"
        time.sleep(0.5)
        assert httpx.get(f"http://127.0.0.1:{port}/nope").status_code == 404
        assert (library / ".plib" / "index.sqlite").exists()
    finally:
        proc.terminate()
        proc.wait(timeout=10)


CONTENTS_PAGE = "\n".join(
    f"{title} {'.' * 40} {page}" for title, page in [
        ("OSPF LSA throttling", 3), ("OSPF LSA throttling timers", 4),
        ("OSPF LSA throttling limits", 5), ("OSPF LSA throttling examples", 6),
        ("Glossary", 9),
    ]
)
BODY_PAGE = (
    "OSPF LSA throttling slows how often a router floods a changed LSA, so a flapping\n"
    "link cannot overwhelm its neighbours with updates. Configure the start interval\n"
    "first, then the hold interval and the maximum interval."
)


async def test_search_ranks_body_pages_above_contents_pages(library):
    from conftest import make_pdf, start_server
    make_pdf(library / "guide.pdf", [CONTENTS_PAGE, BODY_PAGE])
    make_pdf(library / "toc_only.pdf", [CONTENTS_PAGE.replace("OSPF", "BGP"), "Unrelated"])
    with start_server(library) as running:
        text = text_of(await call(running, "search", query="OSPF LSA throttling"))
        assert text.index("unit: 2") < text.index("unit: 1")
        toc_only = text_of(await call(running, "search", query="BGP LSA throttling"))
        assert "toc_only.pdf" in toc_only and "unit: 1" in toc_only
