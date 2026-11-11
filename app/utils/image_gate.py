"""One cap on image decoding for the whole API (v1.17.2).

Decoding and re-encoding an upload is CPU- and memory-hungry (an RGBA copy of a 12 megapixel image is ~48 MB, and
Pillow holds more than one copy while it works). Ticket photos always had a cap of two at a time; the shop logo and the
signature did not, so a burst of uploads (or one stolen owner token in a loop) could pile up hundreds of megabytes.
Photos, the logo and the signature now share the same gate: at most MAX_CONCURRENT_JOBS images are processed at once
across the whole worker process, the rest wait their turn.
"""
from __future__ import annotations

import asyncio
from typing import Callable, TypeVar

MAX_CONCURRENT_JOBS = 2

T = TypeVar("T")
_gate: tuple[asyncio.AbstractEventLoop, asyncio.Semaphore] | None = None


def _semaphore() -> asyncio.Semaphore:
    global _gate
    loop = asyncio.get_running_loop()
    if _gate is None or _gate[0] is not loop:  # a new event loop (e.g. each test client) gets a fresh semaphore
        _gate = (loop, asyncio.Semaphore(MAX_CONCURRENT_JOBS))
    return _gate[1]


async def run_limited(func: Callable[..., T], *args) -> T:
    """`func(*args)` in a worker thread, with at most MAX_CONCURRENT_JOBS image jobs running at once."""
    async with _semaphore():
        return await asyncio.to_thread(func, *args)
