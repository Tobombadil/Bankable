"""Precision and recall of the opportunity technology tags and the type-of-work profile, before
and after docs/28 lane F3, on the labelled sample beside this file.

    python data/eval/opportunity_technology_tags.py           # the tables
    python data/eval/opportunity_technology_tags.py --json    # the same numbers as JSON

Two samples, reported apart (review of 2026-10-24, finding 6):

**In-sample** (`strata` other than `heldout`): 1,030 judgements of 807 notices of the public
register as served on 2026-10-23 (1,809 notices), drawn with seed 20261024 and judged from the
title, the first nine CPV codes, the contract nature and the World Bank reference (for the utility
RFP registers, the register's own summary). The rules were then written and revised against these
rows, so every "after" figure on them is in-sample and optimistic: the gas-network rule and the
coal group code were checked against them, six notices left the transmission `after` stratum after
judging, six work-type labels were revised toward the rules' answer after the first comparison
(their `note` says so), and the draw itself used intermediate rule output, so it cannot be redrawn
from the repo. Strata: technology questions `before` (up to 100 notices the stored tags carried
the tag on), `after` (up to 100 the rules of 2026-10-24 carried it on), `pool` (up to 100 of both
and a screen of title words, CPV prefixes and sector codes; recall is read here), `pad` (outside
the pool); `all_source` (does the notice invite every resource technology?) `before` the untagged
notices, which matched every technology filter until B-1, `after` the tagged ones, `pool` the
utility RFPs and titles naming all-source or capacity; `work_types` 100 default-view notices, 50
others and the 16 utility RFPs (`rfp_census`). Ten work-type labels were changed on 2026-10-24 from
`equipment` to `other` (vehicles, office supplies, medicines, IT, protective vests, a chemical:
not plant and equipment, review finding 4); their `note` says so.

**Held-out** (`strata` `heldout`): drawn by `opportunity_technology_tags_draw.py` (seed 20261025)
after the rules were frozen, from the same register less every in-sample notice, and judged from
the same fields before any rule was run on them. Frames are rule-independent: the stored tags and
the screen. They are the census of what was left: 102 notices for Solar (every one an electricity,
heat or unrelated notice the old CPV rule tagged), 93 for transmission, and 100 drawn of the 533
open or announced notices for type of work. Nuclear, coal, biomass and all-source have no held-out
rows: the in-sample draw already held every screened notice, so those figures stay in-sample.

The solar question is the Solar family (`solar_pv` or `solar_thermal`, docs/28 §8.1): the judges
counted a solar heat plant as solar. Find a Tender rows are left out of the technology figures:
its connector also reads the description, which the register does not serve, so their tags cannot
be recomputed here (`not_recomputed`). The tags are recomputed by the repo's connectors from the
stored fields in the CSV (for the utility RFP registers, from the recorded fixture bundles'
descriptions), so the numbers follow the code. Offline; Wilson 95% intervals.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import math
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
CSV_PATH = HERE / "opportunity_technology_tags.csv"
TECH_QUESTIONS = ("solar_pv", "nuclear", "coal", "biomass", "transmission")
ALL_SOURCE = "all_source"
WORK_QUESTION = "work_types"
HELD_OUT = "heldout"
#: The solar question's tokens: the Solar family (docs/28 §8.1).
SOLAR_FAMILY = ("solar_pv", "solar_thermal")
#: Sources whose tags cannot be recomputed from the CSV's fields (the module docstring).
NOT_RECOMPUTED = ("gb.find_a_tender",)
#: The date the fixture bundles were recorded (the reader drops items closed before it).
FIXTURE_DATE = dt.date(2026, 10, 21)


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval for k successes in n trials (0, 0 when n is 0)."""
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return (max(0.0, centre - half), min(1.0, centre + half))


def rate(k: int, n: int) -> dict[str, Any]:
    low, high = wilson(k, n)
    return {"k": k, "n": n, "value": (k / n) if n else None, "low": low, "high": high}


def split(value: str) -> list[str]:
    return [v for v in (value or "").split("|") if v]


def load_rows(path: Path = CSV_PATH) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def rfp_items(root: Path = ROOT) -> dict[tuple[str, str], dict[str, Any]]:
    """The utility RFP items the recorded fixture bundles hold, by (source_id, title)."""
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
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


def technologies_after(row: dict[str, str], items: dict[tuple[str, str], dict[str, Any]]) -> list[str]:
    """The tags the repo's connector for the row's source gives it now."""
    from pipeline.connectors import opportunity as opp
    from pipeline.connectors.eu_ted_api import connector as ted
    from pipeline.connectors.mdb_worldbank_procnotices import connector as wb
    from pipeline.connectors.us_rfp import classify

    source, title = row["source_id"], row["title"]
    if source == "eu.ted.api":
        return list(ted.technologies(title, split(row["cpv"])))
    if source == "mdb.worldbank.procnotices":
        return list(wb.technologies(title, split(row["sector_codes"])))
    if source.startswith("us.rfp."):
        item = items.get((source, title)) or {}
        return list(classify.technologies(title, str(item.get("description") or "")))
    return list(opp.classify_technologies(title))


