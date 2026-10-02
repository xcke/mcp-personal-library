from __future__ import annotations

import asyncio
import os
import time

import pytest

from conftest import start_server
from test_read import text

WORKERS = 2


@pytest.fixture
def blocked_library(library):
    """A FIFO named like a PDF stalls extraction of that file until something writes to it (it then fails to index, which is fine)."""
    os.mkfifo(library / "slow.pdf")
    yield library
    unblock_waiting_reader(library)


def unblock_waiting_reader(library) -> None:
    """A test that failed before releasing would otherwise leave a worker blocked forever."""
    try:
        os.close(os.open(library / "slow.pdf", os.O_WRONLY | os.O_NONBLOCK))
    except OSError:
        pass  # no reader waiting: nothing is blocked


def release(library, content: bytes) -> None:
    with open(library / "slow.pdf", "wb") as writer:
        writer.write(content)


async def wait_for(server, predicate, timeout=30.0) -> str:
    deadline = time.time() + timeout
    while True:
        out = await text(server, "index_status")
        if predicate(out) or time.time() > deadline:
            return out
        await asyncio.sleep(0.1)


async def test_requests_are_answered_while_indexing_is_in_progress(blocked_library):
    with start_server(blocked_library, background_workers=WORKERS) as server:
        out = await wait_for(server, lambda o: "indexed: 2" in o)
        assert "state: indexing" in out
        assert "pending: 1" in out
        assert "current file: slow.pdf" in out
        assert "total: 5" in out and "failed: 2" in out

        assert "networking.pdf" in await text(server, "search", query="router")
        assert "networking.pdf" in await text(server, "list_documents")
        assert "slow.pdf" not in await text(server, "list_documents")

        release(blocked_library, b"not a pdf")
        out = await wait_for(server, lambda o: "state: idle" in o)
        assert "pending: 0" in out and "current file" not in out
        assert "failed: 3" in out and "total: 5" in out


async def test_index_status_when_idle(library):
    with start_server(library) as server:
        out = await text(server, "index_status")
    assert "state: idle" in out
    assert "total: 4" in out and "indexed: 2" in out and "failed: 2" in out
    assert "pending: 0" in out
