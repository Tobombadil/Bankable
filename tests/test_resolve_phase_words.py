"""pipeline/resolve.py: number words and letter phases in `phase_tokens` / `phase_key` (lane I3,
2026-09-29; docs/22 §22.9 A-22-H5-3 as amended).

A number word ("Attentive Energy Two Offshore Wind") or a letter ("Solar Phase B", "Sand Hill C"
beside "Sand Hill A") names a phase the way "2" or "II" does. Both are parsed conservatively: a
number word counts only after a generation word or at the end of the name, and a trailing letter
counts only after "Phase" or when the same base name also occurs with a different letter in the
frame. The ordinary-word cases below are copied from real names in `data/eval/normalized.parquet`.

Helpers are duplicated from `tests/test_resolve_rules.py` rather than imported, per this repo's
convention that test modules do not import one another.
"""

from __future__ import annotations

import pathlib

import pandas as pd
import pytest

from pipeline import resolve
from pipeline.normalize import norm_county, norm_name, norm_org

EIA = "us.eia.860m"
CAISO = "us.iso.caiso.gen_queue"
ERCOT = "us.iso.ercot.gen_queue"


def rec(
    source: str,
    rid: str,
    name: str,
    technology: str | None,
    mw: float | None,
    *,
    county: str,
    state: str,
    sponsor: str | None = None,
    plant: str | None = None,
) -> dict[str, object]:
    return {
        "record_id": f"{source}:{rid}",
        "source_id": source,
        "source_record_id": rid,
        "source_url": f"https://example.org/{rid}",
        "retrieved_at": "2026-09-29T00:00:00Z",
        "licence": "test",
        "kind": "generation",
        "name_canonical": name,
        "name_norm": norm_name(name),
        "sponsor_name": sponsor,
        "sponsor_norm": norm_org(sponsor) if sponsor else None,
        "technology": technology,
        "capacity_mw": mw,
        "iso": None if source == EIA else source.split(".")[2].upper(),
        "state": state,
        "county": county,
        "county_norm": norm_county(county),
        "lifecycle_state": "filed",
        "status_raw": "x",
        "proposed_cod": pd.NaT,
        "queue_id": None if source == EIA else rid,
        "eia_plant_id": plant,
        "eia_generator_id": rid.split("-", 1)[1] if plant else None,
        "cross_refs": "",
    }


def run(tmp_path: pathlib.Path, rows: list[dict[str, object]]) -> tuple[pd.DataFrame, pd.DataFrame]:
    frame = pd.DataFrame(rows)
    frame["capacity_mw"] = frame["capacity_mw"].astype("Float64")
    frame["proposed_cod"] = pd.to_datetime(frame["proposed_cod"])
    path = tmp_path / "normalized.parquet"
    frame.to_parquet(path, index=False)
    return resolve.run(75.0, path)


def together(clusters: pd.DataFrame, a: str, b: str) -> bool:
    cid = clusters.drop_duplicates("record_id").set_index("record_id")["cluster_id"]
    return a in cid.index and b in cid.index and cid[a] == cid[b]


def pair(matches: pd.DataFrame, a: str, b: str) -> pd.Series:
    hit = matches[
        ((matches["left_id"] == a) & (matches["right_id"] == b))
        | ((matches["left_id"] == b) & (matches["right_id"] == a))
    ]
    assert len(hit), f"{a} / {b} was never a candidate"
    return hit.sort_values("score", ascending=False).iloc[0]


# ------------------------------------------------------------------ number words
@pytest.mark.parametrize(
    ("name", "phase"),
    [
        ("Attentive Energy Two Offshore Wind", {2}),  # after a generation word
        ("SUNSHINE FOUR", {4}),  # the last word
        ("SES SOLAR TWO)", {2}),
        ("NCBP TEN, LLC", {10}),  # only a legal suffix follows
        ("Rock N' Roll Storage Two", {2}),
        ("CROCKER SOLAR POWER - UNIT ONE", set()),  # a lone one reads as unnumbered, like "1"
        ("AV SOLAR ONE PROJECT", set()),
    ],
)
def test_number_words_are_phases(name: str, phase: set[int]) -> None:
    assert resolve.phase_key(name) == frozenset(phase)


def test_unit_one_is_phase_one_for_rule_p() -> None:
    assert resolve.phase_tokens("Glen Charlie Unit One") == {1}


