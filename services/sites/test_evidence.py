"""Grouping evidence (`services/sites/evidence.py`): stems, markers, keys and rules a-d."""

from __future__ import annotations

import pytest

from services.sites import evidence
from services.sites.evidence import Candidate

# A point in the CAISO Imperial Valley area; about 111 m per 0.001 degree of latitude.
LON, LAT = -115.5, 32.7


def cand(i: int, name: str, **kw: object) -> Candidate:
    defaults: dict[str, object] = {"technology": "solar", "capacity_mw": 100.0, "lifecycle_state": "filed"}
    defaults.update(kw)
    return Candidate(proposal_id=i, public_id=f"prop_{i:04d}", name=name, **defaults)  # type: ignore[arg-type]


def rules_between(cands: list[Candidate]) -> set[tuple[int, int, str]]:
    return {(min(e.a, e.b), max(e.a, e.b), e.rule) for e in evidence.grouping_edges(cands)}


# ------------------------------------------------------------------------------------- names
@pytest.mark.parametrize(
    ("name", "stem"),
    [
        ("Darden IV Solar", "darden"),
        ("DARDEN", "darden"),
        ("Keys Hollow Storage Phase II SLF", "keys hollow"),
        ("Project Matador Gas Plant (PMG)", "matador"),
        ("Bishop Ranch - BR 3-GG", "bishop ranch"),
        ("Nockenut Springs Solar 2", "nockenut springs"),
        ("Dersalloch Wind Farm Extension", "dersalloch"),
        ("H&C WILBARGER POWER I", "wilbarger"),
        ("Solar 2", None),
        ("Sun", None),
        ("", None),
        (None, None),
    ],
)
def test_name_stem(name: str | None, stem: str | None) -> None:
    assert evidence.name_stem(name) == stem


def test_phase_markers_and_expansion_words() -> None:
    assert evidence.phase_markers("Darden II Solar") == ("2",)
    assert evidence.phase_markers("Solar Phase B") == ("B",)
    assert evidence.phase_markers("Darden") == ()
    assert evidence.has_expansion_marker("Dersalloch Wind Farm Extension")
    assert evidence.has_expansion_marker("Myrtle Solar Repower")
    assert not evidence.has_expansion_marker("Myrtle Solar")


@pytest.mark.parametrize(
    ("name", "markers"),
    [
        # Roman or Arabic numerals with a letter suffix (lane S2: "St Gall IIIA" was read as no marker).
        ("St Gall IIIA", ("3A",)),
        ("st gall IIIB Storage", ("3B",)),
        ("SAN JOAQUIN 2A", ("2A",)),
        ("Bluepoint Wind 3b", ("3B",)),
        ("LI Cable - Phase 2a", ("2A",)),
        ("Keys Hollow Phase IIA Solar LLC", ("2A",)),
        ("Darden VIIIB", ("8B",)),
        ("Unit 4c", ("4C",)),
        # Plain markers are read as before.
        ("Darden II Solar", ("2",)),
        ("Phase 2", ("2",)),
        # Not phases: state codes, words, a token inside a name, a first word.
        ("Solar IA", ()),
        ("Coastal VA Storage", ()),
        ("Via Verde Solar", ()),
        ("Lake Iva Solar", ()),
        ("Solar Via Roma", ()),
        ("Route 2A Crossing", ()),
        ("2A Solar", ()),
        ("Bishop Ranch - BR 3-GG", ("3",)),
    ],
)
def test_phase_markers_with_a_letter_suffix(name: str, markers: tuple[str, ...]) -> None:
    assert evidence.phase_markers(name) == markers


def test_a_suffixed_marker_leaves_the_stem_like_a_plain_numeral() -> None:
    """ "St Gall IIIA" and "St Gall IIIB" share a stem now, so they can be phases of one project."""
    assert evidence.name_stem("St Gall IIIA") == "gall"
    assert evidence.name_stem("st gall IIIB Storage") == "gall"
    assert evidence.name_stem("Via Verde Solar") == "via verde"
    assert evidence.name_stem("Solar Via Roma") == "via roma"


