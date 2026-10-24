"""The opportunity tags over a pulled public register, offline: the RFP default view's count per
technology option, the untagged share, the type of work, and the matcher (docs/28 lane F3).

    python data/eval/opportunity_technology_tags_measure.py --snapshot DIR            # after
    python data/eval/opportunity_technology_tags_measure.py --snapshot DIR \\
        --root BASE_CHECKOUT --stored --filter old                                    # before
    ... --matcher           # also score every notice against every proposal
    ... --dump FILE         # one JSON row per notice, for `--diff`
    python data/eval/opportunity_technology_tags_measure.py --diff BEFORE.json AFTER.json

`DIR` holds the register as the public API served it, one JSON page per file:
`opportunities-p*.json` from `GET /v1/opportunities?status=all&relevance=all` and, for
`--matcher`, `proposals-p*.json` from `GET /v1/proposals`, each `{"data": [...]}` or a bare list.
The figures docs/28 and the lane report were measured on the pull of 2026-10-23 23:16-23:21 UTC
(1,809 notices, 6,633 proposals; docs/28 §1). The pull is the register itself, under each
source's terms, so it is not in the repo; any later pull runs the same way.

`--root` is the checkout whose code is measured (default: this one); "before" is the base commit
of the lane (`5d15139`, integ-w2a) with `--stored --filter old`.

- Tags. By default every notice is re-tagged by the `--root` connector for its source, from the
  fields the API serves: TED from the title and CPV codes, the World Bank from the title and
  sector codes (and its jurisdiction re-read), Grants.gov from the title, the utility RFP
  registers from the title and the description in the recorded fixture bundles. Find a Tender's
  connector also reads the description, which the API does not serve, so its notices keep their
  stored tags and are counted as `not_recomputed`. `--stored` keeps every stored tag.
- The default RFP view: status `open` or `announced` and `relevance=energy`
  (`services/match/relevance.py` at `--root`; docs/00 2026-10-22).
- A technology option counts what the filter returns: `--filter new` (docs/28 B-1) the notices
  that carry the token or `all_source`; `--filter old` also every untagged notice, as before B-1.
- `--matcher`: `services/match/engine.score_pair` at `--root` over every (proposal, notice) pair
  that shares a country and a technology family, or faces an all-source notice; the blocking is
  `services/match/run.py`'s, which is exact. Proposals are the public pull's (not the store's
  every eligible proposal), and `now` is `--now`.
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import glob
import json
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
DEFAULT_STATUSES = ("open", "announced")
#: The date the utility RFP fixture bundles were recorded (the reader drops items closed before it).
FIXTURE_DATE = dt.date(2026, 10, 21)
NOT_RECOMPUTED = ("gb.find_a_tender",)


def load_pages(snapshot: Path, stem: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted(glob.glob(str(snapshot / f"{stem}-p*.json"))):
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
        rows += doc["data"] if isinstance(doc, dict) else doc
    return rows


def source_of(row: dict[str, Any]) -> str:
    return str((row.get("provenance") or [{}])[0].get("source_id") or "")


def rfp_items(root: Path) -> dict[tuple[str, str], dict[str, Any]]:
    """The utility RFP items the recorded fixture bundles at `root` hold, by (source_id, title)."""
    from pipeline.connectors.us_rfp import extract
    from pipeline.connectors.us_rfp.issuers import registry_rows

    registry = registry_rows()
    items: dict[tuple[str, str], dict[str, Any]] = {}
    for path in sorted((root / "pipeline/connectors/us_rfp/fixtures").glob("bundle_*.json")):
        bundle = json.loads(path.read_text(encoding="utf-8"))
        kept, _ = extract.records(bundle, registry[bundle["issuer"]], FIXTURE_DATE)
        for record in kept:
            items[(bundle["issuer"], str(record["title"]))] = dict(record)
    return items


def retag(
    source: str, title: str, identifiers: dict[str, Any], items: dict[tuple[str, str], dict[str, Any]]
) -> tuple[list[str] | None, dict[str, Any] | None]:
    """(technologies, the source fields the type-of-work reader gets) as the `--root` connector for
    `source` gives them from the served fields; technologies None when it cannot recompute them."""
    from pipeline.connectors import opportunity as opp

    if source.startswith(NOT_RECOMPUTED):
        return None, None
    if source == "eu.ted.api":
        from pipeline.connectors.eu_ted_api import connector as ted

        return list(ted.technologies(title, list(identifiers.get("cpv") or []))), None
    if source == "mdb.worldbank.procnotices":
        from pipeline.connectors.mdb_worldbank_procnotices import connector as wb

        return list(wb.technologies(title, list(identifiers.get("sector_codes") or []))), None
    if source.startswith("us.rfp."):
        from pipeline.connectors.us_rfp import classify

        item = items.get((source, title)) or {}
        description = str(item.get("description") or "")
        raw = {"contract_types": item.get("contract_types"), "description": description}
        return list(classify.technologies(title, description)), raw
    return list(opp.classify_technologies(title)), None


def jurisdiction_now(source: str, stored: str) -> str:
    if source == "mdb.worldbank.procnotices":
        from pipeline.connectors.mdb_worldbank_procnotices import connector as wb

        if hasattr(wb, "jurisdiction"):
            return str(wb.jurisdiction(stored))
    return stored


def measure(
    snapshot: Path, root: Path, *, stored: bool, filter_rule: str, matcher: bool, now: dt.datetime
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    from pipeline.connectors import opportunity as opp
    from services.match.relevance import is_energy_relevant, programme_codes

    rows = load_pages(snapshot, "opportunities")
    items = {} if stored else rfp_items(root)
    all_source = getattr(opp, "ALL_SOURCE", "all_source")
    work_profile = getattr(opp, "work_profile", None)
    dumped: list[dict[str, Any]] = []
    not_recomputed = 0
    for r in rows:
        source = source_of(r)
        identifiers = dict(r.get("identifiers") or {})
        techs: list[str] | None = list(r.get("technologies") or [])
        raw: dict[str, Any] | None = None
        jurisdiction = str(r.get("jurisdiction") or "")
        if not stored:
            new, raw = retag(source, str(r.get("title") or ""), identifiers, items)
            if new is None:
                not_recomputed += 1
            else:
                techs = new
            jurisdiction = jurisdiction_now(source, jurisdiction)
        techs = techs or []
        relevant = is_energy_relevant(
            title=r.get("title"),
            summary=r.get("summary"),
            technologies=techs,
            programme_codes=programme_codes(identifiers),
        )
        row: dict[str, Any] = {
            "public_id": r["public_id"],
            "source_id": source,
            "kind": r.get("kind"),
            "status": r.get("status"),
            "title": r.get("title"),
            "jurisdiction": jurisdiction,
            "capacity_sought_mw": r.get("capacity_sought_mw"),
            "due_at": r.get("due_at"),
            "summary": r.get("summary"),
            "identifiers": identifiers,
            "technologies": techs,
            "relevant": bool(relevant),
            "default_view": bool(relevant) and r.get("status") in DEFAULT_STATUSES,
        }
        if work_profile is not None:
            notice = {**row, "technologies": techs}
            profile = work_profile(notice, raw)
            row["work"] = profile.as_json()
        dumped.append(row)

    view = [d for d in dumped if d["default_view"]]
    tokens = sorted(
        set(getattr(opp, "OPPORTUNITY_TECHNOLOGIES", ()))
        | {t for d in dumped for t in d["technologies"] if t != all_source}
    )

    def returned(token: str, d: dict[str, Any]) -> bool:
        have = set(d["technologies"])
        if filter_rule == "old":
            return not have or token in have
        return token in have or all_source in have

    report: dict[str, Any] = {
        "root": str(root),
        "tags": "stored" if stored else "recomputed",
        "filter": filter_rule,
        "notices": len(dumped),
        "not_recomputed": not_recomputed,
        "default_view": len(view),
        "default_view_untagged": sum(1 for d in view if not d["technologies"]),
        "default_view_all_source": sum(1 for d in view if all_source in d["technologies"]),
        "technology_option": {t: sum(1 for d in view if returned(t, d)) for t in tokens},
        "technology_tagged": {t: sum(1 for d in view if t in d["technologies"]) for t in tokens},
    }
    if work_profile is not None:
        work = collections.Counter(w["key"] for d in view for w in d["work"]["work_types"])
        audience = collections.Counter(a["key"] for d in view for a in d["work"]["audiences"])
        report["default_view_work_types"] = dict(sorted(work.items()))
        report["default_view_work_unknown"] = sum(1 for d in view if not d["work"]["work_types"])
        report["default_view_audiences"] = dict(sorted(audience.items()))
        report["default_view_no_audience"] = sum(1 for d in view if not d["work"]["audiences"])
    if matcher:
        report["matcher"] = score_matches(snapshot, dumped, now)
    return report, dumped


def score_matches(snapshot: Path, notices: list[dict[str, Any]], now: dt.datetime) -> dict[str, Any]:
    from services.match.engine import (
        OpportunityFacts,
        ProposalFacts,
        jurisdiction_country,
        opportunity_families,
        proposal_families,
        score_pair,
    )
    from services.match.relevance import programme_codes
    from services.match.rules import load_rules

    rules = load_rules()
    proposals = load_pages(snapshot, "proposals")
    facts = [
        ProposalFacts(
            p.get("technology"), p.get("jurisdiction") or "", p.get("capacity_mw"), p["lifecycle_state"]
        )
        for p in proposals
    ]
    by_family: dict[tuple[str, str], list[int]] = collections.defaultdict(list)
    by_country: dict[str, list[int]] = collections.defaultdict(list)
    for i, f in enumerate(facts):
        country = jurisdiction_country(f.jurisdiction)
        by_country[country].append(i)
        for family in proposal_families(f, rules):
            by_family[(country, family)].append(i)
    per_notice: dict[str, int] = {}
    per_source: collections.Counter[str] = collections.Counter()
    regions: dict[str, collections.Counter[str]] = {}
    scored = 0
    for n in notices:
        due = dt.datetime.fromisoformat(n["due_at"].replace("Z", "+00:00")) if n.get("due_at") else None
        of = OpportunityFacts(
            n["technologies"],
            n["jurisdiction"],
            n.get("capacity_sought_mw"),
            n["status"],
            due,
            title=n.get("title"),
            summary=n.get("summary"),
            kind=n.get("kind"),
            programme_codes=programme_codes(n["identifiers"]),
        )
        country = jurisdiction_country(of.jurisdiction)
        families = opportunity_families(of, rules)
        if families is None:
            candidates = set(by_country.get(country, ()))
        else:
            candidates = {i for fam in families for i in by_family.get((country, fam), ())}
        scored += len(candidates)
        hits = [i for i in sorted(candidates) if score_pair(facts[i], of, rules, now=now).is_match]
        if hits:
            per_notice[n["public_id"]] = len(hits)
            per_source[n["source_id"]] += len(hits)
            regions[n["public_id"]] = collections.Counter(facts[i].jurisdiction for i in hits)
    return {
        "rule_set_version": rules.version,
        "now": now.isoformat(),
        "proposals": len(facts),
        "pairs_scored": scored,
        "matches": sum(per_notice.values()),
        "notices_with_matches": len(per_notice),
        "by_source": dict(sorted(per_source.items())),
        "by_notice": {
            k: {"matches": v, "top_jurisdictions": regions[k].most_common(5)}
            for k, v in sorted(per_notice.items(), key=lambda kv: -kv[1])
        },
    }


def diff(before_path: Path, after_path: Path) -> dict[str, Any]:
    """Per technology token: default-view notices that gained or lost it, and the notices that
    entered or left the default view, between two `--dump` files."""
    before = {d["public_id"]: d for d in json.loads(before_path.read_text(encoding="utf-8"))}
    after = {d["public_id"]: d for d in json.loads(after_path.read_text(encoding="utf-8"))}
    out: dict[str, Any] = {"entered_default_view": [], "left_default_view": [], "tokens": {}}
    for pid, a in after.items():
        b = before.get(pid)
        if b is None:
            continue
        if a["default_view"] and not b["default_view"]:
            out["entered_default_view"].append([pid, a["source_id"], a["title"]])
        if b["default_view"] and not a["default_view"]:
            out["left_default_view"].append([pid, a["source_id"], a["title"]])
        if not (a["default_view"] or b["default_view"]):
            continue
        for token in set(a["technologies"]) ^ set(b["technologies"]):
            entry = out["tokens"].setdefault(token, {"gained": [], "lost": []})
            side = "gained" if token in a["technologies"] else "lost"
            entry[side].append([pid, a["source_id"], a["title"]])
    out["token_counts"] = {
        t: {"gained": len(v["gained"]), "lost": len(v["lost"])} for t, v in sorted(out["tokens"].items())
    }
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument(
        "--snapshot", type=Path, help="folder of opportunities-p*.json (and proposals-p*.json)"
    )
    parser.add_argument("--root", type=Path, default=ROOT, help="checkout whose code is measured")
    parser.add_argument("--stored", action="store_true", help="keep the stored tags")
    parser.add_argument("--filter", dest="filter_rule", choices=("old", "new"), default="new")
    parser.add_argument("--matcher", action="store_true", help="score the matcher too")
    parser.add_argument("--now", default="2026-10-24T00:00:00+00:00", help="the matcher's clock")
    parser.add_argument("--dump", type=Path, help="write one JSON row per notice here")
    parser.add_argument("--diff", type=Path, nargs=2, metavar=("BEFORE", "AFTER"), help="compare two dumps")
    args = parser.parse_args(argv)
    if args.diff:
        sys.stdout.write(json.dumps(diff(*args.diff), indent=1, ensure_ascii=False) + "\n")
        return 0
    if args.snapshot is None:
        parser.error("--snapshot is required")
    root = args.root.resolve()
    sys.path.insert(0, str(root))
    report, dumped = measure(
        args.snapshot,
        root,
        stored=args.stored,
        filter_rule=args.filter_rule,
        matcher=args.matcher,
        now=dt.datetime.fromisoformat(args.now),
    )
    if args.dump:
        args.dump.write_text(json.dumps(dumped, ensure_ascii=False, indent=0), encoding="utf-8")
    sys.stdout.write(json.dumps(report, indent=1, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
