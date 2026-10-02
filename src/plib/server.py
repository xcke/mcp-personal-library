from __future__ import annotations

import hmac
import logging
import sqlite3
from collections.abc import Iterator
from typing import Annotated
from contextlib import contextmanager

from mcp.server.fastmcp import FastMCP, Image
from pydantic import Field
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings

from .config import Config
from .progress import IndexProgress
from .db import connect
from .errors import QueryError
from . import browse
from . import query as q
from .read import read_units
from .grep import grep
from .render import MAX_PIXELS, render_page
from .present import format_doc_info, format_locator, format_section_info, format_grep, format_index_status, format_listing, format_outline

DEFAULT_CHAR_BUDGET = 20_000


def install_token_redaction(token: str) -> None:
    previous = logging.getLogRecordFactory()

    def factory(*args, **kwargs):
        record = previous(*args, **kwargs)
        try:
            message = record.getMessage()
        except Exception:
            return record
        if token in message:
            record.msg = message.replace(token, "<token>")
            record.args = None
        return record

    logging.setLogRecordFactory(factory)


def build_mcp(config: Config, progress: IndexProgress) -> FastMCP:
    mcp = FastMCP(
        "plib",
        instructions="Search a personal library of PDFs and Markdown notes. Results carry a locator (doc, unit, label).",
        streamable_http_path="/mcp",
        # The secret path segment is the access control; Host checks would block proxied use.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    @mcp.tool(name="search")
    def search_tool(
        query: str,
        path_glob: str | None = None,
        doc: str | None = None,
        pages: str | None = None,
        limit: int = 20,
    ) -> str:
        """Ranked keyword search (stemmed, bm25) across every page of every document.

        query uses FTS5 syntax: words, "exact phrases", AND/OR/NOT, prefix*.
        path_glob restricts by relative path (e.g. "books/*.pdf"); doc restricts to one
        document (relative path or numeric id). pages restricts to a page/section range:
        physical "10-20,35" or printed labels "label:xii-xv" (labels need doc). Each hit shows a locator
        doc/unit/label and a snippet with matches wrapped in [[ ]].
        """
        with _read_connection(config) as conn:
            hits = q.search(conn, query, path_glob=path_glob, doc=doc, pages=pages, limit=limit)
        return format_hits(hits, limit)

    @mcp.tool(name="grep")
    def grep_tool(
        pattern: str,
        regex: bool = False,
        ignore_case: bool = True,
        context: int = 200,
        path_glob: str | None = None,
        doc: str | None = None,
        pages: str | None = None,
        max_hits: int = 50,
    ) -> str:
        """Exact-string or regex search (like `rg -C`) for identifiers, error codes, part
        numbers and exact phrases that ranked search mangles.

        pattern is literal unless regex=true (Python `re` syntax). Case-insensitive unless
        ignore_case=false. context is the number of characters kept on each side of a match.
        path_glob, doc and pages narrow the search as in `search`. Output starts with hit
        counts per document, then each match as a locator plus excerpt with the match
        wrapped in [[ ]]. Beyond max_hits the response says how much was left out.
        """
        with _read_connection(config) as conn:
            result = grep(conn, pattern, regex=regex, ignore_case=ignore_case, context=context,
                          path_glob=path_glob, doc=doc, pages=pages, max_hits=max_hits)
        return format_grep(result, DEFAULT_CHAR_BUDGET)

    @mcp.tool()
    def list_documents(glob: str | None = None, kind: str | None = None,
                       limit: int = 50, offset: int = 0) -> str:
        """List documents (like `ls`): id, path, kind, title, page/section count, status.

        glob filters by relative path (e.g. "books/*"); kind is "pdf" or "md".
        Failed documents (encrypted, corrupt) are listed with their reason.
        """
        with _read_connection(config) as conn:
            listing = browse.list_documents(conn, glob=glob, kind=kind, limit=limit, offset=offset)
        return format_listing(listing, DEFAULT_CHAR_BUDGET)

    @mcp.tool()
    def doc_info(doc: str) -> str:
        """Show one document's metadata (like `stat`): title, author, PDF metadata or Markdown
        front-matter, page/section count, printed page-label ranges, and low-text pages.

        doc is a path relative to the library root or a numeric id.
        """
        with _read_connection(config) as conn:
            info = browse.doc_info(conn, doc)
        return format_doc_info(info, DEFAULT_CHAR_BUDGET)

    @mcp.tool(name="read")
    def read_tool(
        doc: str,
        from_: Annotated[str, Field(validation_alias="from")],
        to: str | None = None,
        max_chars: int = DEFAULT_CHAR_BUDGET,
        cursor: str | None = None,
    ) -> str:
        """Read the text of pages (PDF) or sections (Markdown), like `sed -n`.

        from is a range such as "10-20,35" (physical pages / section numbers) or
        "label:xii-xv" (printed page labels, PDF only). Give to as the last page when
        from is a single page. Each unit is shown under a header with its page, label
        and heading path. Output beyond max_chars stops with a next_cursor; call again
        with the same doc/from/to plus cursor to continue exactly there.
        """
        spec = f"{from_}-{to}" if to else from_
        with _read_connection(config) as conn:
            result = read_units(conn, doc, spec, max_chars=max_chars, cursor=cursor)
        if result.next_cursor is None:
            return result.text
        return (f"{result.text}\n\n[truncated at max_chars={max_chars}. "
                f"next_cursor: {result.next_cursor}  (repeat the call with the same "
                "doc/from/to and cursor=<next_cursor>)]")

    @mcp.tool(name="render_page")
    def render_page_tool(doc: str, page: str, dpi: int = 110,
                         clip: str | None = None) -> list:
        """Render one PDF page (or a region of it) as a PNG image, for tables, figures,
        diagrams, formulas and multi-column layouts that text extraction garbles.

        page is a physical number like "12" or a printed label like "label:xii". clip
        zooms into a region: "x0,y0,x1,y1" from the page's top-left, in PDF points, or as
        fractions of the page when every value is between 0 and 1 (e.g. "0,0.5,1,1" for the
        bottom half). Images are capped at about 2000 pixels per side; clip to a smaller
        region at a higher dpi to read fine print. PDF only; use read for Markdown.
        """
        with _read_connection(config) as conn:
            rendered = render_page(conn, config.root, config.index_dir, doc, page,
                                   dpi=dpi, clip=clip)
        summary = (f"{doc} page {page}: page size {rendered.page_width_pt:.0f}x"
                   f"{rendered.page_height_pt:.0f} pt; image {rendered.width_px}x"
                   f"{rendered.height_px} px")
        if rendered.capped:
            summary += (f" (reduced to fit {MAX_PIXELS}px; pass clip to render a smaller "
                        "region at full detail)")
        return [Image(data=rendered.png, format="png"), summary]

    @mcp.tool()
    def index_status() -> str:
        """Show indexing progress: total, indexed, pending and failed document counts and
        the file being processed now. Use it when a result you expect is missing: the
        document may simply not be indexed yet.
        """
        with _read_connection(config) as conn:
            counts = browse.index_counts(conn)
        return format_index_status(counts, progress.snapshot())

    @mcp.tool()
    def get_outline(doc: str, max_level: int | None = None) -> str:
        """Show a document's outline: PDF bookmarks or Markdown headings, each with the
        unit (page or section number) it starts at. Pass that unit to other tools.

        max_level limits depth (1 = top level only).
        """
        with _read_connection(config) as conn:
            items = browse.get_outline(conn, doc, max_level)
        return format_outline(items, max_level, DEFAULT_CHAR_BUDGET)

    return mcp


@contextmanager
def _read_connection(config: Config) -> Iterator[sqlite3.Connection]:
    conn = connect(config.db_path)
    try:
        yield conn
    except QueryError as e:
        raise ToolError(str(e)) from e
    finally:
        conn.close()


def format_hits(hits: list[q.Hit], limit: int, budget: int | None = None) -> str:
    budget = budget or DEFAULT_CHAR_BUDGET
    if not hits:
        return "No matches. Try fewer or broader terms, a prefix* term, or OR."
    lines, used, shown = [], 0, 0
    for i, h in enumerate(hits, 1):
        entry = (
            f"{i}. {h.title} — {format_locator(h.doc, h.unit, h.label, h.low_text)}"
            + format_section_info(h.heading_path, h.line_start, h.line_end)
            + f"\n   {h.snippet}"
        )
        if used + len(entry) > budget and shown:
            break
        lines.append(entry)
        used += len(entry) + 1
        shown += 1
    out = "\n".join(lines)
    if shown < len(hits):
        out += (
            f"\n\n[truncated: showed {shown} of {len(hits)} hits to stay within "
            f"{budget} characters. Narrow with path_glob or doc, add terms, or lower limit.]"
        )
    elif len(hits) >= limit:
        out += (
            f"\n\n[limit reached: {limit} hits returned; more may exist. "
            "Narrow with path_glob or doc, or add terms.]"
        )
    return out


class TokenGuard:
    """ASGI wrapper: only /{token}/... reaches the app; anything else is an empty 404."""

    def __init__(self, app, token: str):
        self.app = app
        self.token = token.encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            return await self.app(scope, receive, send)
        if scope["type"] in ("http", "websocket"):
            path = scope.get("path", "")
            segment, _, rest = path.lstrip("/").partition("/")
            if hmac.compare_digest(segment.encode(), self.token):
                scope = dict(scope, path="/" + rest)
                if "raw_path" in scope and scope["raw_path"] is not None:
                    scope["raw_path"] = ("/" + rest).encode()
                return await self.app(scope, receive, send)
        if scope["type"] == "http":
            await send({"type": "http.response.start", "status": 404,
                        "headers": [(b"content-length", b"0")]})
            await send({"type": "http.response.body", "body": b""})


def build_app(config: Config, progress: IndexProgress | None = None):
    install_token_redaction(config.token)
    mcp = build_mcp(config, progress or IndexProgress())
    return TokenGuard(mcp.streamable_http_app(), config.token)
