"""CLI: `python -m services.social <command>` (task brief item 5).

Commands:
    draft --events <parquet> [--records <parquet>] [--queue-path PATH]
        Reads a change-events parquet and writes eligible drafts into the review queue.
        Accepts either `pipeline/diff.py`'s prototype row shape (event_type, record_id,
        source_id, field, before, after, observed_at -- joined here against `--records`, the
        canonical snapshot, and `data/sources.yaml` for provenance) or a parquet already shaped
        as `editorial.SocialEvent` columns (the target contract once the real event store
        exists -- docs/21 §3.10).
    review list [--channel C] [--status S]
    review approve <draft_id> --reviewer <email>
    review reject <draft_id> --reviewer <email> --reason <code>
    dry-run <bluesky|linkedin|x>
        Runs every approved/scheduled draft on that channel through its publisher's dry run.
    report
        Prints the docs/32 §6.2 weekly KPI template, filled from the queue where the queue has
        the answer and zero everywhere metrics collection has not been built yet (no post has
        ever actually gone out -- Sprint 2 stops before that).

Never sends anything to a real network (CLAUDE.md guardrails; docs/32 §4.6 -- no channel has
graduated, and none will from this queue alone).
"""

from __future__ import annotations

import argparse
import datetime as dt
import pathlib
import sys
from typing import Any

import pandas as pd
import yaml

from services.social import editorial
from services.social import queue as queue_mod
from services.social.models import REJECT_REASONS
from services.social.publishers import PUBLISHERS

ROOT = pathlib.Path(__file__).resolve().parents[2]
SOURCES_YAML = ROOT / "data" / "sources.yaml"
DEFAULT_QUEUE_PATH = pathlib.Path(__file__).resolve().parent / "var" / "queue.json"

#: docs/00-PLAN.md decisions log: "public-tier lag is 7 days for opportunities and 14 days for
#: supply rows". Opportunity/funding events post live (docs/32 §3.1) so they carry no lag notice
#: regardless of this value; it only reaches proposal events.
LAG_DAYS = {"proposal": 14, "opportunity": 7}

_DIFF_COLUMNS = {"event_type", "record_id", "source_id", "field", "before", "after", "observed_at"}


def _load_source_meta() -> dict[str, dict[str, str]]:
    """Read-only lookup into `data/sources.yaml` for provenance (name, url, reuse class). This
    package never writes that file -- CLAUDE.md/task scope reserve it to the ingest side."""
    if not SOURCES_YAML.exists():
        return {}
    data = yaml.safe_load(SOURCES_YAML.read_text())
    return {
        s["id"]: {
            "name": s.get("name", s["id"]),
            "url": s.get("url", ""),
            "reuse_class": s.get("reuse", "unknown"),
        }
        for s in data.get("sources", [])
    }


def _page_url_for(row: dict[str, Any]) -> str:
    subject_type = editorial.infer_subject_type(str(row["source_id"]))
    kind = "opportunities" if subject_type == "opportunity" else "proposals"
    # "infraque.com" is a literal placeholder (repo convention, CLAUDE.md/task brief), not an
    # f-string substitution -- kept out of the f-string itself so the double braces survive.
    return "https://infraque.com" + f"/{kind}/{row['record_id']}"


def _lag_days_for(row: dict[str, Any]) -> int | None:
    subject_type = editorial.infer_subject_type(str(row["source_id"]))
    return LAG_DAYS[subject_type] if subject_type == "proposal" else None


def _str_keyed(row: pd.Series[Any]) -> dict[str, Any]:
    """`Series.to_dict()` types its keys as `Hashable`; every caller here needs plain `str`
    keys (a parquet column name always is one) to pass as `**kwargs` or into a `dict[str, Any]`."""
    return {str(k): v for k, v in row.to_dict().items()}