@pytest.mark.parametrize(
    "name",
    [
        "Two Rivers Wind Facility",  # the first word is a name
        "Nine Mile Point Station Unit",
        "THREE SISTERS Solar Generation and BESS",
        "Seventy Seven Wind",  # part of a larger number
        "Seventy-Seven Solar",
        "Number Nine Wind Farm",
        "Big Five Storage",  # an ordinary word mid-name
        "NY125B - Two Rivers Solar",
        "EXCELSIOR SOLAR (FKA: BURFORD FIVE POINTS)",
        "SUNSHINE THREE EXPANSION",
        "Cooperative Solar Three-Marion",  # hyphen-joined to a place name
        "Rhubarb One SC",
    ],
)
def test_number_words_in_ordinary_names_are_not_phases(name: str) -> None:
    assert resolve.phase_tokens(name) == set()


# ------------------------------------------------------------------ letter phases
def test_a_letter_after_phase_is_a_phase() -> None:
    assert resolve.phase_key("Clutch City Solar Phase B") == frozenset({"B"})
    assert resolve.phase_key("Clutch City Solar Ph C") == frozenset({"C"})
    assert resolve.phase_key("Clutch City Solar Phase B") != resolve.phase_key("Clutch City Solar Phase C")


def test_a_letter_is_a_phase_when_its_base_name_occurs_with_another_letter() -> None:
    bases = resolve.lettered_bases(["Sand Hill A", "Sand Hill B", "SAND HILL C", "Elevate Pier S"])
    assert resolve.phase_key("SAND HILL C", bases) == frozenset({"C"})
    assert resolve.phase_key("Sand Hill A", bases) == frozenset({"A"})
    # Alone in the frame, the same trailing letter is part of the name.
    assert resolve.phase_key("Elevate Pier S", bases) == frozenset()
    assert resolve.phase_key("SAND HILL C") == frozenset()


def test_ordinary_trailing_letters_are_not_phases() -> None:
    bases = resolve.lettered_bases(
        ["Queensboro B", "Queensboro Renewable Express Circuit A", "DESERT VISTA GREENWORKS B"]
    )
    assert bases == frozenset()
    assert resolve.phase_key("Queensboro B", bases) == frozenset()
    assert resolve.phase_key("PVV-K SOLAR I") == frozenset()  # roman I, a lone first phase
    assert resolve.phase_key("Plant X") == frozenset({10})  # roman X, unchanged
    assert resolve.phase_key("Solar Vitamin-A") == frozenset()  # hyphen-joined, not a word


def test_letters_and_numbers_stay_distinct() -> None:
    assert resolve.phase_key("Clutch City Solar Phase A") != resolve.phase_key("Clutch City Solar 1")


# ------------------------------------------------------------------ through run()
def test_lettered_siblings_do_not_merge_across_letters(tmp_path: pathlib.Path) -> None:
    """Two EIA plants 'Patriot Solar A' and 'Patriot Solar B' (as CAISO files them) and a CAISO
    request for B: before, the request scored high enough with A to chain both plants together."""
    rows = [
        rec(CAISO, "1001", "PATRIOT SOLAR B", "solar", 100.0, county="Kern", state="CA"),
        rec(EIA, "70001-PA", "Patriot Solar A", "solar", 100.0, county="Kern", state="CA", plant="70001"),
        rec(EIA, "70002-PB", "Patriot Solar B", "solar", 100.0, county="Kern", state="CA", plant="70002"),
    ]
    matches, clusters = run(tmp_path, rows)
    wrong = pair(matches, f"{CAISO}:1001", f"{EIA}:70001-PA")
    assert "phase_conflict" in wrong["rationale"]
    assert not wrong["accepted"]
    assert together(clusters, f"{CAISO}:1001", f"{EIA}:70002-PB")
    assert not together(clusters, f"{EIA}:70001-PA", f"{EIA}:70002-PB")


def test_number_word_phases_do_not_merge_across_numbers(tmp_path: pathlib.Path) -> None:
    rows = [
        rec(ERCOT, "27INR0001", "Sunshine Solar Two", "solar", 150.0, county="Pecos", state="TX"),
        rec(
            EIA, "70101-S4", "Sunshine Solar Four", "solar", 150.0, county="Pecos", state="TX", plant="70101"
        ),
        rec(EIA, "70102-S2", "Sunshine Solar Two", "solar", 150.0, county="Pecos", state="TX", plant="70102"),
    ]
    matches, clusters = run(tmp_path, rows)
    wrong = pair(matches, f"{ERCOT}:27INR0001", f"{EIA}:70101-S4")
    assert "phase_conflict" in wrong["rationale"]
    assert not wrong["accepted"]
    assert together(clusters, f"{ERCOT}:27INR0001", f"{EIA}:70102-S2")
    assert not together(clusters, f"{EIA}:70101-S4", f"{EIA}:70102-S2")
