"""Cadence and egress-queue mapping for `data/sources.yaml` entries.

Pure functions only — no I/O, no database, no network — so they are unit-testable without a
Postgres instance (`infra/scheduler/test_cadence.py`).

Cadence strings in the registry are free text (see the `# Field guide` header of
`data/sources.yaml`). Rather than compute an arbitrary poll interval per source (which cannot be
expressed as a Procrastinate `@app.periodic(cron=...)` schedule without one cron per source),
this module buckets every observed cadence string into one of a handful of named, cron-expressible
buckets, always rounding to the **more frequent** bucket on any ambiguity — the same reasoning as
docs/20 §4.2's floor rule for `realtime` ("polled at a floor of 15 min"), generalised: polling a
slow source too often costs a few wasted `unchanged` runs (docs/20 §3.2); polling a fast-changing
one too rarely costs missed change events, which is the product.

`infra/scheduler/app.py` registers one `@app.periodic` task per bucket in `CRON_BY_BUCKET`; each
tick, that task walks `data/sources.yaml` and defers a `run_connector` job for every source whose
`bucket_for_cadence()` matches.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

# Bucket name -> cron expression. Offsets (the non-zero minute/hour/day-of-month values) are
# arbitrary jitter so the five buckets do not all fire at once and stack every source's fetch at
# the top of the hour (docs/20 §4.2 "enqueues `fetch` jobs with jitter").
CRON_BY_BUCKET: dict[str, str] = {
    "15min": "*/15 * * * *",
    "daily": "7 3 * * *",
    "weekly": "13 4 * * 1",  # Monday 04:13 UTC
    "monthly": "21 5 1 * *",  # 1st of the month, 05:21 UTC
    "quarterly": "29 6 1 1,4,7,10 *",  # Jan/Apr/Jul/Oct 1st, 06:29 UTC
    "annual": "37 7 2 1 *",  # Jan 2nd, 07:37 UTC (avoids New Year's Day maintenance windows)
}

# Ordered most-frequent-first: the first keyword found in the (lowercased) cadence string wins,
# which is what gives "twice weekly" -> weekly rather than being missed entirely, and what makes a
# compound string like "quarterly (scorecard); monthly (Generation Information)" resolve to the
# more frequent "monthly" bucket rather than the first-mentioned "quarterly" one.
_KEYWORD_BUCKET: tuple[tuple[str, str], ...] = (
    ("15-min", "15min"),
    ("realtime", "15min"),
    ("continuous", "15min"),
    ("daily", "daily"),
    ("twice weekly", "weekly"),
    ("weekly", "weekly"),
    ("monthly", "monthly"),
    ("quarterly", "quarterly"),
    ("semi-annual", "quarterly"),  # semi-annual/quarterly: the quarterly half is more frequent
    ("biennial", "quarterly"),  # "biennial + amendments": amendments can land anytime; never < quarterly
    ("annual", "annual"),
)

# Cadence strings with no fixed schedule at all (event-driven or unpredictable, e.g. "per-auction").
# Polled on the weekly bucket rather than left unscheduled, so a source is never silently never
# checked (DA-12 "a source without a connector shows as unimplemented, never silently absent" —
# the same principle applied to scheduling rather than registration).
_NO_FIXED_SCHEDULE = {"per-auction", "per-batch", "per-window", "varies"}

DEFAULT_BUCKET = "weekly"


@dataclass(frozen=True)
class BucketDecision:
    bucket: str
    matched_keyword: str | None  # None means the default bucket was applied, not a recognised keyword


def bucket_for_cadence(cadence: str) -> BucketDecision:
    """Map a `data/sources.yaml` `cadence` string to one of `CRON_BY_BUCKET`'s keys."""
    normalised = cadence.strip().lower()

    if normalised in _NO_FIXED_SCHEDULE:
        return BucketDecision(DEFAULT_BUCKET, None)

    for keyword, bucket in _KEYWORD_BUCKET:
        if keyword in normalised:
            return BucketDecision(bucket, keyword)

    # Unrecognised string (e.g. a future cadence value nobody has taught this module about yet):
    # fail safe to the weekly bucket rather than raising, and let the caller log it as a warning so
    # it surfaces in the admin/source-health view (docs/20 §8) rather than being silently wrong.
    return BucketDecision(DEFAULT_BUCKET, None)


_BROWSER_ACCESS_VALUES = {"js_app"}


def queue_for_source(source: dict[str, Any]) -> str:
    """Route a source to the `fetch` or `fetch_browser` Procrastinate queue.

    `data/sources.yaml` does not carry an `egress` field yet (docs/20 §4.3 proposes it as a
    data-engineer addition, DA-12); until it does, this infers the browser pool from `access:
    js_app` (docs/20 §4.3's own examples — ISO-NE IRTT, BLM ePlanning — are JS-rendered sites) and
    prefers an explicit `egress: browser` value the moment the registry gains one, so this
    function needs no change when that field lands.
    """
    egress = source.get("egress")
    if egress == "browser":
        return "fetch_browser"
    if egress in {"plain", "api_key", "residential"}:
        return "fetch"
    return "fetch_browser" if source.get("access") in _BROWSER_ACCESS_VALUES else "fetch"


_NON_ALNUM = re.compile(r"[^a-zA-Z0-9]+")


def queueing_lock_for(source_id: str) -> str:
    """A stable per-source lock so a still-running or still-queued fetch is never queued a second
    time by the next tick (docs/20 §4.2: every job is idempotent on its `(type, key)`;
    Procrastinate's `queueing_lock` turns that rule into a database constraint — `defer()` raises
    `AlreadyEnqueued`, caught in `infra/scheduler/app.py`, instead of silently double-running a
    fetch — rather than an application convention that can be forgotten).
    """
    safe_id = _NON_ALNUM.sub("-", source_id.strip())
    return f"fetch:{safe_id}"
