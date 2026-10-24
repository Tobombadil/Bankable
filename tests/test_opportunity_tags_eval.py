"""Pins the labelled evaluation of the opportunity technology tags and type-of-work profile
(`data/eval/opportunity_technology_tags.py`, docs/28 lane F3): the samples are big enough, their
labels are valid and attributed, the held-out rows share no notice with the in-sample rows, and
the figures the lane reports are the ones the code gives. In-sample "after" figures are floors (the
rules were fitted to those rows); held-out figures are pinned exactly, so a rule change that moves
them has to be re-reported. Offline: the CSV and the recorded utility RFP bundles only."""

from __future__ import annotations

import importlib.util
import pathlib
from types import ModuleType
from typing import Any

import pytest

from pipeline.connectors.opportunity import WORK_TYPES

ROOT = pathlib.Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "data" / "eval" / "opportunity_technology_tags.py"


def _module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("opportunity_technology_tags_eval", MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def ev() -> ModuleType:
    return _module()


@pytest.fixture(scope="module")
def rows(ev: ModuleType) -> list[dict[str, str]]:
    return list(ev.load_rows())


@pytest.fixture(scope="module")
def report(ev: ModuleType, rows: list[dict[str, str]]) -> dict[str, Any]:
    return dict(ev.evaluate(rows))


def _held(row: dict[str, str]) -> bool:
    return "heldout" in row["strata"].split("|")


def test_every_affected_tag_has_at_least_100_judgements(rows: list[dict[str, str]]) -> None:
    for question in ("solar_pv", "nuclear", "coal", "biomass", "transmission", "all_source"):
        assert sum(1 for r in rows if r["question"] == question) >= 100, question
    drawn = [
        r for r in rows if r["question"] == "work_types" and r["strata"] not in ("rfp_census", "heldout")
    ]
    assert len(drawn) >= 100
    held = [r for r in rows if _held(r)]
    assert {
        q: sum(1 for r in held if r["question"] == q) for q in ("solar_pv", "transmission", "work_types")
    } == {
        "solar_pv": 102,
        "transmission": 93,
        "work_types": 100,
    }


def test_labels_are_valid_and_every_row_is_attributed(rows: list[dict[str, str]]) -> None:
    for r in rows:
        if r["question"] == "work_types":
            keys = r["label"].split("|")
            assert keys and set(keys) <= set(WORK_TYPES), r["public_id"]
            assert "other" not in keys or keys == ["other"], r["public_id"]  # other is "none of the above"
        else:
            assert r["label"] in ("0", "1"), r["public_id"]
        for field in ("public_id", "source_id", "source_url", "retrieved_at", "licence_id", "reuse_class"):
            assert r[field], (r["public_id"], field)
        assert r["title"] == " ".join(r["title"].split()), r["public_id"]  # no stray whitespace
    assert len({(r["question"], r["public_id"]) for r in rows}) == len(rows)


def test_the_held_out_rows_share_no_notice_with_the_in_sample_rows(rows: list[dict[str, str]]) -> None:
    held = {r["public_id"] for r in rows if _held(r)}
    fitted = {r["public_id"] for r in rows if not _held(r)}
    assert held and not held & fitted
    for question in ("solar_pv", "transmission", "work_types"):
        numbers = sorted(int(r["sheet_row"][1:]) for r in rows if _held(r) and r["question"] == question)
        assert numbers == list(range(1, len(numbers) + 1)), question


def test_the_goods_labels_follow_the_plant_and_equipment_definition(rows: list[dict[str, str]]) -> None:
    """Review of 2026-10-24, finding 4: vehicles, office supplies, medicines, radio-isotopes, IT,
    protective vests and a chemical were labelled `equipment`."""
    by_row = {r["sheet_row"]: r for r in rows if r["question"] == "work_types" and not _held(r)}
    for sheet_row in ("7", "22", "25", "62", "69", "74", "83", "109", "125", "142"):
        assert by_row[sheet_row]["label"] == "other", sheet_row
        assert "review finding 4" in by_row[sheet_row]["note"], sheet_row


def test_wilson_interval(ev: ModuleType) -> None:
    low, high = ev.wilson(73, 76)  # by hand: centre 0.9384, half-width 0.0481
    assert abs(low - 0.8903) < 5e-4 and abs(high - 0.9865) < 5e-4
    assert ev.wilson(0, 0) == (0.0, 0.0)


# In-sample. The stored tags (before) are fixed data: their figures are exact. Find a Tender rows
# are left out (their tags cannot be recomputed offline), so transmission and all-source count 99.
BEFORE_PRECISION = {
    "solar_pv": (18, 100),
    "nuclear": (3, 58),
    "coal": (2, 28),
    "biomass": (8, 13),
    "transmission": (44, 99),
    "all_source": (1, 99),
}
# The rules' in-sample figures (after) measured on 2026-10-24: floors, so a regression fails.
AFTER_FLOORS = {
    "solar_pv": (0.96, 1.00),
    "nuclear": (1.00, 1.00),
    "coal": (1.00, 1.00),
    "biomass": (1.00, 0.96),
    "transmission": (0.87, 0.93),
    "all_source": (1.00, 0.83),
}


@pytest.mark.parametrize("question", sorted(BEFORE_PRECISION))
def test_in_sample_before_and_after(report: dict[str, Any], question: str) -> None:
    m = report["in_sample"]["questions"][question]
    before = m["before"]["precision"]
    assert (before["k"], before["n"]) == BEFORE_PRECISION[question]
    precision, recall = AFTER_FLOORS[question]
    assert m["after"]["precision"]["value"] >= precision
    assert m["after"]["recall"]["value"] >= recall
    assert m["after"]["precision"]["value"] > before["value"]


def _kn(r: dict[str, Any]) -> tuple[int, int]:
    return (r["k"], r["n"])


def test_held_out_technology_figures(report: dict[str, Any]) -> None:
    held = report["held_out"]["questions"]
    assert set(held) == {"solar_pv", "transmission"}  # the other frames were the in-sample census
    solar = held["solar_pv"]
    assert (
        solar["positives"] == 0
        and solar["before_false_positives"] == 102
        and solar["after_false_positives"] == 0
    )
    tx = held["transmission"]
    assert tx["labelled"] == 92 and tx["positives"] == 28  # one Find a Tender row left out
    assert _kn(tx["before"]["precision"]) == (13, 55) and _kn(tx["before"]["recall"]) == (13, 28)
    assert _kn(tx["after"]["precision"]) == (21, 25) and _kn(tx["after"]["recall"]) == (21, 28)


def test_type_of_work(report: dict[str, Any]) -> None:
    w = report["in_sample"]["work_types"]
    assert w["known_before"]["k"] == 0
    assert w["known_after"]["value"] == 1.0
    assert w["exact_after"]["value"] >= 0.90
    assert w["micro_after"]["precision"]["value"] >= 0.90
    assert w["micro_after"]["recall"]["value"] >= 0.89
    keys = w["keys"]
    assert keys["project"]["precision"]["value"] == 1.0
    assert keys["works"]["precision"]["value"] >= 0.97
    assert keys["energy_supply"]["precision"]["value"] >= 0.97
    held = report["held_out"]["work_types"]
    assert held["labelled"] == 100 and _kn(held["known_after"]) == (100, 100)
    assert _kn(held["exact_after"]) == (81, 100)
    assert _kn(held["micro_after"]["precision"]) == (86, 102) and _kn(held["micro_after"]["recall"]) == (
        86,
        104,
    )
    assert _kn(held["keys"]["equipment"]["precision"]) == (17, 26)
    assert _kn(held["keys"]["funding"]["precision"]) == (17, 17)