def read_events_parquet(
    events_path: pathlib.Path, records_path: pathlib.Path | None
) -> list[editorial.SocialEvent]:
    df = pd.read_parquet(events_path)
    if _DIFF_COLUMNS.issubset(df.columns):
        records: dict[str, dict[str, Any]] = {}
        if records_path is not None:
            rdf = pd.read_parquet(records_path)
            records = {str(r["record_id"]): _str_keyed(r) for _, r in rdf.iterrows()}
        source_meta = _load_source_meta()
        rows = [_str_keyed(row) for _, row in df.iterrows()]
        return queue_mod.load_events_from_diff_frame(
            rows,
            records,
            source_meta=source_meta,
            page_url_for=_page_url_for,
            lag_days_for=_lag_days_for,
        )

    # "rich" shape: one row per SocialEvent, columns named after its fields.
    events = []
    date_fields = {"event_date", "deadline_date"}
    for _, row in df.iterrows():
        data = {k: v for k, v in _str_keyed(row).items() if pd.notna(v)}
        for field in date_fields:
            if field in data:
                data[field] = pd.Timestamp(data[field]).date()
        if "retrieved_at" in data:
            data["retrieved_at"] = pd.Timestamp(data["retrieved_at"]).to_pydatetime()
        events.append(editorial.SocialEvent(**data))
    return events


def cmd_draft(args: argparse.Namespace) -> int:
    records_path = pathlib.Path(args.records) if args.records else None
    events = read_events_parquet(pathlib.Path(args.events), records_path)
    drafts = editorial.draft_events(events)
    q = queue_mod.ReviewQueue(args.queue_path)
    created, duplicates = 0, 0
    by_channel: dict[str, int] = {}
    for draft in drafts:
        try:
            q.add_draft(draft)
        except queue_mod.DuplicateDraft as exc:
            duplicates += 1
            print(f"skip {draft.channel}/{draft.event_type} for {draft.subject_id}: {exc.reason}")
            continue
        created += 1
        by_channel[draft.channel] = by_channel.get(draft.channel, 0) + 1

    print(
        f"\n{len(events)} events read, {len(drafts)} eligible drafts rendered, "
        f"{created} queued, {duplicates} suppressed as duplicates"
    )
    for channel, n in sorted(by_channel.items()):
        print(f"  {channel}: {n}")
    return 0


def cmd_review_list(args: argparse.Namespace) -> int:
    q = queue_mod.ReviewQueue(args.queue_path)
    drafts = q.list_drafts(channel=args.channel, status=args.status)
    if not drafts:
        print("(no drafts match)")
        return 0
    for d in drafts:
        flag = "OK" if (d.validation and d.validation.passed) else "FAIL"
        print(f"{d.id}  [{d.channel:8s}] [{d.status:9s}] [{flag}]  {d.event_type}  {d.body[:70]!r}")
    return 0


def cmd_review_approve(args: argparse.Namespace) -> int:
    q = queue_mod.ReviewQueue(args.queue_path)
    draft = q.approve(args.draft_id, args.reviewer)
    print(f"approved {draft.id} ({draft.channel}/{draft.event_type})")
    return 0


def cmd_review_reject(args: argparse.Namespace) -> int:
    q = queue_mod.ReviewQueue(args.queue_path)
    draft = q.reject(args.draft_id, args.reviewer, args.reason)
    print(f"rejected {draft.id} ({draft.channel}/{draft.event_type}) reason={args.reason}")
    return 0


