"""Tests for `pipeline.context.poi_match`. The POI strings are verbatim values of the queues'
``Interconnection Location`` / ``Connection Site`` fields (data/normalized, 2026-09-13 run); the
node table is synthetic -- names only, no real coordinates are needed to exercise the rules."""

from __future__ import annotations

import json

import pandas as pd
import pytest

from pipeline.context import poi_match as pm


@pytest.mark.parametrize(
    ("text", "kind", "name", "kv"),
    [
        ("39010 138kV HASTINGS", "substation", "hastings", (138.0,)),
        ("7254 WEIMAR8 138kV", "substation", "weimar", (138.0,)),
        ("345kV BUS #5725 PAWNEE SWITCHING STATION", "substation", "pawnee", (345.0,)),
        ("(Bus: 39015) North Alvin TNMP 138kV", "substation", "north alvin", (138.0,)),
        ("To 345 kV Birthright Station (#11684)", "substation", "birthright", (345.0,)),
        ("Eldorado 500/230kV Substation", "substation", "eldorado", (500.0, 230.0)),
        ("Tesla Substation 230 KV bus ", "substation", "tesla", (230.0,)),
        ("SCE owned Eldorado 220 kV bus", "substation", "eldorado", (220.0,)),
        ("Oswego Substation 345kV", "substation", "oswego", (345.0,)),
        ("W. Yaphank - North Bellport 69kV", "line_tap", "west yaphank north bellport", (69.0,)),
        ("Tap 138kV 7100 Burnet - 7529 Bertram", "line_tap", "tap burnet bertram", (138.0,)),
        ("Antelope-Magunden 230kV", "line_tap", "antelope magunden", (230.0,)),
        ("Greenidge Ð Haley Rd. 115kV", "line_tap", "greenidge haley rd", (115.0,)),
        ("Milliken to Wright Ave 115 kV line #973", "line_tap", "milliken wright ave line", (115.0,)),
        ("138kV Escondido (8260) to Brackettville (8252)", "line_tap", "escondido brackettville", (138.0,)),
        ("Oneida-Cortland line", "line_tap", "oneida cortland line", ()),
        ("Hurley Ave & Saugerties 69kV", "multiple", "hurley ave and saugerties", (69.0,)),
        (
            "East Garden City, Ruland Road, Sprain Brook",
            "multiple",
            "east garden city ruland road sprain brook",
            (),
        ),
        ("", "empty", "", ()),
        (None, "empty", "", ()),
    ],
)
def test_parse_poi(text, kind, name, kv):
    p = pm.parse_poi(text)
    assert p.kind == kind
    assert p.name_norm == name
    assert p.voltages_kv == kv


def _index(rows):
    return pm.NodeIndex.from_frame(pd.DataFrame(rows))


NODES = [
    {
        "node_id": "n1",
        "name": "HASTINGS",
        "state": "US-TX",
        "lon": -95.0,
        "lat": 29.0,
        "voltages_kv": [138.0],
    },
    {"node_id": "n2", "name": "Hastings", "state": "NE", "lon": -98.4, "lat": 40.6, "voltages_kv": [115.0]},
    {
        "node_id": "n3",
        "name": "ELDORADO",
        "state": "NV",
        "lon": -114.9,
        "lat": 35.8,
        "voltages_kv": [500.0, 230.0],
    },
    {"node_id": "n4", "name": "OSWEGO", "state": "NY", "lon": -76.5, "lat": 43.4, "voltages_kv": [345.0]},
    {"node_id": "n5", "name": "OSWEGO", "state": "NY", "lon": -76.51, "lat": 43.41, "voltages_kv": [115.0]},
    {"node_id": "n6", "name": "RIVERSIDE", "state": "CA", "lon": -117.4, "lat": 33.9, "voltages_kv": [230.0]},
    {"node_id": "n7", "name": "RIVERSIDE", "state": "CA", "lon": -121.0, "lat": 37.0, "voltages_kv": [230.0]},
    {"node_id": "n8", "name": "WEIMAR", "state": "TX", "lon": -96.8, "lat": 29.7, "voltages_kv": []},
    {"node_id": "n9", "name": "TAP123456", "state": "TX", "lon": -97.0, "lat": 30.0, "voltages_kv": [138.0]},
    {
        "node_id": "n10",
        "name": "PAWNEE SWITCHING STATION",
        "state": "TX",
        "lon": -97.9,
        "lat": 28.7,
        "voltages_kv": [345.0],
    },
    {
        "node_id": "n11",
        "name": "BIRTHRIGHT",
        "state": "TX",
        "lon": -95.6,
        "lat": 33.1,
        "voltages_kv": [138.0],
    },
    {"node_id": "n14", "name": "Antelope", "state": "CA", "lon": None, "lat": None, "voltages_kv": [70.0]},
]


def test_exact_match_uses_state_block_and_voltage():
    idx = _index(NODES)
    m = pm.match_poi(pm.parse_poi("39010 138kV HASTINGS"), idx, iso="ERCOT", state="TX")
    assert (m.method, m.node.node_id, m.voltage_agrees) == ("exact", "n1", True)
    # No row state: the ISO footprint (ERCOT -> TX) blocks the Nebraska namesake out.
    m = pm.match_poi(pm.parse_poi("39010 138kV HASTINGS"), idx, iso="ERCOT")
    assert m.node.node_id == "n1"


