"""`pipeline/resolve.py`: the D2 queue-id guards and `evaluate()` (audit 2026-10-07 QA-8; PRD A-9,
"precision prioritised because wrong merges are visible to sponsors").

D2 hard-merges two records that share an ISO and a queue id. ISO-NE reuses queue ids across
unrelated projects, so D2 refuses a pair when the states differ, when both counties are known and
differ, or when both names are known and `token_set_ratio < 60` (`deterministic()`). Before these
tests, no test reached those lines: deleting any of the three `continue`s merged unrelated projects
and every test still passed. Each negative case below differs from the positive control in exactly
one field, so each one fails if its own guard is removed.

`evaluate()` computes the sample and band-weighted precision and recall behind M-2 and A-9. The
labelled frame below is small enough to compute by hand, and the arithmetic is in the comments.
"""

from __future__ import annotations

import math

import pandas as pd
import pytest

from pipeline import resolve
from pipeline.normalize import norm_county, norm_name

ISO_NE = "us.iso.isone.gen_queue"
OTHER = "us.state.permits"


def row(
    source: str,
    queue_id: str | None,
    name: str | None,
    *,
    state: str | None = "US-ME",
    county: str | None = "Aroostook",
    iso: str | None = "ISONE",
) -> dict[str, object]:
    return {
        "source_id": source,
        "iso": iso,
        "queue_id": queue_id,
        "state": state,
        "county_norm": norm_county(county) if county else None,
        "name_norm": norm_name(name) if name else None,
        "eia_plant_id": None,
        "eia_generator_id": None,
        "cross_refs": "",
    }


def d2_pairs(rows: list[dict[str, object]]) -> set[tuple[int, int]]:
    out = resolve.deterministic(pd.DataFrame(rows))
    d2 = out[out["pass"] == "D2_queue_id"]
    return {(int(li), int(ri)) for li, ri in zip(d2["li"], d2["ri"], strict=True)}


# ---------------------------------------------------------------------------------- D2 guards
def test_same_iso_and_queue_id_with_consistent_place_and_name_is_a_d2_pair() -> None:
    """The positive control: without it the negative cases below would pass on a D2 that never fires."""
    rows = [
        row(ISO_NE, "QP-1000", "Number Nine Wind Farm"),
        row(OTHER, "qp-1000 ", "Number Nine Wind"),  # case and whitespace are normalised in the key
    ]
    assert d2_pairs(rows) == {(0, 1)}


def test_a_shared_queue_id_in_two_states_is_id_reuse_not_a_merge(capsys: pytest.CaptureFixture[str]) -> None:
    rows = [
        row(ISO_NE, "QP-1000", "Number Nine Wind Farm", state="US-ME", county="Washington"),
        row(OTHER, "QP-1000", "Number Nine Wind Farm", state="US-VT", county="Washington"),
    ]
    assert d2_pairs(rows) == set()
    assert "D2 groups/pairs rejected as queue-id reuse: 1" in capsys.readouterr().err


def test_a_shared_queue_id_in_two_counties_of_one_state_is_not_a_merge() -> None:
    rows = [
        row(ISO_NE, "QP-1000", "Number Nine Wind Farm", county="Aroostook"),
        row(OTHER, "QP-1000", "Number Nine Wind Farm", county="Penobscot"),
    ]
    assert d2_pairs(rows) == set()


def test_a_shared_queue_id_on_dissimilar_names_is_not_a_merge() -> None:
    names = ("Number Nine Wind Farm", "Pittsfield Battery Storage")
    assert resolve.fuzz.token_set_ratio(norm_name(names[0]), norm_name(names[1])) < 60
    rows = [row(ISO_NE, "QP-1000", names[0]), row(OTHER, "QP-1000", names[1])]
    assert d2_pairs(rows) == set()


def test_unknown_county_or_name_does_not_block_the_pair() -> None:
    """The county and name guards compare known values only; a missing value is not a conflict."""
    assert d2_pairs([row(ISO_NE, "QP-1", "Number Nine Wind"), row(OTHER, "QP-1", None, county=None)]) == {
        (0, 1)
    }


def test_in_a_group_of_three_only_the_conflicting_pairs_are_dropped() -> None:
    rows = [
        row(ISO_NE, "QP-7", "Number Nine Wind", county="Aroostook"),
        row(OTHER, "QP-7", "Number Nine Wind", county="Aroostook"),
        row(OTHER, "QP-7", "Number Nine Wind", county="Penobscot"),
    ]
    assert d2_pairs(rows) == {(0, 1)}


def test_the_same_queue_id_under_two_isos_is_two_keys() -> None:
    rows = [row(ISO_NE, "Q-1", "Number Nine Wind"), row(OTHER, "Q-1", "Number Nine Wind", iso="NYISO")]
    assert d2_pairs(rows) == set()