def cmd_dry_run(args: argparse.Namespace) -> int:
    q = queue_mod.ReviewQueue(args.queue_path)
    publisher = PUBLISHERS[args.channel]()
    candidates = [d for d in q.list_drafts(channel=args.channel) if d.status in ("approved", "scheduled")]
    if not candidates:
        print(f"(no approved/scheduled drafts for {args.channel})")
        return 0
    for draft in candidates:
        result = publisher.publish(draft, dry_run=True)
        print(f"--- {draft.id} ---")
        print(f"would send: {result.would_send}")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    q = queue_mod.ReviewQueue(args.queue_path)
    stats = q.review_stats()
    costs = q.cost_summary()
    today = dt.datetime.now(dt.UTC).date()
    week = today.isocalendar()
    print(f"# Social weekly — week {week.week}, {today.isoformat()}")
    print()
    print("## Headline")
    print("0 sign-ups this week vs 0 last week; no channel has published yet (Sprint 2 stops before publish)")
    print()
    print("## Per channel")
    print(
        "| Channel | Posts | Impressions | Engagements | Clicks | Sign-ups | Unsubs "
        "| Cost (USD) | Cost/sign-up |"
    )
    print("|---|---|---|---|---|---|---|---|---|")
    print("| Email/RSS | 0 | n/a | 0 | 0 | 0 | 0 | 0.00 | not measured |")
    for channel in ("bluesky", "linkedin", "x"):
        published = len(q.list_drafts(channel=channel, status="published"))
        cost = costs.get(channel, 0.0)
        na = "n/a" if channel != "x" else "0"
        print(f"| {channel.capitalize()} | {published} | 0 | 0 | 0 | 0 | {na} | {cost:.2f} | not measured |")
    print()
    print("## Top 5 posts by clicks")
    print("(none -- no post has published yet)")
    print()
    print("## Bottom 5 by impressions (same volume class)")
    print("(none)")
    print()
    print("## Event-type performance")
    print("| Event type | Posts | Clicks/post | Sign-ups/post |")
    print("|---|---|---|---|")
    print()
    print("## Review queue")
    print(
        f"Drafts: {stats['drafted']}; approved {stats['approved']}; edited {stats['edited']}; "
        f"rejected {stats['rejected']}; expired {stats['expired']}; "
        f"wrong_fact corrections {stats['wrong_fact']}."
    )
    print()
    print("## Budget")
    print(
        f"X spend {costs.get('x', 0.0):.2f} of 250.00; model spend 0.00; "
        "email sends 0 of plan; forecast month-end 0.00."
    )
    print()
    print("## Incidents")
    print("(none)")
    print()
    print("## Graduation status")
    for channel in ("bluesky", "linkedin", "x"):
        for event_type in sorted(editorial.POSTABLE_EVENT_TYPES):
            result = q.graduation_status(channel, event_type)
            print(f"{channel} / {event_type}: eligible={result.eligible} ({'; '.join(result.reasons)})")
    print()
    print("## Recommendations (max 3)")
    print("1. No channel has posted yet -- complete owner account setup (docs/32 §2) before any config edit")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m services.social")
    parser.add_argument(
        "--queue-path", default=str(DEFAULT_QUEUE_PATH), help="path to the JSON review-queue store"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_draft = sub.add_parser("draft", help="render eligible drafts from a change-events parquet")
    p_draft.add_argument("--events", required=True)
    p_draft.add_argument(
        "--records", default=None, help="canonical snapshot parquet, for diff.py-shaped events"
    )
    p_draft.set_defaults(func=cmd_draft)

    p_review = sub.add_parser("review", help="review-queue operations")
    review_sub = p_review.add_subparsers(dest="review_command", required=True)

    p_list = review_sub.add_parser("list")
    p_list.add_argument("--channel", default=None, choices=list(PUBLISHERS))
    p_list.add_argument("--status", default=None)
    p_list.set_defaults(func=cmd_review_list)

    p_approve = review_sub.add_parser("approve")
    p_approve.add_argument("draft_id")
    p_approve.add_argument("--reviewer", required=True)
    p_approve.set_defaults(func=cmd_review_approve)

    p_reject = review_sub.add_parser("reject")
    p_reject.add_argument("draft_id")
    p_reject.add_argument("--reviewer", required=True)
    p_reject.add_argument("--reason", required=True, choices=list(REJECT_REASONS))
    p_reject.set_defaults(func=cmd_review_reject)

    p_dry_run = sub.add_parser("dry-run", help="preview what would be sent for approved/scheduled drafts")
    p_dry_run.add_argument("channel", choices=list(PUBLISHERS))
    p_dry_run.set_defaults(func=cmd_dry_run)

    p_report = sub.add_parser("report", help="print the weekly KPI report template")
    p_report.set_defaults(func=cmd_report)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
