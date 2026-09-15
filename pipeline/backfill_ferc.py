#!/usr/bin/env python3
"""FERC filer-name backfill + measurement runner (Sprint 3 item 5, `docs/00-PLAN.md`).

The 2026-09-12 docket-linkage measurement (`pipeline/link_dockets.py`, `docs/00-PLAN.md`'s
"Docket linkage measured" decision row) ran FERC eLibrary over its default 30-day window and
found 1 of 1,673 active ISO queue records (0.06%) gained a link — "FERC search descriptions
rarely name a queue id or project, so the description field is a weak key; a full-text or
filer-name backfill over years, not 30 days, is the honest next test before docket linkage is
counted in M-1." This script is that test.

What it does, live (never against fixtures — this is a measurement, not a unit test):

1. Runs the three reachable ISO generation-queue connectors (ERCOT, CAISO, NYISO; SPP/ISO-NE/PJM/
   MISO are gated or unimplemented per `data/sources.yaml`, `CLAUDE.md`) to get the current queue.
2. Runs `us.ferc.elibrary` over `--years` trailing one-year windows (default 3, most recent
   first), using the `.window` override added to the connector for this sprint item instead of
   its default 30-day cadence. `filterDate` is a no-op server-side (the connector's own
   docstring), so a wider window means more `ER<yy>`/`CP<yy>` query-years, each paged the same as
   any other run — pages and requests are measured per window and reported, not estimated.
   Time-boxed to about `TIME_BUDGET_S` (20 minutes) of wall time across the FERC windows; a
   window not yet started when the budget is spent is recorded `skipped_time_budget` and the
   measurement reports exactly what was fetched, not what was requested.
3. Runs `pipeline.link_dockets.run` over the concatenated result — both methods (explicit
   description/queue-id match, and the filer-name method, i.e. `_sponsor_matches`, this sprint
   item's focus) stay active exactly as `docs/22` §10 specified; nothing about `link_dockets.run`
   changed to make this script possible except the year-scoped dedupe/filter helpers `link_dockets`
   itself gained (`dedupe_filings_per_docket`, `_filer_index`, `docket_year`).
4. Reports: the link rate among all active queue records and among `contracted`/
   `under_construction` records specifically (`docs/00-PLAN.md`'s recalibrated M-1 bar), a
   per-ISO breakdown, a `--sample-size` (default 50) random sample of proposed links for hand
   precision review (each row is written with a `verdict` field left for the reviewer to fill in;
   this script does not itself decide precision), request counts, wall time, and the date — to
   stdout and to `data/probes/ferc-backfill-<date>.json`.

Usage:
    .venv/bin/python pipeline/backfill_ferc.py [--years 3] [--threshold 85] [--sample-size 50]
                                                [--seed 20260913] [--out data/probes/...]
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import sys
import time
from typing import Any

import pandas as pd

from pipeline.connectors.base import Connector, ConnectorError, RawSnapshot
from pipeline.connectors.http import HttpBlocked, HttpFailed, PoliteSession
from pipeline.connectors.registry import Registry
from pipeline.link_dockets import ACTIVE_STATES, LINK_COLUMNS, active_link_stats
from pipeline.link_dockets import run as link_dockets_run

ROOT = pathlib.Path(__file__).resolve().parents[1]
PROBES = ROOT / "data" / "probes"

FERC_SOURCE_ID = "us.ferc.elibrary"
#: SPP/ISO-NE are `reuse: restricted`, PJM/MISO are `reuse: restricted`/`unknown` (`data/sources.yaml`,
#: `CLAUDE.md`) — none register without `allow_restricted=True`, which this script never passes.
ISO_SOURCE_IDS: tuple[str, ...] = (
    "us.iso.ercot.gen_queue",
    "us.iso.caiso.gen_queue",
    "us.iso.nyiso.gen_queue",
)
#: `docs/00-PLAN.md`'s recalibrated M-1 bar: link rate among records at this stage, not all active.
CONTRACTED_STATES: set[str] = {"contracted", "under_construction"}
#: "stop early with a partial measurement if it would exceed about 20 minutes of wall time".
TIME_BUDGET_S = 20 * 60


def _out(line: str = "") -> None:
    """`print()` triggers ruff T20 and `backfill_ferc.py` has no pyproject exemption (this
    task's write allowlist does not include pyproject.toml), so summaries go through here."""
    sys.stdout.write(line + "\n")


def _fetch_iso(source_id: str, registry: Registry) -> tuple[pd.DataFrame, dict[str, Any]]:
    """One live fetch/parse/normalize of an ISO queue connector, in memory — no `data/normalized`
    write (this script's write allowlist is `data/probes/` only; the coordinator's ordinary runs
    already persist these sources through `pipeline.connectors.runner.run`)."""
    src = registry.get(source_id)
    http = PoliteSession(rate_limits={src.host: src.max_rps})
    connector: Connector = registry.instantiate(source_id, http=http)
    t0 = time.monotonic()
    status = "ok"
    df = pd.DataFrame()
    try:
        raw = connector.fetch()
        rows = connector.parse(raw)
        df = connector.normalize(rows, raw)
    except (ConnectorError, HttpBlocked, HttpFailed) as e:
        status = f"failed: {e!r}"[:300]
    meta = {
        "source_id": source_id,
        "status": status,
        "rows": len(df),
        "requests_made": http.requests_made,
        "elapsed_s": round(time.monotonic() - t0, 2),
    }
    return df, meta


def _year_windows(years: int, today: dt.date) -> list[tuple[dt.date, dt.date]]:
    """`years` trailing 365-day windows ending `today`, most recent first — so a time-boxed
    partial run keeps the years likeliest to carry live, still-open dockets."""
    return [
        (today - dt.timedelta(days=365 * (i + 1)), today - dt.timedelta(days=365 * i)) for i in range(years)
    ]


def fetch_ferc_backfill(
    years: int, registry: Registry, time_budget_s: float = TIME_BUDGET_S
) -> tuple[pd.DataFrame, list[dict[str, Any]]]:
    """Run `us.ferc.elibrary` over `years` trailing one-year windows via the `.window` override,
    time-boxed to `time_budget_s` total wall time. Returns the concatenated, deduplicated
    normalised frame and a per-window measurement record (status/rows/pages/queries/requests/
    elapsed) — a window skipped for budget reasons is recorded, never silently dropped."""
    src = registry.get(FERC_SOURCE_ID)
    http = PoliteSession(rate_limits={src.host: src.max_rps})
    connector: Connector = registry.instantiate(FERC_SOURCE_ID, http=http)
    today = dt.datetime.now(dt.UTC).date()
    windows = _year_windows(years, today)
    frames: list[pd.DataFrame] = []
    per_window: list[dict[str, Any]] = []
    t_start = time.monotonic()
    for start, end in windows:
        if time.monotonic() - t_start > time_budget_s:
            per_window.append(
                {"window": [start.isoformat(), end.isoformat()], "status": "skipped_time_budget"}
            )
            continue
        connector.window = (start, end)  # type: ignore[attr-defined]
        t0 = time.monotonic()
        status = "ok"
        df = pd.DataFrame()
        raw: RawSnapshot | None = None
        try:
            raw = connector.fetch()
            rows = connector.parse(raw)
            df = connector.normalize(rows, raw)
            frames.append(df)
        except (ConnectorError, HttpBlocked, HttpFailed) as e:
            status = f"failed: {e!r}"[:300]
        per_window.append(
            {
                "window": [start.isoformat(), end.isoformat()],
                "status": status,
                "rows": len(df),
                "pages": raw.meta.get("pages") if raw is not None else None,
                "queries": raw.meta.get("queries") if raw is not None else None,
                "requests_made": raw.requests_made if raw is not None else 0,
                "elapsed_s": round(time.monotonic() - t0, 2),
            }
        )
    docs = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    if not docs.empty and "record_id" in docs.columns:
        docs = docs.drop_duplicates(subset=["record_id"]).reset_index(drop=True)
    return docs, per_window


def _link_stats_json(
    iso: pd.DataFrame, links: pd.DataFrame, states: set[str] | None = None
) -> dict[str, Any]:
    """`active_link_stats` JSON-safe, optionally restricted to a subset of lifecycle states first
    (`docs/00-PLAN.md`'s recalibrated M-1 bar: `contracted`/`under_construction` only)."""
    scoped = iso[iso["lifecycle_state"].isin(states)] if states is not None else iso
    stats = active_link_stats(scoped, links)
    per_source = stats["per_source"]
    return {
        "n_active": stats["n_active"],
        "n_linked": stats["n_linked"],
        "rate_pct": round(stats["rate_pct"], 2),
        "per_source": (
            per_source.reset_index().rename(columns={"sum": "linked", "count": "total"}).to_dict("records")
            if not per_source.empty
            else []
        ),
    }


def precision_sample(links: pd.DataFrame, sample_size: int, seed: int) -> list[dict[str, Any]]:
    """Up to `sample_size` links, chosen deterministically (`seed`) for hand review — `docs/00-
    PLAN.md`'s brief: "precision estimate on a random sample of 50 proposed links you inspect by
    hand". This function only draws the sample and carries every field a reviewer needs
    (rationale, both sides' names, docket refs); it never assigns a verdict itself — that is a
    human judgement recorded in the output's `verdict` field, one line each, and in `docs/22` §14.
    """
    if links.empty:
        return []
    n = min(sample_size, len(links))
    sample = links.sample(n=n, random_state=seed)
    cols = [c for c in LINK_COLUMNS if c in sample.columns]
    rows: list[dict[str, Any]] = sample[cols].to_dict("records")
    for row in rows:
        row["verdict"] = "unreviewed"
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--years", type=int, default=3)
    ap.add_argument("--threshold", type=float, default=85.0)
    ap.add_argument("--sample-size", type=int, default=50)
    ap.add_argument("--seed", type=int, default=20260913)
    ap.add_argument("--time-budget-s", type=float, default=TIME_BUDGET_S)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    wall_t0 = time.monotonic()
    registry = Registry()
    date_str = dt.datetime.now(dt.UTC).date().isoformat()

    _out(f"FERC filer-name backfill — {date_str}, years={args.years}, threshold={args.threshold}")
    _out("\nfetching ISO queues live:")
    iso_frames: list[pd.DataFrame] = []
    iso_meta: list[dict[str, Any]] = []
    for source_id in ISO_SOURCE_IDS:
        df, meta = _fetch_iso(source_id, registry)
        iso_frames.append(df)
        iso_meta.append(meta)
        _out(
            f"  {meta['status']:<10} {meta['rows']:>5} rows  {meta['requests_made']:>3} reqs  "
            f"{meta['elapsed_s']:>6.1f}s  {source_id}"
        )
    non_empty = [f for f in iso_frames if not f.empty]
    iso = pd.concat(non_empty, ignore_index=True) if non_empty else pd.DataFrame()

    _out(
        f"\nfetching FERC eLibrary over {args.years} year(s) (window override, time-boxed "
        f"{args.time_budget_s:.0f}s):"
    )
    docs, ferc_per_window = fetch_ferc_backfill(args.years, registry, args.time_budget_s)
    for w in ferc_per_window:
        if w["status"] == "skipped_time_budget":
            _out(f"  skipped (time budget)         {w['window'][0]}..{w['window'][1]}")
        else:
            _out(
                f"  {w['status']:<10} {w['rows']:>5} rows  {w['pages']!s:>4} pages  "
                f"{w['requests_made']:>3} reqs  {w['elapsed_s']:>6.1f}s  {w['window'][0]}..{w['window'][1]}"
            )

    ferc_requests = sum(w.get("requests_made") or 0 for w in ferc_per_window)
    iso_requests = sum(m["requests_made"] for m in iso_meta)

    _out(f"\nISO queue records: {len(iso):,}   FERC documents (deduped): {len(docs):,}")

    links = (
        link_dockets_run(docs, iso, threshold=args.threshold)
        if not iso.empty
        else pd.DataFrame(columns=LINK_COLUMNS)
    )
    method_counts = links["method"].value_counts().to_dict() if len(links) else {}
    _out(f"links: {len(links):,}   by method: {method_counts}")

    _empty_stats = {"n_active": 0, "n_linked": 0, "rate_pct": 0.0}
    overall = _link_stats_json(iso, links) if not iso.empty else _empty_stats
    contracted = _link_stats_json(iso, links, states=CONTRACTED_STATES) if not iso.empty else _empty_stats
    _out(f"\nACTIVE (non-terminal): {overall['n_linked']}/{overall['n_active']} ({overall['rate_pct']}%)")
    _out(
        f"CONTRACTED/UNDER_CONSTRUCTION: {contracted['n_linked']}/{contracted['n_active']} "
        f"({contracted['rate_pct']}%)"
    )

    sample = precision_sample(links, args.sample_size, args.seed)
    _out(f"\nprecision sample: {len(sample)} rows (verdict field left 'unreviewed' for hand review)")

    wall_time_s = round(time.monotonic() - wall_t0, 2)
    result = {
        "date": date_str,
        "years": args.years,
        "threshold": args.threshold,
        "active_states": sorted(ACTIVE_STATES),
        "contracted_states": sorted(CONTRACTED_STATES),
        "iso": iso_meta,
        "ferc": {
            "source_id": FERC_SOURCE_ID,
            "per_window": ferc_per_window,
            "documents_deduped": len(docs),
            "requests_total": ferc_requests,
        },
        "links": {
            "total": len(links),
            "by_method": {str(k): int(v) for k, v in method_counts.items()},
        },
        "active_link_rate": overall,
        "contracted_link_rate": contracted,
        "precision_sample": sample,
        "request_budget": {
            "iso_requests": iso_requests,
            "ferc_requests": ferc_requests,
            "total_requests": iso_requests + ferc_requests,
        },
        "wall_time_s": wall_time_s,
    }

    out_path = pathlib.Path(args.out) if args.out else PROBES / f"ferc-backfill-{date_str}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, indent=1, ensure_ascii=False), encoding="utf-8")
    _out(f"\nwall time: {wall_time_s:.1f}s   total requests: {iso_requests + ferc_requests}")
    _out(f"wrote {out_path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
