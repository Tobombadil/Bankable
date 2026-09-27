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
tick, that task walks `data/sources.yaml` and defers a `run_connector` job for every source that
`is_due()` says is due: its `bucket_for_cadence()` matches and, for the annual bucket, the tick
falls in the source's run month.

Release-aware annual (2026-09-27). The annual tick used to fire on 2 January for every annual
source, months before most annual datasets exist (EIA-860's final release lands in September,
GHGRP's in October), so each annual source fetched the year-old file in January and then waited a
year. The annual bucket now ticks on the 2nd of every month, and a source is due only in its run
month: the month *after* its optional `release_month` (1-12) in `data/sources.yaml`, or January
when the field is absent (the previous default, unchanged). The month after rather than the month
of: a release month is known at month granularity and the release day inside it varies (EIA-860
2025 final on 10 September 2026, GHGRP RY2023 on 15 October 2024), so a run on the 2nd of the
release month would almost always precede the release and then wait a year, while a run on the
2nd of the following month follows any in-month release, at a cost of at most a few weeks of
latency on an annual dataset. A release that slips past its month is caught by "Run now".
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
    # The 2nd of every month, 07:37 UTC; `is_due` keeps each annual source to its one run month
    # (module docstring). The 2nd, not the 1st, avoids New Year's Day maintenance windows.
    "annual": "37 7 2 * *",
}

#: The run month of an annual source with no `release_month`: January, as before 2026-09-27.
DEFAULT_ANNUAL_RUN_MONTH = 1

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


def release_month(source: dict[str, Any]) -> int | None:
    """The source's optional `release_month` (1-12, the month its annual data is published), or
    None when absent. Raises `ValueError` for any other value, so a typo in the manifest fails
    `infra/scheduler/test_cadence.py` rather than silently scheduling in the wrong month."""
    value = source.get("release_month")
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 12:
        raise ValueError(f"{source.get('id')}: release_month must be an integer 1-12, got {value!r}")
    return value


def annual_run_month(source: dict[str, Any]) -> int:
    """The month an annual-bucket source runs in: the month after its `release_month` (December
    wraps to January), else `DEFAULT_ANNUAL_RUN_MONTH`."""
    month = release_month(source)
    return DEFAULT_ANNUAL_RUN_MONTH if month is None else month % 12 + 1


def schedule_for_source(source: dict[str, Any]) -> str:
    """The effective cron for one source: its bucket's cron, narrowed to the run month for an
    annual source (for display and tests; the scheduler itself ticks per bucket)."""
    bucket = bucket_for_cadence(str(source.get("cadence", ""))).bucket
    cron = CRON_BY_BUCKET[bucket]
    if bucket != "annual":
        return cron
    minute, hour, day, _month, weekday = cron.split()
    return f"{minute} {hour} {day} {annual_run_month(source)} {weekday}"


def is_due(source: dict[str, Any], bucket: str, month: int) -> bool:
    """Whether a tick of `bucket` in calendar `month` (UTC) should fetch `source`."""
    if bucket_for_cadence(str(source.get("cadence", ""))).bucket != bucket:
        return False
    return bucket != "annual" or annual_run_month(source) == month


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


def safe_id(source_id: str) -> str:
    """A registry id rendered lock-safe (`us.iso.ercot.gen_queue` -> `us-iso-ercot-gen-queue`)."""
    return _NON_ALNUM.sub("-", source_id.strip())


def execution_lock_for(source_id: str) -> str:
    """Procrastinate `lock` shared by every job that touches one source's files and rows (the
    `fetch` and the `load_source` job): jobs holding the same lock never *run* concurrently,
    where `queueing_lock_for` only stops a second one from being *queued*. Together they close
    the overlap the audit named (a load reading a parquet the next fetch is rewriting)."""
    return f"source:{safe_id(source_id)}"


def queueing_lock_for(source_id: str) -> str:
    """A stable per-source lock so a still-running or still-queued fetch is never queued a second
    time by the next tick (docs/20 §4.2: every job is idempotent on its `(type, key)`;
    Procrastinate's `queueing_lock` turns that rule into a database constraint — `defer()` raises
    `AlreadyEnqueued`, caught in `infra/scheduler/app.py`, instead of silently double-running a
    fetch — rather than an application convention that can be forgotten).
    """
    return f"fetch:{safe_id(source_id)}"