def work_types_after(
    row: dict[str, str], techs: list[str], items: dict[tuple[str, str], dict[str, Any]]
) -> list[str]:
    from pipeline.connectors import opportunity as opp

    identifiers: dict[str, Any] = {
        "cpv": split(row["cpv"]),
        "contract_nature": split(row["contract_nature"]),
        "sector_codes": split(row["sector_codes"]),
    }
    if row["bid_reference_no"]:
        identifiers["bid_reference_no"] = row["bid_reference_no"]
    raw: dict[str, Any] | None = None
    if row["source_id"].startswith("us.rfp."):
        item = items.get((row["source_id"], row["title"])) or {}
        raw = {"contract_types": item.get("contract_types"), "description": item.get("description")}
    notice = {
        "source_id": row["source_id"],
        "kind": row["kind"],
        "title": row["title"],
        "technologies": techs,
        "identifiers": identifiers,
    }
    return [tag.key for tag in opp.work_profile(notice, raw).work_types]


def carries(question: str, techs: Iterable[str]) -> bool:
    """Whether tags answer the question yes, as the filter would."""
    have = set(techs)
    if question == ALL_SOURCE:
        return ALL_SOURCE in have
    if question == "solar_pv":
        return bool(have & set(SOLAR_FAMILY))
    return question in have


def carried_before(question: str, stored: list[str]) -> bool:
    """The stored tags' answer: before B-1 an untagged notice matched every technology filter."""
    return (not stored) if question == ALL_SOURCE else question in stored


def _binary(rows: Iterable[dict[str, Any]], predicted: str) -> dict[str, Any]:
    """Precision on rows predicted positive, recall on rows labelled positive."""
    rows = list(rows)
    pos = [r for r in rows if r[predicted]]
    true = [r for r in rows if r["truth"]]
    return {
        "precision": rate(sum(1 for r in pos if r["truth"]), len(pos)),
        "recall": rate(sum(1 for r in true if r[predicted]), len(true)),
    }


def _judged(
    rows: list[dict[str, str]], question: str, items: dict[tuple[str, str], dict[str, Any]]
) -> list[dict[str, Any]]:
    judged = []
    for row in rows:
        if row["question"] != question or row["source_id"].startswith(NOT_RECOMPUTED):
            continue
        stored = split(row["stored_technologies"])
        judged.append(
            {
                "strata": split(row["strata"]),
                "truth": row["label"] == "1",
                "before": carried_before(question, stored),
                "after": carries(question, technologies_after(row, items)),
                "public_id": row["public_id"],
            }
        )
    return judged


def _in_sample_question(judged: list[dict[str, Any]]) -> dict[str, Any]:
    def stratum(name: str) -> list[dict[str, Any]]:
        return [j for j in judged if name in j["strata"]]

    before_rows, after_rows, pool = stratum("before"), stratum("after"), stratum("pool")
    removed = sorted(j["public_id"] for j in after_rows if not j["after"])
    return {
        "labelled": len(judged),
        "positives": sum(1 for j in judged if j["truth"]),
        "before": {
            "precision": rate(sum(1 for j in before_rows if j["truth"]), len(before_rows)),
            "recall": _binary(pool, "before")["recall"],
        },
        "after": {
            "precision": rate(
                sum(1 for j in after_rows if j["after"] and j["truth"]),
                sum(1 for j in after_rows if j["after"]),
            ),
            "recall": _binary(pool, "after")["recall"],
        },
        "outside_pool_positives": sum(1 for j in stratum("pad") if j["truth"]),
        "removed_since_draw": removed,
    }


def _held_out_question(judged: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "labelled": len(judged),
        "positives": sum(1 for j in judged if j["truth"]),
        "before": _binary(judged, "before"),
        "after": _binary(judged, "after"),
        "before_false_positives": sum(1 for j in judged if j["before"] and not j["truth"]),
        "after_false_positives": sum(1 for j in judged if j["after"] and not j["truth"]),
    }


def _work(rows: list[dict[str, str]], items: dict[tuple[str, str], dict[str, Any]]) -> dict[str, Any]:
    from pipeline.connectors import opportunity as opp

    judged_work: list[tuple[set[str], set[str]]] = []
    for row in rows:
        truth = set(split(row["label"]))
        predicted = set(work_types_after(row, technologies_after(row, items), items))
        judged_work.append((truth, predicted))
    per_key: dict[str, Any] = {}
    for key in opp.WORK_TYPES:
        tp = sum(1 for t, p in judged_work if key in t and key in p)
        n_pred = sum(1 for _, p in judged_work if key in p)
        n_true = sum(1 for t, _ in judged_work if key in t)
        per_key[key] = {"precision": rate(tp, n_pred), "recall": rate(tp, n_true), "truth": n_true}
    audiences: dict[str, Any] = {}
    for audience in opp.AUDIENCES[:5]:

        def reaches(work: set[str], audience: str = audience) -> bool:
            return any(audience in opp.WORK_TYPE_AUDIENCES.get(k, ()) for k in work)

        tp = sum(1 for t, p in judged_work if reaches(t) and reaches(p))
        n_pred = sum(1 for _, p in judged_work if reaches(p))
        n_true = sum(1 for t, _ in judged_work if reaches(t))
        audiences[audience] = {"precision": rate(tp, n_pred), "recall": rate(tp, n_true)}
    tp_all = sum(len(t & p) for t, p in judged_work)
    return {
        "labelled": len(judged_work),
        "known_before": rate(0, len(judged_work)),
        "known_after": rate(sum(1 for _, p in judged_work if p), len(judged_work)),
        "exact_after": rate(sum(1 for t, p in judged_work if t == p), len(judged_work)),
        "micro_after": {
            "precision": rate(tp_all, sum(len(p) for _, p in judged_work)),
            "recall": rate(tp_all, sum(len(t) for t, _ in judged_work)),
        },
        "keys": per_key,
        "audiences": audiences,
    }


