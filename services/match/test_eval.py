"""`services/match/eval.py` and the committed labels: the file holds at least 100 rows (its
independent part, 82, does not: docs/22 §25.5), its split is the documented one, the numbers
docs/22 §21 (v1) and §25 (v2) quote are what the code computes, and the sampling frame draws what
it says it draws."""

from __future__ import annotations

import copy
import csv
import datetime as dt
import re
from collections import Counter
from pathlib import Path
from typing import Any

import pytest
import yaml
from sqlalchemy.orm import Session

from services.api.conftest import (
    make_open_licence,
    make_public_source,
    make_visible_opportunity,
    make_visible_proposal,
)
from services.db.session import get_engine, get_sessionmaker, init_db
from services.match import relevance
from services.match.engine import score_pair
from services.match.eval import (
    COLUMNS,
    CONTEXT_COLUMNS,
    DEFAULT_CONTEXT_PATH,
    DEFAULT_LABELS_PATH,
    LabelledPair,
    Metrics,
    context_rows,
    draw_sample,
    main,
    measure,
    notice_key,
    predict,
    read_context,
    read_labels,
    report_lines,
    split_for,
    wilson_interval,
    write_context,
    write_rows,
)
from services.match.rules import DEFAULT_RULES_PATH, RuleSet, load_rules, parse_rules

V1_SAMPLE, V2_SAMPLE = "2026-09-26", "2026-10-20"


def test_committed_labels_meet_the_acceptance_size_and_are_mixed() -> None:
    pairs = read_labels()
    assert len(pairs) >= 100
    assert Counter(p.row["as_of"] for p in pairs) == {V1_SAMPLE: 103, V2_SAMPLE: 82}
    labels = Counter(p.label for p in pairs)
    assert labels[True] > 0 and labels[False] > 0
    assert len({p.proposal.technology for p in pairs}) >= 5
    assert len({p.proposal.jurisdiction for p in pairs}) >= 10
    for pair in pairs:
        assert pair.split == split_for(pair.row["opportunity_source_id"], pair.row["opportunity_record_id"])
        assert pair.row["note"], "every label carries the reason it was given"


def test_every_labelled_notice_has_a_context_row() -> None:
    context = read_context()
    keys = {notice_key(p.row) for p in read_labels()}
    assert set(context) == keys
    origins = Counter(row["context_origin"] for row in context.values())
    assert origins == {"store 2026-10-20": 34, "not_in_store": 26}
    # Every notice of the independent sample is in the store it was drawn from.
    v2_keys = {notice_key(p.row) for p in read_labels() if p.row["as_of"] == V2_SAMPLE}
    assert all(context[k]["context_origin"] == "store 2026-10-20" for k in v2_keys)


def _v1_doc() -> dict[str, Any]:
    """The committed file with every v2 change taken out: what match-rules@v1 was."""
    doc: dict[str, Any] = copy.deepcopy(yaml.safe_load(DEFAULT_RULES_PATH.read_text(encoding="utf-8")))
    doc["rule_set_version"] = "match-rules@v1"
    doc["rules"].pop("eligibility")
    doc["rules"]["technology"].pop("excluded_tokens")
    doc["rules"]["technology"]["families"].pop("ccs")
    doc["rules"]["jurisdiction"].pop("national_kinds")
    return doc


def _m(pairs: list[LabelledPair], rules: RuleSet, as_of: str | None, split: str | None) -> Metrics:
    return measure(
        [
            p
            for p in pairs
            if (as_of is None or p.row["as_of"] == as_of) and (split is None or p.split == split)
        ],
        rules,
    )


def _cells(m: Metrics) -> tuple[int, int, int, int, int]:
    return m.n, m.positives, m.tp, m.fp, m.fn


