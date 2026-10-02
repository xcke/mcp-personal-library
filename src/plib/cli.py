from __future__ import annotations

import argparse
import logging
import os
import sys
import threading

import uvicorn

from .config import Config, ConfigError, load_config
from .indexer import IndexProgress, index_library, library_status
from .server import build_app


def format_status(s: dict) -> str:
    lines = [
        f"documents: {s['documents']}",
        f"indexed: {s['indexed']}",
        f"failed: {len(s['failed'])}",
        f"pending: {s['pending']}",
    ]
    if s["removed_on_disk"]:
        lines.append(f"missing from disk (removed on next index): {s['removed_on_disk']}")
    if s["failed"]:
        lines.append("failed files:")
        lines += [f"  {path}: {reason}" for path, reason in s["failed"]]
    return "\n".join(lines)


def _index_in_background(config: Config, workers: int | None, progress: IndexProgress) -> None:
    print(f"Indexing {config.root} in the background ...", flush=True)
    try:
        print(f"Index complete: {index_library(config, workers, progress).summary()}", flush=True)
    except Exception:  # the server must keep answering even if a pass dies
        logging.getLogger("plib.indexer").exception("Background indexing failed")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="plib")
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve", help="index the library and serve it over MCP (HTTP)")
    serve.add_argument("root", nargs="?", default=".")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    for name, help_ in (("index", "index the library without serving"),
                        ("status", "show index totals, pending work and failed files")):
        p = sub.add_parser(name, help=help_)
        p.add_argument("root", nargs="?", default=".")
    for name in ("serve", "index"):
        sub.choices[name].add_argument(
            "--workers", type=int, default=None,
            help="extraction processes (default: CPU count - 1)")
    args = parser.parse_args(argv)
    if getattr(args, "workers", None) is not None and args.workers < 1:
        parser.error("--workers must be at least 1")

    try:
        config = load_config(args.root, dict(os.environ), require_token=args.command == "serve")
    except ConfigError as e:
        print(f"plib: {e}", file=sys.stderr)
        return 2

    if args.command == "status":
        print(format_status(library_status(config)))
        return 0
    if args.command == "index":
        print(f"Indexing {config.root} ...", flush=True)
        print(f"Index complete: {index_library(config, args.workers).summary()}", flush=True)
        return 0
    progress = IndexProgress()
    threading.Thread(target=_index_in_background, args=(config, args.workers, progress),
                     name="indexer", daemon=True).start()
    app = build_app(config, progress)
    print(f"MCP endpoint: http://{args.host}:{args.port}/{config.token}/mcp", flush=True)
    uvicorn.run(app, host=args.host, port=args.port, access_log=False, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
