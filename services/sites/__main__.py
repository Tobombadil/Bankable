"""`python -m services.sites`: run the site pass over the store `DATABASE_URL` names, or measure it.

    python -m services.sites                  # rebuild sites (what the resolve tick runs) and report
    python -m services.sites --dry-run        # build in memory, write nothing, report
    python -m services.sites --dry-run --top 10 --sample 20 --seed 7 --show "Matador" --show darden

The report prints the counts the owner's hand check needs: sites, members per site, records grouped
per rule, relationship labels and confidences, the largest sites with their lead and labels, any
site flagged for review, the sites holding a named record, and a seeded random sample of sites.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from collections.abc import Sequence

from services.sites.build import PlannedSite, make_candidates, plan, read_inputs, rebuild_sites, summarise


def _site_lines(site: PlannedSite, limit: int) -> list[str]:
    lead = site.lead.candidate
    out = [
        f"  {lead.public_id}  {lead.name!r}  {lead.capacity_mw} MW {lead.technology} {lead.lifecycle_state}"
        f"  members={len(site.members)}{'  REVIEW' if site.review else ''}"
    ]
    for m in site.members[:limit]:
        c = m.candidate
        out.append(
            f"    {m.rank:>3} {m.label.relation:<13} {m.label.confidence:<6} {m.label.rule:<34}"
            f" via {m.grouping_rule:<19} {c.name[:40]!r} {c.capacity_mw} MW"
            f" {c.technology} {c.lifecycle_state}"
        )
    if len(site.members) > limit:
        out.append(f"    ... {len(site.members) - limit} more")
    return out


def render(
    planned: Sequence[PlannedSite],
    *,
    top: int = 10,
    sample: int = 20,
    seed: int = 7,
    show: Sequence[str] = (),
    limit: int = 8,
) -> str:
    report = summarise(planned)
    lines = [json.dumps(report.as_dict(), indent=1)]
    lines.append(f"\nLargest {top} sites:")
    for site in sorted(planned, key=lambda s: (-len(s.members), s.lead.candidate.public_id))[:top]:
        lines += _site_lines(site, limit)
    flagged = [s for s in planned if s.review]
    lines.append(f"\nFlagged for review: {len(flagged)}")
    for site in flagged:
        lines += _site_lines(site, limit)
    for needle in show:
        hits = [
            s
            for s in planned
            if any(
                needle.lower() in m.candidate.name.lower() or needle == m.candidate.public_id
                for m in s.members
            )
        ]
        lines.append(f"\nSites holding {needle!r}: {len(hits)}")
        for site in hits:
            lines += _site_lines(site, 40)
    if sample:
        rnd = random.Random(seed)  # noqa: S311 -- a reproducible hand-check sample, not a secret
        picked = rnd.sample(list(planned), min(sample, len(planned)))
        lines.append(f"\n{len(picked)} random sites (seed {seed}):")
        for site in picked:
            lines += _site_lines(site, limit)
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m services.sites", description=__doc__.split("\n")[0])
    parser.add_argument("--dry-run", action="store_true", help="build in memory, write nothing")
    parser.add_argument("--database-url", default=None, help="defaults to DATABASE_URL")
    parser.add_argument("--top", type=int, default=10)
    parser.add_argument("--sample", type=int, default=20)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--show", action="append", default=[], help="a name fragment or public id")
    args = parser.parse_args(argv)

    from services.db.session import get_engine, get_sessionmaker, session_scope

    factory = get_sessionmaker(get_engine(args.database_url))
    if args.dry_run:
        session = factory()
        try:
            planned = plan(make_candidates(*read_inputs(session)))
        finally:
            session.rollback()
            session.close()
    else:
        with session_scope(factory) as session:
            candidates = make_candidates(*read_inputs(session))
            written = rebuild_sites(session, candidates=candidates)
            planned = plan(candidates)
        counts = {k: v for k, v in written.as_dict().items() if not isinstance(v, dict)}
        print("written:", json.dumps(counts))  # noqa: T201 -- a CLI report
    print(render(planned, top=args.top, sample=args.sample, seed=args.seed, show=args.show))  # noqa: T201
    return 0


if __name__ == "__main__":
    sys.exit(main())
