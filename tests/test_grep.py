from __future__ import annotations

import sqlite3

from conftest import Server, start_server, make_pdf
from test_read import error, text


LEGACY_SCHEMA_DOWNGRADE = """
DROP TRIGGER units_ai; DROP TRIGGER units_ad; DROP TRIGGER units_au; DROP TABLE units_tri;
CREATE TRIGGER units_ai AFTER INSERT ON units BEGIN
    INSERT INTO units_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER units_ad AFTER DELETE ON units BEGIN
    INSERT INTO units_fts(units_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;
CREATE TRIGGER units_au AFTER UPDATE ON units BEGIN
    INSERT INTO units_fts(units_fts, rowid, text) VALUES ('delete', old.id, old.text);
    INSERT INTO units_fts(rowid, text) VALUES (new.id, new.text);
END;
"""


async def test_literal_grep_returns_locator_and_highlighted_context(server):
    out = await text(server, "grep", pattern="irrigation", context=4)
    assert "locator {doc: 'books/gardening.pdf', unit: 2, label: 'ii'}" in out
    assert "the [[irrigation]] sys" in out


async def test_literal_is_case_insensitive_by_default_and_not_a_regex(server):
    assert "[[Tomatoes]] need" in await text(server, "grep", pattern="TOMATOES", context=5)
    assert "No matches" in await text(server, "grep", pattern="to.atoes")


async def test_case_sensitive_matching(server):
    assert "No matches" in await text(server, "grep", pattern="firewall", ignore_case=False)
    assert "[[Firewall]]" in await text(server, "grep", pattern="Firewall", ignore_case=False,
                                        context=5)


async def test_short_patterns_still_match(server):
    out = await text(server, "grep", pattern="ou", doc="networking.pdf", context=3)
    assert "[[ou]]" in out


async def test_regex_with_extractable_literal(server):
    out = await text(server, "grep", pattern=r"Firewall\s+rules", regex=True, context=0)
    assert "[[Firewall rules]]" in out


async def test_regex_without_literal_scans_filtered_units(server):
    out = await text(server, "grep", pattern=r"\b[a-z]+ing\b", regex=True, context=0,
                     doc="books/gardening.pdf")
    assert "[[Configuring]]" in out


async def test_regex_alternation_is_not_wrongly_prefiltered(server):
    out = await text(server, "grep", pattern="Firewall|Tomatoes", regex=True, context=0)
    assert "networking.pdf" in out and "gardening.pdf" in out


async def test_invalid_regex_gives_clear_error(server):
    message = await error(server, "grep", pattern="(unclosed", regex=True)
    assert "Invalid regex" in message and "regex=false" in message


async def test_hit_counts_per_document(server):
    out = await text(server, "grep", pattern="config")
    assert "2 hits in 2 documents" in out
    assert "books/gardening.pdf: 1" in out and "networking.pdf: 1" in out


async def test_filters_by_doc_glob_and_pages(server):
    assert "gardening" not in await text(server, "grep", pattern="config", doc="networking.pdf")
    assert "gardening" not in await text(server, "grep", pattern="config", path_glob="net*")
    assert "No matches" in await text(server, "grep", pattern="irrigation",
                                      doc="books/gardening.pdf", pages="3-4")
    assert "unit: 2" in await text(server, "grep", pattern="irrigation",
                                   doc="books/gardening.pdf", pages="label:ii")


async def test_max_hits_truncation_reports_remainder(server):
    out = await text(server, "grep", pattern="o", max_hits=2, context=1)
    assert "showing 2 of" in out and "more hits in" in out and "Narrow with" in out
    assert "documents:" in out


async def test_markdown_matches_show_section_and_lines(markdown_files, server):
    out = await text(server, "grep", pattern="apt installer", doc="notes/guide.md")
    assert "Install > Linux" in out and "lines" in out


async def test_unknown_doc_is_an_error(server):
    assert "Unknown document" in await error(server, "grep", pattern="x", doc="nope.pdf")


async def test_grep_follows_file_changes(library):
    with start_server(library) as running:
        assert "No matches" in await text(running, "grep", pattern="zebra")
    make_pdf(library / "networking.pdf", ["Zebra crossing rules"])
    with start_server(library) as running:
        assert "[[Zebra]]" in await text(running, "grep", pattern="zebra", context=0)
        assert "No matches" in await text(running, "grep", pattern="Firewall")
    (library / "networking.pdf").unlink()
    with start_server(library) as running:
        assert "No matches" in await text(running, "grep", pattern="zebra")


async def test_existing_index_without_trigram_table_is_upgraded(library):
    with start_server(library):
        pass
    conn = sqlite3.connect(library / ".plib" / "index.sqlite")
    conn.executescript(LEGACY_SCHEMA_DOWNGRADE)
    conn.commit()
    conn.close()
    with start_server(library) as running:
        assert "[[irrigation]]" in await text(running, "grep", pattern="irrigation", context=0)
