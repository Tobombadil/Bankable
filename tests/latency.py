"""Helpers for the latency-budget tests (docs/04 D-13): time the endpoint, not the test process.

Measured 2026-10-07 on the full core suite (`coverage run -m pytest tests pipeline services infra`):
by the time `tests/test_api_assets_lines.py`'s timed call runs, the test process tracks ~3.57 million
objects, of which only ~470,000 were frozen by the API's startup `gc.freeze()`
(`services/api/gc_tuning.py`). One full (generation-2) collection over the rest took 2.44 s. When it
landed inside a timed call, a 157 ms warm request measured 2.47-2.51 s and failed its 2 s budget (two
full-suite runs in a row); when it landed elsewhere, the same request measured 208-284 ms. That
collection is the cost of every earlier test's leftovers, not of the request: a serving API process
freezes its startup heap and carries no such backlog. So each timed call starts from a collected heap.
"""

from __future__ import annotations

import gc


def settle_heap() -> None:
    """Run the cyclic collector now, so a pending full collection is not billed to the next timed call."""
    gc.collect()