def test_eia_plant_id_from_860m_record_ids_only() -> None:
    assert evidence.eia_plant_id("us.eia.860m", "69661-IPD1B") == "69661"
    assert evidence.eia_plant_id("us.eia.860m", "plant:1239") == "1239"
    assert evidence.eia_plant_id("us.eia.860m", "ABC-1") is None
    assert evidence.eia_plant_id("us.iso.caiso.gen_queue", "1949") is None


def test_neso_mw_increase_needs_a_connected_project() -> None:
    assert evidence.neso_mw_increase({"MW Connected": "69", "MW Increase / Decrease": "16"})
    assert not evidence.neso_mw_increase({"MW Connected": "0", "MW Increase / Decrease": "240"})
    assert not evidence.neso_mw_increase({"MW Connected": "x"})
    assert not evidence.neso_mw_increase(None)


def test_technology_families() -> None:
    assert evidence.technology_families("solar_storage") == ("solar", "storage")
    assert evidence.technology_families("gas_cc") == ("thermal",)
    assert evidence.technology_families(None) is None
    assert evidence.technology_families("not_a_technology") is None


# ---------------------------------------------------------------------------------- sponsors
def test_sponsor_relation() -> None:
    a = cand(1, "Alpha Solar", sponsor_key="o1", sponsor_name="NextEra Energy Resources")
    same = cand(2, "Alpha Storage", sponsor_key="o1", sponsor_name="NextEra Energy Resources")
    other = cand(3, "Alpha Solar 2", sponsor_key="o2", sponsor_name="Invenergy LLC")
    house = cand(4, "Matador", sponsor_key="o3", sponsor_name="Fermi America")
    house2 = cand(5, "Matador", sponsor_key="o4", sponsor_name="Fermi Nuclear")
    family = cand(6, "Alpha Wind", sponsor_key="o5", sponsor_name="Zeta Holdings", sponsor_family="p1")
    family2 = cand(7, "Alpha Wind 2", sponsor_key="o6", sponsor_name="Omega LLC", sponsor_family="p1")
    vehicle = cand(8, "Cowboy Solar II", sponsor_key="o7", sponsor_name="TGE Wyoming 225")
    vehicle2 = cand(9, "Cowboy Solar I", sponsor_key="o8", sponsor_name="Cowboy Solar I")
    unknown = cand(10, "Alpha Solar 3")
    assert evidence.sponsor_relation(a, same) == "same"
    assert evidence.sponsor_relation(a, other) == "conflict"
    assert evidence.sponsor_relation(house, house2) == "compatible"
    assert evidence.sponsor_relation(family, family2) == "compatible"
    assert evidence.sponsor_relation(vehicle, vehicle2) == "compatible"
    assert evidence.sponsor_relation(a, unknown) == "compatible"


# ------------------------------------------------------------------------------- rule a: plant
def test_rule_a_groups_generators_of_one_eia_plant() -> None:
    cands = [
        cand(0, "Kingston", plant_ids=frozenset({"3407"})),
        cand(1, "Kingston", plant_ids=frozenset({"3407"})),
        cand(2, "Elsewhere", plant_ids=frozenset({"9999"})),
    ]
    assert rules_between(cands) == {(0, 1, "eia_plant")}


# ------------------------------------------------------------------------- rule b: exact point
def exact(i: int, name: str, dlat: float = 0.0, **kw: object) -> Candidate:
    return cand(i, name, precision="exact", lon=LON, lat=LAT + dlat, **kw)


def test_rule_b_same_exact_point_and_sponsor_or_stem() -> None:
    cands = [
        exact(
            0, "Project Matador Nuclear", sponsor_key="o1", sponsor_name="Fermi Nuclear", technology="nuclear"
        ),
        exact(
            1,
            "Project Matador Gas Plant (PMG)",
            sponsor_key="o2",
            sponsor_name="Fermi America",
            technology="gas_ct",
        ),
        exact(2, "Quantum II BESS", dlat=0.001, sponsor_key="o3", sponsor_name="Intersect USA"),
        exact(3, "Unrelated Name", dlat=0.001, sponsor_key="o3", sponsor_name="Intersect USA"),
    ]
    found = rules_between(cands)
    assert (0, 1, "exact_point_stem") in found
    assert (2, 3, "exact_point_sponsor") in found