def evaluate(rows: list[dict[str, str]] | None = None, root: Path = ROOT) -> dict[str, Any]:
    """Every number the module reports, as one dict (the pinning test reads it)."""
    rows = load_rows() if rows is None else rows
    items = rfp_items(root)
    in_sample = [r for r in rows if HELD_OUT not in split(r["strata"])]
    held_out = [r for r in rows if HELD_OUT in split(r["strata"])]
    report: dict[str, Any] = {"in_sample": {"questions": {}}, "held_out": {"questions": {}}}
    for question in (*TECH_QUESTIONS, ALL_SOURCE):
        report["in_sample"]["questions"][question] = _in_sample_question(_judged(in_sample, question, items))
        report["in_sample"]["questions"][question]["not_recomputed"] = sum(
            1 for r in in_sample if r["question"] == question and r["source_id"].startswith(NOT_RECOMPUTED)
        )
        held = _judged(held_out, question, items)
        if held:
            report["held_out"]["questions"][question] = _held_out_question(held)
    report["in_sample"]["work_types"] = _work([r for r in in_sample if r["question"] == WORK_QUESTION], items)
    report["held_out"]["work_types"] = _work([r for r in held_out if r["question"] == WORK_QUESTION], items)
    return report


def _fmt(r: dict[str, Any]) -> str:
    if not r["n"]:
        return "n/a (0 of 0)"
    return f"{r['value']:.2f} [{r['low']:.2f}, {r['high']:.2f}] ({r['k']}/{r['n']})"


def _render_work(w: dict[str, Any], lines: list[str]) -> None:
    lines.append(
        f"type of work, {w['labelled']} notices: known before {_fmt(w['known_before'])}, "
        f"known after {_fmt(w['known_after'])}, exact {_fmt(w['exact_after'])}"
    )
    lines.append(
        f"  micro precision {_fmt(w['micro_after']['precision'])}, recall {_fmt(w['micro_after']['recall'])}"
    )
    for key, m in w["keys"].items():
        lines.append(f"  {key:<14} P {_fmt(m['precision']):<26} R {_fmt(m['recall'])}")
    for key, m in w["audiences"].items():
        lines.append(f"  -> {key:<20} P {_fmt(m['precision']):<26} R {_fmt(m['recall'])}")


def render(report: dict[str, Any]) -> str:
    header = (
        "question       labelled  P before                 P after                  "
        "R before                 R after"
    )
    lines = ["IN-SAMPLE (the rules were fitted to these rows: optimistic)", header]
    for q, m in report["in_sample"]["questions"].items():
        lines.append(
            f"{q:<14} {m['labelled']:>8}  {_fmt(m['before']['precision']):<24} "
            f"{_fmt(m['after']['precision']):<24} {_fmt(m['before']['recall']):<24} "
            f"{_fmt(m['after']['recall'])}"
        )
        if m["removed_since_draw"]:
            lines.append(f"  removed since the draw: {len(m['removed_since_draw'])} of the after stratum")
        if m["outside_pool_positives"]:
            lines.append(f"  positives outside the screened pool: {m['outside_pool_positives']}")
        if m["not_recomputed"]:
            lines.append(f"  left out, not recomputable offline: {m['not_recomputed']}")
    lines.append("")
    _render_work(report["in_sample"]["work_types"], lines)
    lines += [
        "",
        "HELD-OUT (seed 20261025, drawn after the rules were frozen, judged before they were run)",
        header,
    ]
    for q, m in report["held_out"]["questions"].items():
        lines.append(
            f"{q:<14} {m['labelled']:>8}  {_fmt(m['before']['precision']):<24} "
            f"{_fmt(m['after']['precision']):<24} {_fmt(m['before']['recall']):<24} "
            f"{_fmt(m['after']['recall'])}"
        )
        lines.append(
            f"  positives {m['positives']}; false positives before {m['before_false_positives']}, "
            f"after {m['after_false_positives']}"
        )
    lines.append("")
    _render_work(report["held_out"]["work_types"], lines)
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--json", action="store_true", help="print the numbers as JSON")
    args = parser.parse_args(argv)
    report = evaluate()
    sys.stdout.write(json.dumps(report, indent=1) + "\n" if args.json else render(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
