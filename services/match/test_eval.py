"""`services/match/eval.py` and the committed labels: the file meets US-401 AC3's size, its split is
the documented one, the numbers docs/22 §21 quotes are what the code computes, and the sampling
frame draws what it says it draws."""

from __future__ import annotations

import csv
import datetime as dt
from collections import Counter
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from services.api.conftest import (
    make_open_licence,
    make_public_source,
    make_visible_opportunity,
    make_visible_proposal,
)
from services.db.session import get_engine, get_sessionmaker, init_db
from services.match.eval import (
    COLUMNS,
    DEFAULT_LABELS_PATH,
    LabelledPair,
    draw_sample,
    main,
    measure,
    read_labels,
    report_lines,
    split_for,
    wilson_interval,
    write_rows,
)
from services.match.rules import load_rules


def test_committed_labels_meet_the_acceptance_size_and_are_mixed() -> None:
    pairs = read_labels()
    assert len(pairs) >= 100
    labels = Counter(p.label for p in pairs)
    assert labels[True] > 0 and labels[False] > 0
    assert len({p.proposal.technology for p in pairs}) >= 5
    assert len({p.proposal.jurisdiction for p in pairs}) >= 10
    for pair in pairs:
        assert pair.split == split_for(pair.row["opportunity_source_id"], pair.row["opportunity_record_id"])
        assert pair.row["note"], "every label carries the reason it was given"


def test_the_numbers_docs_22_quotes() -> None:
    pairs = read_labels()
    rules = load_rules()
    everything = measure(pairs, rules)
    assert (everything.n, everything.positives, everything.tp, everything.fp, everything.fn) == (
        103,
        5,
        5,
        15,
        0,
    )
    assert everything.precision == pytest.approx(0.25)
    assert everything.recall == 1.0
    test = measure([p for p in pairs if p.split == "test"], rules)
    assert (test.tp, test.fp, test.fn) == (5, 10, 0)
    tune = measure([p for p in pairs if p.split == "tune"], rules)
    assert (tune.positives, tune.tp, tune.fp) == (0, 0, 5)
    assert tune.recall is None and tune.recall_ci is None


def test_wilson_interval() -> None:
    assert wilson_interval(0, 0) is None
    low, high = wilson_interval(5, 20) or (0.0, 0.0)
    assert low == pytest.approx(0.112, abs=1e-3) and high == pytest.approx(0.469, abs=1e-3)
    assert wilson_interval(5, 5) == pytest.approx((0.566, 1.0), abs=1e-3)


def test_report_and_cli(capsys: pytest.CaptureFixture[str]) -> None:
    lines = report_lines(read_labels(), load_rules(), sweep=True)
    assert lines[0].startswith("rule set match-rules@v1")
    assert any(line.startswith("all    103") for line in lines)
    assert any("threshold sweep" in line for line in lines)
    assert main(["--sweep"]) == 0
    assert "0.250 (0.112-0.469)" in capsys.readouterr().out
    assert main(["--rules", str(load_rules().path), "--labels", str(DEFAULT_LABELS_PATH)]) == 0


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