def test_rule_b_unrelated_developers_at_one_exact_point_do_not_group() -> None:
    cands = [
        exact(0, "Iron BESS", sponsor_key="o1", sponsor_name="Panoche BESS LLC"),
        exact(1, "Midway BESS", sponsor_key="o2", sponsor_name="Midway BESS LLC"),
        # A shared stem is no evidence when two named developers conflict.
        exact(2, "Sandy Creek Solar", sponsor_key="o3", sponsor_name="NextEra Energy"),
        exact(3, "Sandy Creek Storage", sponsor_key="o4", sponsor_name="Invenergy"),
        # Unknown sponsors and different names: two data centres at one address.
        exact(4, "MICROSOFT", technology="load"),
        exact(5, "AMAZON DATA SERVICES - DCA-51", technology="load"),
    ]
    assert rules_between(cands) == set()


def test_rule_b_tolerance_is_200_metres() -> None:
    near = [exact(0, "Pescara 1"), exact(1, "Pescara 2", dlat=0.0015)]  # ~167 m
    far = [exact(0, "Pescara 1"), exact(1, "Pescara 2", dlat=0.0030)]  # ~334 m
    assert rules_between(near) == {(0, 1, "exact_point_stem")}
    assert rules_between(far) == set()


def test_rule_d_centroids_group_nothing() -> None:
    cands = [
        cand(0, "Darden I", precision="county_centroid", lon=LON, lat=LAT, sponsor_key="o1"),
        cand(1, "Darden II", precision="county_centroid", lon=LON, lat=LAT, sponsor_key="o1"),
        cand(2, "Darden III", precision="state_centroid", lon=LON, lat=LAT, sponsor_key="o1"),
        cand(3, "Darden IV", precision="unknown", lon=None, lat=None, sponsor_key="o1"),
    ]
    assert rules_between(cands) == set()


# ------------------------------------------------------------------------- rule c: POI
def poi(
    i: int, name: str, point: str = "poi-iv", point_name: str = "Imperial Valley 230kV", **kw: object
) -> Candidate:
    return cand(i, name, poi_key=point, poi_name=point_name, **kw)


def test_rule_c_same_poi_and_sponsor_or_shared_stem() -> None:
    cands = [
        poi(0, "Darden I Solar"),
        poi(1, "Darden II Solar"),
        poi(2, "Hiru Solar", sponsor_key="o1", sponsor_name="Hiru Dev"),
        poi(3, "Kiwi Storage", sponsor_key="o1", sponsor_name="Hiru Dev"),
    ]
    assert rules_between(cands) == {(0, 1, "poi_stem"), (2, 3, "poi_sponsor")}


def test_rule_c_unrelated_developers_at_one_poi_do_not_group() -> None:
    """46 records at CAISO Imperial Valley 230 kV, 45 with no named developer: competing requests."""
    cands = [
        poi(i, f"Project {name}", technology=tech)
        for i, (name, tech) in enumerate(
            [("Alpha", "solar"), ("Bravo", "storage"), ("Charlie", "solar"), ("Delta", "gas_ct")]
        )
    ]
    cands.append(poi(9, "Echo Solar", sponsor_key="o1", sponsor_name="NextEra"))
    cands.append(poi(10, "Echo Storage", sponsor_key="o2", sponsor_name="Invenergy"))
    assert rules_between(cands) == set()


def test_rule_c_a_stem_that_is_only_the_points_name_is_no_evidence() -> None:
    cands = [poi(0, "Imperial Valley Solar 1"), poi(1, "Imperial Valley Storage")]
    assert rules_between(cands) == set()
    other_points = [
        poi(0, "Imperial Valley Solar 1", point="a"),
        poi(1, "Imperial Valley Solar 2", point="b"),
    ]
    assert rules_between(other_points) == set()


def test_a_merged_records_link_names_count_as_its_stems() -> None:
    c = cand(0, "Darden IV Solar", names=("DARDEN", "Darden I Solar"))
    assert c.stems == frozenset({"darden"})
    assert c.basis().stems == ("darden",)