def test_ercot_mnemonic_and_switching_station_words():
    idx = _index(NODES)
    assert pm.match_poi(pm.parse_poi("7254 WEIMAR8 138kV"), idx, iso="ERCOT", state="TX").node.node_id == "n8"
    m = pm.match_poi(pm.parse_poi("345kV BUS #5725 PAWNEE SWITCHING STATION"), idx, iso="ERCOT", state="TX")
    assert (m.method, m.node.node_id) == ("exact", "n10")


def test_exact_name_at_another_voltage_is_kept_but_marked_and_scored_lower():
    idx = _index(NODES)
    m = pm.match_poi(pm.parse_poi("To 345 kV Birthright Station (#11684)"), idx, iso="ERCOT", state="TX")
    assert (m.method, m.node.node_id, m.voltage_agrees) == ("exact", "n11", False)
    assert m.score == pm.EXACT_VOLTAGE_CONFLICT_SCORE
    # ...but not when the node is named by a single, unplaced line.
    m = pm.match_poi(pm.parse_poi("Antelope Substation 230kV"), idx, iso="CAISO", state="CA")
    assert (m.method, m.reason, m.voltage_agrees) == ("none", "voltage_conflict", False)
    m = pm.match_poi(pm.parse_poi("Antelope 70kV"), idx, iso="CAISO", state="CA")
    assert (m.method, m.node.node_id, m.voltage_agrees) == ("exact", "n14", True)


def test_fuzzy_name_at_another_voltage_is_dropped():
    idx = _index(
        [
            *NODES,
            {
                "node_id": "n13",
                "name": "Imperial Valley",
                "state": "CA",
                "lon": -115.5,
                "lat": 32.7,
                "voltages_kv": [500.0],
            },
        ]
    )
    m = pm.match_poi(pm.parse_poi("Imperial Vally 230kV sub"), idx, iso="CAISO", state="CA")
    assert (m.method, m.reason, m.voltage_agrees) == ("none", "voltage_conflict", False)


def test_same_name_same_place_is_one_node_and_far_apart_is_ambiguous():
    idx = _index(NODES)
    m = pm.match_poi(pm.parse_poi("Oswego Substation 345kV"), idx, iso="NYISO", state="NY")
    assert (m.method, m.node.node_id) == ("exact", "n4")  # voltage keeps the 345 kV yard
    m = pm.match_poi(pm.parse_poi("Riverside 230kV"), idx, iso="CAISO", state="CA")
    assert (m.method, m.reason) == ("none", "ambiguous")


def test_caiso_footprint_reaches_nevada():
    idx = _index(NODES)
    m = pm.match_poi(pm.parse_poi("Eldorado 500/230kV Substation"), idx, iso="CAISO")
    assert (m.method, m.node.node_id, m.voltage_agrees) == ("exact", "n3", True)


def test_line_taps_and_multiples_are_never_forced():
    idx = _index(NODES)
    for text in ("Tap 138kV 7100 Burnet - 7529 Bertram", "Hurley Ave & Saugerties 69kV"):
        m = pm.match_poi(pm.parse_poi(text), idx, iso="ERCOT", state="TX")
        assert m.method == "none" and m.node is None
        assert m.reason in ("line_tap", "multiple")


def test_placeholder_node_names_are_not_indexed():
    idx = _index(NODES)
    assert "tap" not in idx.by_name.get("TX", {})
    assert all(n.node_id != "n9" for n in idx.by_state["TX"])


def test_fuzzy_match_threshold_and_margin():
    idx = _index(
        [*NODES, {"node_id": "n12", "name": "Imperial Valley", "state": "CA", "lon": -115.5, "lat": 32.7}]
    )
    m = pm.match_poi(pm.parse_poi("Imperial Vally 230kV sub"), idx, iso="CAISO", state="CA")
    assert m.method == "fuzzy" and m.node.node_id == "n12" and 0.9 <= m.score < 1.0
    m = pm.match_poi(pm.parse_poi("Imperial 230kV"), idx, iso="CAISO", state="CA")
    assert (m.method, m.reason) == ("none", "below_threshold")


def test_neso_is_never_matched_against_us_nodes():
    idx = _index(NODES)
    m = pm.match_poi(pm.parse_poi("Drax 132kV Substation"), idx, iso="NESO")
    assert (m.method, m.reason) == ("none", "non_us")


def test_build_crosswalk_and_rates():
    ercot = pd.DataFrame(
        {
            "raw": [
                json.dumps({"Interconnection Location": "39010 138kV HASTINGS"}),
                json.dumps({"Interconnection Location": "39010 138kV HASTINGS"}),
                {"Interconnection Location": "Tap 138kV 7100 Burnet - 7529 Bertram"},
                {"Interconnection Location": None},
            ],
            "state": ["TX", "TX", "TX", "TX"],
        }
    )
    cw = pm.build_crosswalk({"us.iso.ercot.gen_queue": ercot}, _index(NODES))
    assert list(cw.columns) == pm.CROSSWALK_COLUMNS
    assert len(cw) == 2
    hit = cw[cw["method"] == "exact"].iloc[0]
    assert (hit["node_id"], hit["queue_rows"]) == ("n1", 2)
    rates = pm.match_rates(cw).iloc[0].to_dict()
    assert rates["distinct_poi"] == 2 and rates["matched"] == 1 and rates["line_tap"] == 1
    assert rates["match_rate"] == 0.5 and rates["match_rate_substation_kind"] == 1.0