def test_the_numbers_docs_22_quotes_for_v1() -> None:
    """§21.3 on the 2026-09-26 sample, and §25.5's v1 row on the independent 2026-10-20 sample."""
    pairs, v1 = read_labels(), parse_rules(_v1_doc())
    assert _cells(_m(pairs, v1, V1_SAMPLE, None)) == (103, 5, 5, 15, 0)
    assert _m(pairs, v1, V1_SAMPLE, None).precision == pytest.approx(0.25)
    assert _cells(_m(pairs, v1, V1_SAMPLE, "test")) == (55, 5, 5, 10, 0)
    assert _cells(_m(pairs, v1, V1_SAMPLE, "tune")) == (48, 0, 0, 5, 0)
    assert _cells(_m(pairs, v1, V2_SAMPLE, "test")) == (62, 6, 6, 24, 0)
    assert _cells(_m(pairs, v1, V2_SAMPLE, "tune")) == (20, 0, 0, 12, 0)
    assert _m(pairs, v1, V2_SAMPLE, "test").precision == pytest.approx(0.2)


def test_the_numbers_docs_22_quotes_for_v2() -> None:
    """§25.5: on the held-out half of the independent labels every predicted pair is right and no
    true pair is lost; the tune half has no positive pair and v2 predicts none of it."""
    pairs, v2 = read_labels(), load_rules()
    test = _m(pairs, v2, V2_SAMPLE, "test")
    assert _cells(test) == (62, 6, 6, 0, 0)
    assert test.precision == 1.0 and test.recall == 1.0
    assert test.precision_ci == pytest.approx((0.610, 1.0), abs=1e-3)
    assert _cells(_m(pairs, v2, V2_SAMPLE, "tune")) == (20, 0, 0, 0, 0)
    assert _cells(_m(pairs, v2, V1_SAMPLE, None)) == (103, 5, 5, 0, 0)
    both_test = _m(pairs, v2, None, "test")
    assert _cells(both_test) == (117, 11, 11, 0, 0)
    assert both_test.precision_ci == pytest.approx((0.741, 1.0), abs=1e-3)


def test_rule_j_is_what_clears_the_independent_test_half() -> None:
    """§25.5 ablation: without `national_kinds` (or without the context file that carries the
    notices' kind) the Northumberland wind tender's six pairs stay predicted, and the independent
    test half reads 6/12."""
    pairs = read_labels()
    doc: dict[str, Any] = copy.deepcopy(yaml.safe_load(DEFAULT_RULES_PATH.read_text(encoding="utf-8")))
    doc["rules"]["jurisdiction"].pop("national_kinds")
    no_j = parse_rules(doc)
    assert (_m(pairs, no_j, V2_SAMPLE, "test").tp, _m(pairs, no_j, V2_SAMPLE, "test").fp) == (6, 6)
    blind = read_labels(context_path=None)
    assert (_m(blind, load_rules(), V2_SAMPLE, "test").tp, _m(blind, load_rules(), V2_SAMPLE, "test").fp) == (
        6,
        6,
    )


def _without(doc: dict[str, Any], *, classes: tuple[str, ...] = (), rule_j: bool = True) -> dict[str, Any]:
    out: dict[str, Any] = copy.deepcopy(doc)
    gate = out["rules"]["eligibility"]
    gate["classes"] = [c for c in gate["classes"] if c["name"] not in classes]
    if not rule_j:
        out["rules"]["jurisdiction"].pop("national_kinds")
    return out


