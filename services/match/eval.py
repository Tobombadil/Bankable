"""Measure the rule set against labelled pairs (docs/10 US-401 AC3; docs/22 §21).
`python -m services.match.eval [--split tune|test|all] [--sweep] [--labels PATH] [--rules PATH]`.

`data/eval/match_labels.csv` holds one row per labelled proposal <-> opportunity pair with the
facts the label was judged on (both sides' technology, jurisdiction, capacity, lifecycle state or
status, due date) and the date they were true (`as_of`), so scoring a row needs neither a database
nor data/normalized: `score_pair(facts, now=as_of)` is exactly what `services/match/run.py` would
have computed that day. Identity is `(source_id, source_record_id)` on each side -- the public ids
are minted from random uuids at load time and do not survive a reload.

`split` is fixed per *opportunity* (`tune` when the first hex digit of
`sha256("<source_id>:<source_record_id>")` is even), so every pair of one notice lands in the same
half and the half used to choose the threshold never shares a notice with the half it is reported
on. `draw_sample` is the frame the file was drawn from (docs/22 §21.2); it reads a loaded store and
emits unlabelled rows, so an independent labeller can re-draw and re-label from the same frame.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import math
import sys
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from services.match.engine import OpportunityFacts, ProposalFacts, score_pair
from services.match.rules import REPO_ROOT, RuleSet, load_rules, load_rules_from

DEFAULT_LABELS_PATH = REPO_ROOT / "data" / "eval" / "match_labels.csv"

COLUMNS = (
    "stratum",
    "split",
    "as_of",
    "proposal_source_id",
    "proposal_record_id",
    "proposal_name",
    "proposal_technology",
    "proposal_jurisdiction",
    "proposal_capacity_mw",
    "proposal_lifecycle_state",
    "opportunity_source_id",
    "opportunity_record_id",
    "opportunity_title",
    "opportunity_technologies",
    "opportunity_jurisdiction",
    "opportunity_capacity_sought_mw",
    "opportunity_status",
    "opportunity_due_at",
    "label",
    "note",
)


def split_for(opportunity_source_id: str, opportunity_record_id: str) -> str:
    digest = hashlib.sha256(f"{opportunity_source_id}:{opportunity_record_id}".encode()).hexdigest()
    return "tune" if int(digest[0], 16) % 2 == 0 else "test"


def _opt_float(value: str) -> float | None:
    return float(value) if value not in ("", None) else None


def _opt_datetime(value: str) -> dt.datetime | None:
    if not value:
        return None
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=dt.UTC)


@dataclass(frozen=True)
class LabelledPair:
    row: dict[str, str]
    proposal: ProposalFacts
    opportunity: OpportunityFacts
    as_of: dt.datetime
    label: bool
    split: str

    @classmethod
    def from_row(cls, row: dict[str, str]) -> LabelledPair:
        label = row["label"].strip().lower()
        if label not in ("true", "false"):
            raise ValueError(f"label must be true or false, got {row['label']!r}")
        as_of = _opt_datetime(row["as_of"])
        if as_of is None:
            raise ValueError("as_of is required")
        return cls(
            row=row,
            proposal=ProposalFacts(
                technology=row["proposal_technology"] or None,
                jurisdiction=row["proposal_jurisdiction"],
                capacity_mw=_opt_float(row["proposal_capacity_mw"]),
                lifecycle_state=row["proposal_lifecycle_state"],
            ),
            opportunity=OpportunityFacts(
                technologies=[t for t in row["opportunity_technologies"].split(";") if t],
                jurisdiction=row["opportunity_jurisdiction"],
                capacity_sought_mw=_opt_float(row["opportunity_capacity_sought_mw"]),
                status=row["opportunity_status"],
                due_at=_opt_datetime(row["opportunity_due_at"]),
            ),
            as_of=as_of,
            label=label == "true",
            split=row["split"],
        )


def read_labels(path: Path = DEFAULT_LABELS_PATH) -> list[LabelledPair]:
    with path.open(encoding="utf-8", newline="") as fh:
        return [LabelledPair.from_row(row) for row in csv.DictReader(fh)]


# ------------------------------------------------------------------------------------------ metrics
def wilson_interval(successes: int, n: int, z: float = 1.96) -> tuple[float, float] | None:
    if n == 0:
        return None
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


@dataclass(frozen=True)
class Metrics:
    n: int
    positives: int
    tp: int
    fp: int
    fn: int
    tn: int

    @property
    def precision(self) -> float | None:
        predicted = self.tp + self.fp
        return self.tp / predicted if predicted else None

    @property
    def recall(self) -> float | None:
        return self.tp / self.positives if self.positives else None

    @property
    def precision_ci(self) -> tuple[float, float] | None:
        return wilson_interval(self.tp, self.tp + self.fp)

    @property
    def recall_ci(self) -> tuple[float, float] | None:
        return wilson_interval(self.tp, self.positives)


def predict(pair: LabelledPair, rules: RuleSet, threshold: float | None = None) -> bool:
    result = score_pair(pair.proposal, pair.opportunity, rules, now=pair.as_of)
    return result.all_rules_pass and result.score >= (rules.threshold if threshold is None else threshold)


def measure(pairs: Iterable[LabelledPair], rules: RuleSet, threshold: float | None = None) -> Metrics:
    tp = fp = fn = tn = n = positives = 0
    for pair in pairs:
        n += 1
        positives += pair.label
        predicted = predict(pair, rules, threshold)
        if predicted and pair.label:
            tp += 1
        elif predicted:
            fp += 1
        elif pair.label:
            fn += 1
        else:
            tn += 1
    return Metrics(n=n, positives=positives, tp=tp, fp=fp, fn=fn, tn=tn)


def _fmt(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.3f}"


def _fmt_ci(ci: tuple[float, float] | None) -> str:
    return "n/a" if ci is None else f"{ci[0]:.3f}-{ci[1]:.3f}"


def report_lines(pairs: Sequence[LabelledPair], rules: RuleSet, *, sweep: bool = False) -> list[str]:
    lines = [f"rule set {rules.version} (threshold {rules.threshold:.2f}), {len(pairs)} labelled pairs"]
    lines.append("split  n    pos  tp  fp  fn  tn    precision (95% CI)       recall (95% CI)")
    for name in ("tune", "test", "all"):
        subset = [p for p in pairs if name == "all" or p.split == name]
        m = measure(subset, rules)
        lines.append(
            f"{name:<6} {m.n:<4} {m.positives:<4} {m.tp:<3} {m.fp:<3} {m.fn:<3} {m.tn:<5} "
            f"{_fmt(m.precision)} ({_fmt_ci(m.precision_ci)})   {_fmt(m.recall)} ({_fmt_ci(m.recall_ci)})"
        )
    by_stratum: dict[str, list[LabelledPair]] = defaultdict(list)
    for pair in pairs:
        by_stratum[pair.row["stratum"]].append(pair)
    lines.append("stratum                      n    pos  predicted")
    for stratum in sorted(by_stratum):
        subset = by_stratum[stratum]
        m = measure(subset, rules)
        lines.append(f"{stratum:<28} {m.n:<4} {m.positives:<4} {m.tp + m.fp}")
    if sweep:
        lines.append("threshold sweep on the tune split (precision / recall / predicted):")
        tune = [p for p in pairs if p.split == "tune"]
        for step in range(50, 100, 5):
            threshold = step / 100
            m = measure(tune, rules, threshold)
            lines.append(f"  {threshold:.2f}  {_fmt(m.precision)} / {_fmt(m.recall)} / {m.tp + m.fp}")
    return lines


# ------------------------------------------------------------------------------------ sampling frame
_PER_OPPORTUNITY_CAP = {"timing_only_fail": 12, "probe_same_country": 5}


def _stable_hash(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


def draw_sample(
    session: Any,
    *,
    as_of: dt.datetime,
    rules: RuleSet | None = None,
    per_stratum: dict[str, int] | None = None,
    probe_opportunities: Sequence[tuple[str, str]] = (),
) -> list[dict[str, str]]:
    """The frame `data/eval/match_labels.csv` was drawn from, as unlabelled rows (docs/22 §21.2):

    - `predicted_match` -- every pair the rule set accepts on `as_of` (a census, not a sample);
    - `timing_only_fail` -- technology and jurisdiction pass, timing fails;
    - `technology_fail_same_country` -- same country, technology fails;
    - `probe_same_country` -- the `probe_opportunities` (source id, record id) the labeller named
      as possibly project-eligible despite carrying no technology tag, against same-country
      proposals: where recall errors would be, if there are any;
    - `cross_country_same_family` -- technology family shared, different country.

    Deterministic: within a stratum rows are ordered by a hash of both record keys and taken
    greedily with a per-opportunity cap (`_PER_OPPORTUNITY_CAP`: the timing and probe strata are
    drawn from five and four notices, the others from hundreds) and one row per (technology,
    jurisdiction) of the proposal, so a stratum spreads across notices, technologies and states."""
    from sqlalchemy import select

    from services.db.models import Opportunity, Proposal
    from services.match.engine import jurisdiction_country, opportunity_families, proposal_families
    from services.match.run import opportunity_facts, proposal_facts

    rules = rules or load_rules()
    quotas = per_stratum or {
        "timing_only_fail": 24,
        "technology_fail_same_country": 30,
        "probe_same_country": 20,
        "cross_country_same_family": 20,
    }
    proposals = list(
        session.scalars(
            select(Proposal).where(Proposal.merged_into_id.is_(None), Proposal.publish_state != "unpublished")
        ).unique()
    )
    opportunities = list(
        session.scalars(
            select(Opportunity).where(
                Opportunity.merged_into_id.is_(None), Opportunity.publish_state != "unpublished"
            )
        ).unique()
    )

    def key(record: Proposal | Opportunity) -> tuple[str, str]:
        link = min((s for s in record.sources if s.active), key=lambda s: (s.source_id, s.source_record_id))
        return link.source_id, link.source_record_id

    p_facts = {p.id: proposal_facts(p) for p in proposals}
    o_facts = {o.id: opportunity_facts(o) for o in opportunities}
    p_keys = {p.id: key(p) for p in proposals}
    o_keys = {o.id: key(o) for o in opportunities}
    probes = {tuple(p) for p in probe_opportunities}
    p_country = {pid: jurisdiction_country(f.jurisdiction) for pid, f in p_facts.items()}
    p_fams = {pid: proposal_families(f, rules) for pid, f in p_facts.items()}

    strata: dict[str, list[tuple[str, Proposal, Opportunity]]] = defaultdict(list)
    for o in opportunities:
        of = o_facts[o.id]
        o_country = jurisdiction_country(of.jurisdiction)
        o_fams = opportunity_families(of, rules)
        for p in proposals:
            pf = p_facts[p.id]
            same_country = p_country[p.id] == o_country
            shares_family = o_fams is None or bool(p_fams[p.id] & o_fams)
            if not same_country and not shares_family:
                continue
            h = _stable_hash(*p_keys[p.id], *o_keys[o.id])
            if not same_country:
                strata["cross_country_same_family"].append((h, p, o))
                continue
            result = score_pair(pf, of, rules, now=as_of)
            if result.is_match:
                strata["predicted_match"].append((h, p, o))
            elif result.rules_failed == ("timing",):
                strata["timing_only_fail"].append((h, p, o))
            elif o_keys[o.id] in probes:
                strata["probe_same_country"].append((h, p, o))
            elif "technology" in result.rules_failed:
                strata["technology_fail_same_country"].append((h, p, o))

    rows: list[dict[str, str]] = []
    for stratum in (
        "predicted_match",
        "timing_only_fail",
        "technology_fail_same_country",
        "probe_same_country",
        "cross_country_same_family",
    ):
        taken: list[tuple[str, Proposal, Opportunity]] = []
        per_opp: dict[Any, int] = defaultdict(int)
        seen_profile: set[tuple[str | None, str]] = set()
        for h, p, o in sorted(strata[stratum], key=lambda t: t[0]):
            if stratum != "predicted_match":
                if len(taken) >= quotas.get(stratum, 0):
                    break
                profile = (p.technology, p.jurisdiction)
                if per_opp[o.id] >= _PER_OPPORTUNITY_CAP.get(stratum, 2) or profile in seen_profile:
                    continue
                seen_profile.add(profile)
            per_opp[o.id] += 1
            taken.append((h, p, o))
        for _, p, o in taken:
            rows.append(_row(stratum, as_of, p, o, p_keys[p.id], o_keys[o.id]))
    return rows


def _row(
    stratum: str,
    as_of: dt.datetime,
    p: Any,
    o: Any,
    p_key: tuple[str, str],
    o_key: tuple[str, str],
) -> dict[str, str]:
    def num(value: Any) -> str:
        return "" if value is None else f"{float(value):g}"

    return {
        "stratum": stratum,
        "split": split_for(*o_key),
        "as_of": as_of.date().isoformat(),
        "proposal_source_id": p_key[0],
        "proposal_record_id": p_key[1],
        "proposal_name": p.name_canonical,
        "proposal_technology": p.technology or "",
        "proposal_jurisdiction": p.jurisdiction,
        "proposal_capacity_mw": num(p.capacity_mw),
        "proposal_lifecycle_state": p.lifecycle_state,
        "opportunity_source_id": o_key[0],
        "opportunity_record_id": o_key[1],
        "opportunity_title": o.title,
        "opportunity_technologies": ";".join(o.technologies or []),
        "opportunity_jurisdiction": o.jurisdiction,
        "opportunity_capacity_sought_mw": num(o.capacity_sought_mw),
        "opportunity_status": o.status,
        "opportunity_due_at": o.due_at.date().isoformat() if o.due_at is not None else "",
        "label": "",
        "note": "",
    }


def write_rows(rows: Iterable[dict[str, str]], path: Path) -> None:
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(COLUMNS), lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


# --------------------------------------------------------------------------------------------- CLI
def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m services.match.eval",
        description="Precision and recall of the match rule set on labelled pairs (US-401 AC3).",
    )
    parser.add_argument("--labels", type=Path, default=DEFAULT_LABELS_PATH)
    parser.add_argument("--rules", type=Path, default=None, help="another rules file (defaults to data/)")
    parser.add_argument("--sweep", action="store_true", help="threshold sweep on the tune split")
    args = parser.parse_args(argv)
    rules = load_rules_from(args.rules) if args.rules else load_rules()
    pairs = read_labels(args.labels)
    sys.stdout.write("\n".join(report_lines(pairs, rules, sweep=args.sweep)) + "\n")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "COLUMNS",
    "DEFAULT_LABELS_PATH",
    "LabelledPair",
    "Metrics",
    "draw_sample",
    "main",
    "measure",
    "predict",
    "read_labels",
    "report_lines",
    "split_for",
    "wilson_interval",
    "write_rows",
]
