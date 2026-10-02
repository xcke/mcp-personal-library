from __future__ import annotations

import argparse
import os
import sys

import uvicorn

from .config import ConfigError, load_config
from .indexer import index_library
from .server import build_app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="plib")
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve", help="index the library and serve it over MCP (HTTP)")
    serve.add_argument("root", nargs="?", default=".")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)

    try:
        config = load_config(args.root, dict(os.environ))
    except ConfigError as e:
        print(f"plib: {e}", file=sys.stderr)
        return 2

    print(f"Indexing {config.root} ...", flush=True)
    index_library(config)
    app = build_app(config)
    print(f"MCP endpoint: http://{args.host}:{args.port}/{config.token}/mcp", flush=True)
    uvicorn.run(app, host=args.host, port=args.port, access_log=False, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