# ----------------------------------------------------------------------------------- evaluate()
def _matches() -> pd.DataFrame:
    cols = ["left_id", "right_id", "score", "evidence", "pass", "veto", "id_conflict"]
    data = [
        ("a", "b", 90.0, 0.90, "F_fuzzy", "", False),  # label 1, read back reversed: TP at 80 and 85
        ("c", "d", 82.0, 0.80, "F_fuzzy", "", False),  # label 0: FP at 80, TN at 85
        ("e", "f", 70.0, 0.90, "F_fuzzy", "", False),  # label 1: FN (below both thresholds)
        ("g", "h", 100.0, 1.00, "D2_queue_id", "", False),  # label 1: deterministic, always predicted
        ("i", "j", 95.0, 0.90, "F_fuzzy", "capacity", False),  # label 0: vetoed, never predicted
        ("k", "l", 88.0, 0.30, "F_fuzzy", "", False),  # label 1: evidence < MIN_EVIDENCE, FN
        ("o", "p", 92.0, 0.90, "F_fuzzy", "", True),  # label 0: registry-id conflict, never predicted
    ]
    return pd.DataFrame(data, columns=cols)


def _labels() -> pd.DataFrame:
    return pd.DataFrame(
        [
            ("b", "a", 1),  # stored reversed: evaluate() must find (a, b)
            ("c", "d", 0),
            ("e", "f", 1),
            ("g", "h", 1),
            ("i", "j", 0),
            ("k", "l", 1),
            ("o", "p", 0),
            ("m", "n", 1),  # not among the matches at all: FN, counted as unmatched
            ("c", "d", -1),  # uncertain: excluded everywhere
        ],
        columns=["left_id", "right_id", "label"],
    )


def test_evaluate_reports_hand_computed_sample_and_weighted_precision_and_recall() -> None:
    assert resolve.MIN_EVIDENCE == 0.50  # the fixture's evidence values straddle this
    report = resolve.evaluate(_matches(), _labels(), [80, 85]).set_index("threshold")

    # t=80: predicted = a-b, c-d, g-h.  TP a-b, g-h; FP c-d; FN e-f, k-l, m-n; TN i-j, o-p.
    at80 = report.loc[80]
    assert (at80.tp, at80.fp, at80.fn, at80.tn, at80.n, at80.unmatched_labels) == (2, 1, 3, 2, 8, 1)
    assert at80.sample_precision == round(2 / 3, 3)
    assert at80.sample_recall == round(2 / 5, 3)
    assert at80.sample_f1 == round(2 * (2 / 3) * (2 / 5) / ((2 / 3) + (2 / 5)), 3)
    # Weights: population pairs per score band (evidence >= 0.5) / labelled pairs in that band.
    #   [88,95): population a-b, o-p (2); labelled a-b, k-l, o-p (3)  -> 2/3 each
    #   [95,101): population g-h, i-j (2); labelled g-h, i-j (2)      -> 1
    #   [82,88) c-d and [66,72) e-f: 1/1                               -> 1
    #   [0,35): the unmatched label scores 0; no population pair       -> 0
    # wTP = a-b 2/3 + g-h 1 = 5/3; wFP = c-d 1; wFN = e-f 1 + k-l 2/3 + m-n 0 = 5/3.
    assert at80.weighted_precision == round((5 / 3) / (5 / 3 + 1), 3)  # 0.625
    assert at80.weighted_recall == round((5 / 3) / (5 / 3 + 5 / 3), 3)  # 0.5

    # t=85: c-d drops out of the predictions and becomes a TN; nothing else moves.
    at85 = report.loc[85]
    assert (at85.tp, at85.fp, at85.fn, at85.tn) == (2, 0, 3, 3)
    assert (at85.sample_precision, at85.sample_recall) == (1.0, 0.4)
    assert (at85.weighted_precision, at85.weighted_recall) == (1.0, 0.5)


def test_a_vetoed_or_conflicting_pair_is_never_predicted_even_at_threshold_zero() -> None:
    report = resolve.evaluate(_matches(), _labels(), [0]).iloc[0]
    # At 0 every unvetoed pair with enough evidence is predicted: a-b, c-d, e-f, g-h.
    # i-j (veto) and o-p (id conflict) stay negative; k-l lacks evidence; m-n was never scored.
    assert (report.tp, report.fp, report.fn, report.tn) == (3, 1, 2, 2)


def test_evaluate_with_no_positive_prediction_reports_nan_precision() -> None:
    labels = pd.DataFrame([("e", "f", 1)], columns=["left_id", "right_id", "label"])
    report = resolve.evaluate(_matches(), labels, [99]).iloc[0]
    assert (report.tp, report.fp, report.fn) == (0, 0, 1)
    assert math.isnan(report.sample_precision)
    assert report.sample_recall == 0.0
