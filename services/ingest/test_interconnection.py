"""The POI key rule (`services/ingest/interconnection.py`, docs/25 §1.2), one case per clause, taken
from real register spellings on the dev store. The false merges it was changed to prevent are pinned
as "stay apart" cases."""

from __future__ import annotations

import pytest

from services.ingest.interconnection import parse_point, parse_raw, poi_text


def key(text: str, field: str = "Interconnection Location") -> str | None:
    parsed = parse_point(text, field=field)
    return parsed.name_key if parsed else None


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("59903 Bearkat 345kV", "Bearkat Substation 345 kV (#59903)"),
        ("WA Parish 345 kV Bus #44000", "44000 Wa Parish 345 kV"),
        ("Cortland - Fenner 115kV", "Fenner-Cortland 115 kV line"),
        ("Mohican to Battenkill 115 kV Line #15", "Battenkill to Mohican 115kV line#15"),
        (
            "Tap 345KV 1900 Comanche Peak to 1440 Comanche SS",
            "Tap 345KV 1900 Comanche Peak - 1440 Comanche Switch",
        ),
        ("Birds Landing Switchyard 230kV", "Birds Landing Sub 230 kV Bus"),
        ("Hartfield \x96 South Dow 34.5kV", "Hartfield - South Dow 34.5 kV"),
        (
            "Tap the 69 kV Bagwell (#1785) - Clarksville (#11784) line, ONCOR",
            "Tap 69kV 1785 Bagwell - 11784 Clarksville",
        ),
    ],
)
def test_spellings_of_one_point_group(a: str, b: str) -> None:
    assert key(a) is not None
    assert key(a) == key(b)


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("Gates 230 kV", "Gates 500 kV"),  # two buses at one substation
        ("8795 Roma 138kV", "8796 Roma 138kV"),  # two stated buses (a measured false merge)
        ("Tap 345kV 1906 Venus - 68091 Navarro", "Tap 345kV 1907 Venus - 68091 Navarro"),
        ("Bellota", "Bellota 115 kV"),  # no stated voltage is its own value
        ("Kramer-Lugo #1 220 kV", "Kramer-Lugo #2 220 kV"),  # two circuits
        ("New Cumnock 275kV", "Cumnock 275kV"),  # `new` is part of a place name
        ("Warners 69kV line", "Warners Substation 69kV"),  # a line out of a place is not its bus
    ],
)
def test_different_points_stay_apart(a: str, b: str) -> None:
    assert key(a) != key(b)


def test_fields_and_kinds() -> None:
    bearkat = parse_point("59903 Bearkat 345kV")
    assert bearkat is not None
    assert (bearkat.kind, bearkat.voltage_kv, bearkat.bus_number, bearkat.name_display) == (
        "substation",
        345.0,
        "59903",
        "59903 Bearkat 345kV",
    )
    tap = parse_point("Tap #1429 Jacksboro Substation 345kV")
    assert tap is not None
    assert tap.kind == "substation"
    assert tap.name_key.startswith("sub:jacksboro|345")
    coalburn = parse_point("Coalburn 400/132kV")
    assert coalburn is not None
    assert coalburn.voltage_kv == 400.0
    dash = parse_point("Hartfield \x96 South Dow 34.5kV")
    assert dash is not None
    assert dash.name_display == "Hartfield – South Dow 34.5kV"
    multi = parse_point("Sprain Brook 345 kV; Tremont 345 kV")
    assert multi is not None
    assert (multi.kind, multi.name_key[:4]) == ("unknown", "raw:")
    long_text = parse_point("x" * 120 + " 115kV")
    assert long_text is not None
    assert long_text.kind == "unknown"
    neso = parse_point("Sussex and Romney Connection Node A 400kV Substation", field="Connection Site")
    assert neso is not None
    assert neso.kind == "substation"
    brackets = parse_point("68091 (Navarro) (345)")
    assert brackets is not None
    assert brackets.bus_number == "68091"
    assert "navarro" in brackets.name_key
    cruce = parse_point("Tap 80079 (Cruce) - 5901 (San Miguel) 345 kV Ckt 1")
    assert cruce is not None
    assert cruce.kind == "line_tap"
    assert cruce.name_key.endswith("|c1")


@pytest.mark.parametrize("text", [None, 42, "", "TBD", "n/a", "34.5kV", "46kV line", "Line 123975", "; ,"])
def test_no_point_from_text_that_names_no_place(text: object) -> None:
    assert parse_point(text) is None


def test_raw_record_field_lookup() -> None:
    assert poi_text(None) is None
    assert poi_text({"Interconnection Location": "  "}) is None
    assert poi_text({"Connection Site": "Berkswell GSP"}) == ("Connection Site", "Berkswell GSP")
    assert parse_raw({"Queue ID": "1"}) is None
    assert parse_raw({"Connection Site": "Berkswell GSP"}) is not None
