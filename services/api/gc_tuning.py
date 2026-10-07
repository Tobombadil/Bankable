"""Freeze the API process's startup heap out of the cyclic garbage collector (docs/04 D-13).

Measured 2026-10-07 on the full dev store (docs/CHANGELOG.md, 2026-10-07 geo latency):
after the import of every route module the API process tracks ~250,000 container objects, and one
full (generation 2) collection over them costs 120-170 ms on the dev VM. A national `GET
/v1/proposals/geo` allocates enough short-lived rows to trigger one in most calls, so a third of
that call was the collector re-walking objects that live as long as the process (functions,
classes, SQLAlchemy and FastAPI metadata) and can never be garbage.

`gc.freeze()` moves every object tracked at that moment to a permanent generation that later
collections skip (https://docs.python.org/3/library/gc.html#gc.freeze). Objects created afterwards
are collected as before, and reference counting still frees anything, frozen or not, the moment it
is unreachable; the only thing given up is the cyclic collection of a startup object that later
becomes part of an unreachable cycle, which nothing at startup is expected to do. A full collection
runs first, so no garbage already present is frozen.

Once per process: the API runs one uvicorn worker per container (`infra/entrypoint.py`), and the
test suite starts the app many times in one process, where only the first start pays the
collection.
"""

from __future__ import annotations

import gc
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

_frozen = False


def freeze_startup_heap() -> bool:
    """Collect, then freeze every tracked object, once per process. True when this call froze."""
    global _frozen
    if _frozen:
        return False
    gc.collect()
    gc.freeze()
    _frozen = True
    return True


@asynccontextmanager
async def lifespan(_app: Any) -> AsyncIterator[None]:
    """The API app's lifespan: freeze after every module is imported, before the first request."""
    freeze_startup_heap()
    yield
