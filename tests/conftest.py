from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import pymupdf
import pytest
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from plib.config import load_config
from plib.indexer import index_library
from plib.server import build_app

TOKEN = "t" * 40


def make_pdf(path: Path, pages: list[str], *, toc=None, labels=None, password=None) -> None:
    doc = pymupdf.open()
    for text in pages:
        page = doc.new_page()
        page.insert_text((72, 100), text, fontsize=11)
    if toc:
        doc.set_toc(toc)
    if labels:
        doc.set_page_labels(labels)
    if password:
        doc.save(path, encryption=pymupdf.PDF_ENCRYPT_AES_256,
                 owner_pw=password, user_pw=password)
    else:
        doc.save(path)
    doc.close()


@pytest.fixture
def library(tmp_path: Path) -> Path:
    root = tmp_path / "lib"
    (root / "books").mkdir(parents=True)
    make_pdf(
        root / "books" / "gardening.pdf",
        ["Introduction to gardening", "Configuring the irrigation system",
         "Tomatoes need full sun and rich soil"],
        toc=[[1, "Basics", 1], [2, "Irrigation", 2]],
        labels=[{"startpage": 0, "prefix": "", "style": "r", "firstpagenum": 1},
                {"startpage": 2, "prefix": "", "style": "D", "firstpagenum": 1}],
    )
    make_pdf(root / "networking.pdf", ["Configure the router", "Firewall rules and ports"])
    make_pdf(root / "secret.pdf", ["Top secret plans"], password="pw")
    (root / "corrupt.pdf").write_bytes(b"this is not a pdf")
    return root


def run_cli(*args, env=None, cwd=None):
    full_env = {k: v for k, v in os.environ.items() if k != "PLIB_TOKEN"}
    full_env.update(env or {})
    return subprocess.run([sys.executable, "-m", "plib.cli", *args], env=full_env,
                          cwd=cwd, capture_output=True, text=True, timeout=30)


@dataclass
class Server:
    root: Path
    base_url: str
    token: str

    @property
    def mcp_url(self) -> str:
        return f"{self.base_url}/{self.token}/mcp"

    @asynccontextmanager
    async def client(self, url: str | None = None):
        async with streamablehttp_client(url or self.mcp_url) as (r, w, _):
            async with ClientSession(r, w) as session:
                await session.initialize()
                yield session


@pytest.fixture
def server(library: Path):
    config = load_config(library, {"PLIB_TOKEN": TOKEN})
    index_library(config)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    uv = uvicorn.Server(uvicorn.Config(build_app(config), host="127.0.0.1", port=port,
                                       access_log=False, log_level="warning"))
    thread = threading.Thread(target=uv.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not uv.started and time.time() < deadline:
        time.sleep(0.02)
    assert uv.started
    yield Server(root=library, base_url=f"http://127.0.0.1:{port}", token=TOKEN)
    uv.should_exit = True
    thread.join(timeout=10)
