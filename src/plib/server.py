from __future__ import annotations

import hmac
import logging

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings

from .config import Config
from .db import connect
from . import query as q

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
        instructions="Search a personal library of PDFs. Results carry a locator (doc, unit, label).",
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
        conn = connect(config.db_path)
        try:
            hits = q.search(conn, query, path_glob=path_glob, doc=doc, limit=limit)
        except q.QueryError as e:
            raise ToolError(str(e)) from e
        finally:
            conn.close()
        return format_hits(hits, limit)

    return mcp


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