def test_v2_does_not_meet_ac3_on_this_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    """§25.5 "Against US-401 AC3": the independent sample is below AC3's 100 pairs, every pair v2
    predicts on its held-out half comes from one notice, the Wilson lower bound is below 0.7, and
    without the two components written with test-half notices known (`people`, rule J) the half
    reads 6/18. NIEHS 359970 then stays out through the "weapons" false friends (also written with
    its title known) and `research`'s "clinical trial" term, which hits NIH's "(Clinical Trial Not
    Allowed)" suffix; without the false friends it is still 6/18, without both, 6/24."""
    pairs = read_labels()
    independent = [p for p in pairs if p.row["as_of"] == V2_SAMPLE]
    held_out = [p for p in independent if p.split == "test"]
    assert (len(independent), len(held_out)) == (82, 62)
    assert len(independent) < 100
    v2 = load_rules()
    assert {p.row["opportunity_record_id"] for p in held_out if predict(p, v2)} == {"358100"}
    low, _high = measure(held_out, v2).precision_ci or (1.0, 1.0)
    assert low == pytest.approx(0.610, abs=1e-3) and low < 0.7

    doc: dict[str, Any] = yaml.safe_load(DEFAULT_RULES_PATH.read_text(encoding="utf-8"))
    no_people = _m(pairs, parse_rules(_without(doc, classes=("people",))), V2_SAMPLE, "test")
    assert (no_people.tp, no_people.fp, no_people.fn) == (6, 6, 0)
    neither_doc = _without(doc, classes=("people",), rule_j=False)
    neither = _m(pairs, parse_rules(neither_doc), V2_SAMPLE, "test")
    assert (neither.tp, neither.fp, neither.fn) == (6, 12, 0)
    assert neither.precision_ci == pytest.approx((0.163, 0.563), abs=1e-3)

    seen = [f for f in relevance.FALSE_FRIENDS if "weapons" in f]
    assert len(seen) == 2
    unseen = re.compile("|".join(f for f in relevance.FALSE_FRIENDS if f not in seen), re.IGNORECASE)
    monkeypatch.setattr(relevance, "_FALSE_FRIEND_RE", unseen)
    no_weapons = _m(pairs, parse_rules(neither_doc), V2_SAMPLE, "test")
    assert (no_weapons.tp, no_weapons.fp, no_weapons.fn) == (6, 12, 0)

    for notice_class in neither_doc["rules"]["eligibility"]["classes"]:
        if notice_class["name"] == "research":
            notice_class["terms"] = [t for t in notice_class["terms"] if "clinical" not in t]
    no_clinical = _m(pairs, parse_rules(neither_doc), V2_SAMPLE, "test")
    assert (no_clinical.tp, no_clinical.fp, no_clinical.fn) == (6, 18, 0)
    assert no_clinical.precision_ci == pytest.approx((0.120, 0.449), abs=1e-3)


def test_every_tune_row_that_informed_a_rule_is_now_rejected_by_it() -> None:
    """The [T] terms of data/match_rules.yaml name three tune-half notices; v2 rejects each of their
    v1 matches with the gate, for the reason the term gives."""
    pairs = [p for p in read_labels() if p.row["as_of"] == V2_SAMPLE and p.split == "tune"]
    by_notice: dict[str, set[str]] = {}
    for pair in pairs:
        result = score_pair(pair.proposal, pair.opportunity, load_rules(), now=pair.as_of)
        assert not result.is_match
        gate = result.outcomes[4]
        by_notice.setdefault(pair.row["opportunity_record_id"], set()).add(
            str(gate.features.get("notice_class")) if gate.passed is False else "passed"
        )
    assert by_notice == {
        "ocds-h6vhtk-07834a": {"goods_services"},
        "ocds-h6vhtk-07809d": {"goods_services"},
        "ocds-h6vhtk-0775ad": {"people"},
    }


def test_wilson_interval() -> None:
    assert wilson_interval(0, 0) is None
    low, high = wilson_interval(5, 20) or (0.0, 0.0)
    assert low == pytest.approx(0.112, abs=1e-3) and high == pytest.approx(0.469, abs=1e-3)
    assert wilson_interval(5, 5) == pytest.approx((0.566, 1.0), abs=1e-3)


