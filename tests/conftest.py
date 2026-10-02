from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
import time
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from pathlib import Path

import pymupdf
import pytest
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from plib.config import load_config
from plib.indexer import index_library
from plib.progress import IndexProgress
from plib.server import build_app
from plib.watcher import LibraryWatcher, WatchTiming

TOKEN = "t" * 40


IMAGE_ONLY = None  # marker: a page holding only a picture, no extractable text


def make_pdf(path: Path, pages: list[str | None], *, toc=None, labels=None, password=None) -> None:
    doc = pymupdf.open()
    for text in pages:
        page = doc.new_page()
        if text is IMAGE_ONLY:
            pixmap = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 200, 120))
            pixmap.set_rect(pixmap.irect, (200, 30, 30))
            page.insert_image(pymupdf.Rect(72, 100, 372, 280), pixmap=pixmap)
        else:
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


GUIDE = """---
title: Guide Title
author: Ada
tags: [zebrameta]
---
Intro text before any heading.

# Install

Install overview.

## Linux

Run the apt installer.

```bash
# fakeheading comment inside a fence
echo hi
```

Still in Linux after the fence.

## macOS

Use brew.

# Usage

Usage text.
"""

OVERSIZED = "# Big\n\n" + "\n\n".join(
    f"para{i:03d} " + ("lorem ipsum " * 60) for i in range(30)
) + "\n"


@pytest.fixture
def markdown_files(library):
    (library / "notes").mkdir()
    (library / "notes" / "guide.md").write_text(GUIDE)
    (library / "notes" / "h1only.markdown").write_text("# Real Title\n\nbody about quokkas\n")
    (library / "plain.md").write_text("Just prose about axolotls.\n\nSecond paragraph.\n")
    (library / "big.md").write_text(OVERSIZED)


@pytest.fixture
def library(tmp_path: Path) -> Path:
    root = tmp_path / "lib"
    (root / "books").mkdir(parents=True)
    make_pdf(
        root / "books" / "gardening.pdf",
        ["Introduction to gardening", "Configuring the irrigation system",
         "Tomatoes need full sun and rich soil", IMAGE_ONLY],
        toc=[[1, "Basics", 1], [2, "Irrigation", 2], [1, "Diagrams", 4]],
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


@contextmanager
def start_server(library: Path, *, background_workers: int | None = None,
                 watch: WatchTiming | None = None):
    """background_workers=None indexes up front; a number indexes in a thread while serving.
    watch starts the live file watcher (after the up-front index) with that timing."""
    config = load_config(library, {"PLIB_TOKEN": TOKEN})
    progress = IndexProgress()
    indexing = None
    if background_workers is None:
        index_library(config)
    else:
        indexing = threading.Thread(
            target=index_library, args=(config, background_workers, progress), daemon=True)
        indexing.start()
    watcher = LibraryWatcher(config, watch) if watch else None
    if watcher:
        watcher.start_observing()
        watcher.start_processing()
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    uv = uvicorn.Server(uvicorn.Config(build_app(config, progress), host="127.0.0.1", port=port,
                                       access_log=False, log_level="warning"))
    thread = threading.Thread(target=uv.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not uv.started and time.time() < deadline:
        time.sleep(0.02)
    assert uv.started
    try:
        yield Server(root=library, base_url=f"http://127.0.0.1:{port}", token=TOKEN)
    finally:
        if watcher:
            watcher.stop()
        if indexing:
            indexing.join(timeout=30)
        uv.should_exit = True
        thread.join(timeout=10)


@pytest.fixture
def server(library: Path):
    with start_server(library) as running:
        yield running
