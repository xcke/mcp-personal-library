from __future__ import annotations

import hmac
import logging
from contextlib import contextmanager

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings

from .config import Config
from .db import connect
from . import browse
from . import query as q
from .present import format_doc_info, format_listing, format_outline

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


def build_mcp(config: Config) -> FastMCP:
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
        limit: int = 20,
    ) -> str:
        """Ranked keyword search (stemmed, bm25) across every page of every document.

        query uses FTS5 syntax: words, "exact phrases", AND/OR/NOT, prefix*.
        path_glob restricts by relative path (e.g. "books/*.pdf"); doc restricts to one
        document (relative path or numeric id). Each hit shows a locator
        doc/unit/label and a snippet with matches wrapped in [[ ]].
        """
        with _read_connection(config) as conn:
            hits = q.search(conn, query, path_glob=path_glob, doc=doc, limit=limit)
        return format_hits(hits, limit)

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
def _read_connection(config: Config):
    conn = connect(config.db_path)
    try:
        yield conn
    except q.QueryError as e:
        raise ToolError(str(e)) from e
    finally:
        conn.close()


def _section_info(h) -> str:
    parts = []
    if h.heading_path:
        parts.append(f"section: {h.heading_path}")
    if h.line_start is not None:
        parts.append(f"lines {h.line_start}-{h.line_end}")
    return f"\n   ({', '.join(parts)})" if parts else ""


def format_hits(hits, limit: int, budget: int | None = None) -> str:
    budget = budget or DEFAULT_CHAR_BUDGET
    if not hits:
        return "No matches. Try fewer or broader terms, a prefix* term, or OR."
    lines, used, shown = [], 0, 0
    for i, h in enumerate(hits, 1):
        label = h.label if h.label else "-"
        entry = (
            f"{i}. {h.title} — locator {{doc: {h.doc!r}, unit: {h.unit}, label: {label!r}}}"
            + ("  [low text: consider render_page]" if h.low_text else "")
            + _section_info(h)
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


def build_app(config: Config):
    install_token_redaction(config.token)
    mcp = build_mcp(config)
    return TokenGuard(mcp.streamable_http_app(), config.token)