def test_report_and_cli(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    lines = report_lines(read_labels(), load_rules(), sweep=True)
    assert lines[0].startswith("rule set match-rules@v2")
    assert any(line.startswith("all    185") for line in lines)
    assert any(line.startswith("  2026-10-20 test  n=62") and "1.000 (0.610-1.000)" in line for line in lines)
    assert any("threshold sweep" in line for line in lines)
    assert main(["--sweep"]) == 0
    assert "1.000 (0.741-1.000)" in capsys.readouterr().out
    assert main(["--rules", str(load_rules().path), "--labels", str(DEFAULT_LABELS_PATH)]) == 0
    v1_path = tmp_path / "v1.yaml"
    v1_path.write_text(yaml.safe_dump(_v1_doc()), encoding="utf-8")
    assert main(["--rules", str(v1_path), "--context", str(DEFAULT_CONTEXT_PATH)]) == 0
    assert "0.250 (0.112-0.469)" in capsys.readouterr().out  # §21.3's all-sample v1 line


def _row(**overrides: str) -> dict[str, str]:
    row = dict.fromkeys(COLUMNS, "")
    row.update(
        stratum="predicted_match",
        split="tune",
        as_of="2026-09-26",
        proposal_technology="storage",
        proposal_jurisdiction="US-TX",
        proposal_capacity_mw="200",
        proposal_lifecycle_state="studied",
        opportunity_technologies="bess",
        opportunity_jurisdiction="US-TX",
        opportunity_capacity_sought_mw="500",
        opportunity_status="open",
        opportunity_due_at="2026-11-10",
        label="true",
    )
    row.update(overrides)
    return row


def test_labelled_pair_parsing() -> None:
    pair = LabelledPair.from_row(_row())
    assert pair.label and pair.opportunity.due_at == dt.datetime(2026, 11, 10, tzinfo=dt.UTC)
    assert pair.proposal.capacity_mw == 200.0
    with pytest.raises(ValueError, match="label must be"):
        LabelledPair.from_row(_row(label="maybe"))
    with pytest.raises(ValueError, match="as_of"):
        LabelledPair.from_row(_row(as_of=""))
    blank = LabelledPair.from_row(
        _row(opportunity_due_at="", proposal_capacity_mw="", as_of="2026-09-26T00:00:00Z")
    )
    assert blank.opportunity.due_at is None and blank.proposal.capacity_mw is None


def test_measure_counts_every_cell() -> None:
    rules = load_rules()
    pairs = [
        LabelledPair.from_row(_row(label="true")),  # tp
        LabelledPair.from_row(_row(label="false")),  # fp
        LabelledPair.from_row(_row(label="true", opportunity_jurisdiction="PL")),  # fn
        LabelledPair.from_row(_row(label="false", opportunity_status="closed")),  # tn
    ]
    m = measure(pairs, rules)
    assert (m.tp, m.fp, m.fn, m.tn) == (1, 1, 1, 1)
    assert measure(pairs[:1], rules, threshold=1.01).tp == 0
    assert measure([], rules).precision is None


@pytest.fixture()
def db() -> Session:
    engine = get_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    with get_sessionmaker(engine)() as session:
        yield session


def test_draw_sample_frame(db: Session, tmp_path: Path) -> None:
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    now = dt.datetime(2026, 9, 26, tzinfo=dt.UTC)
    storage = make_visible_proposal(db, src, public_id_suffix="1")
    storage.technology, storage.jurisdiction = "storage", "US-TX"
    withdrawn = make_visible_proposal(db, src, public_id_suffix="2")
    withdrawn.technology, withdrawn.jurisdiction, withdrawn.lifecycle_state = "storage", "US-NY", "withdrawn"
    solar = make_visible_proposal(db, src, public_id_suffix="3")
    solar.technology, solar.jurisdiction = "solar", "US-CA"
    rfp = make_visible_opportunity(db, src, public_id_suffix="1")
    rfp.technologies, rfp.jurisdiction, rfp.due_at = ["bess"], "US", now + dt.timedelta(days=30)
    probe = make_visible_opportunity(db, src, public_id_suffix="2")
    probe.technologies, probe.jurisdiction = [], "US"
    abroad = make_visible_opportunity(db, src, public_id_suffix="3")
    abroad.technologies, abroad.jurisdiction = ["solar_pv"], "PL"
    db.flush()

    rows = draw_sample(db, as_of=now, probe_opportunities=[(src.id, "N2")])
    strata = Counter(r["stratum"] for r in rows)
    assert strata == {
        "predicted_match": 1,  # storage x rfp
        "timing_only_fail": 1,  # withdrawn storage x rfp
        "probe_same_country": 3,  # every US proposal x the probe notice
        "technology_fail_same_country": 1,  # solar x rfp
        "cross_country_same_family": 1,  # solar x the Polish solar notice
    }
    assert all(r["label"] == "" and r["as_of"] == "2026-09-26" for r in rows)

    for r in rows:
        r["label"], r["note"] = "false", "test"
    path = tmp_path / "labels.csv"
    write_rows(rows, path)
    with path.open(encoding="utf-8") as fh:
        assert next(csv.reader(fh)) == list(COLUMNS)
    assert len(read_labels(path)) == len(rows)


# ---------------------------------------------------------------------------------- notice context
def test_labelled_pair_reads_the_notice_context() -> None:
    context = {
        "kind": "tender",
        "summary": "Supply of LV cables",
        "programme_codes": "cpv:31321200;cpv:09310000",
    }
    pair = LabelledPair.from_row(_row(opportunity_title="Lot 1"), context)
    assert pair.opportunity.title == "Lot 1"
    assert pair.opportunity.kind == "tender"
    assert pair.opportunity.summary == "Supply of LV cables"
    assert tuple(pair.opportunity.programme_codes) == ("cpv:31321200", "cpv:09310000")
    bare = LabelledPair.from_row(_row())
    assert (bare.opportunity.kind, bare.opportunity.summary, tuple(bare.opportunity.programme_codes)) == (
        None,
        None,
        (),
    )


def test_context_rows_from_a_store(db: Session, tmp_path: Path) -> None:
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    opp = make_visible_opportunity(db, src, public_id_suffix="1")
    opp.kind, opp.identifiers = "tender", {"cpv": ["31321200"], "ocid": "x"}
    db.flush()
    rows = context_rows(db, [(src.id, "N1"), (src.id, "N1"), (src.id, "missing")], origin="store test")
    assert rows == [
        {
            "opportunity_source_id": src.id,
            "opportunity_record_id": "N1",
            "kind": "tender",
            "summary": "",
            "programme_codes": "cpv:31321200",
            "context_origin": "store test",
        },
        {
            "opportunity_source_id": src.id,
            "opportunity_record_id": "missing",
            "kind": "",
            "summary": "",
            "programme_codes": "",
            "context_origin": "not_in_store",
        },
    ]
    path = tmp_path / "context.csv"
    write_context(rows, path)
    with path.open(encoding="utf-8") as fh:
        assert next(csv.reader(fh)) == list(CONTEXT_COLUMNS)
    assert read_context(path)[(src.id, "N1")]["kind"] == "tender"
    assert read_context(tmp_path / "absent.csv") == {}


def test_cli_rebuilds_the_context_from_a_store(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    url = f"sqlite+pysqlite:///{tmp_path / 'store.db'}"
    engine = get_engine(url)
    init_db(engine)
    with get_sessionmaker(engine)() as session:
        src = make_public_source(session, make_open_licence(session))
        opp = make_visible_opportunity(session, src, public_id_suffix="1")
        opp.kind, opp.identifiers = "tender", {"cpv": ["31321200"]}
        session.commit()
        source_id = src.id
    labels = tmp_path / "labels.csv"
    write_rows([_row(opportunity_source_id=source_id, opportunity_record_id="N1", note="n")], labels)
    out = tmp_path / "context.csv"
    argv = ["--write-context", "store test", "--database-url", url, "--labels", str(labels)]
    assert main([*argv, "--context", str(out)]) == 0
    assert capsys.readouterr().out == ""
    row = read_context(out)[(source_id, "N1")]
    assert (row["kind"], row["programme_codes"]) == ("tender", "cpv:31321200")
    assert row["context_origin"] == "store test"
