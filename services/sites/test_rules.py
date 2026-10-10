"""The pure site rules (`services/sites/rules.py`): lead, labels, membership, review cap, stable ids."""

from __future__ import annotations

import datetime as dt

from services.db import models
from services.sites import rules
from services.sites.rules import Basis, Edge, PriorSite

UTC = dt.UTC


def b(pid: str, mw: float | None = 100.0, state: str = "filed", **kw: object) -> Basis:
    return Basis(public_id=pid, capacity_mw=mw, lifecycle_state=state, **kw)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------------------- lead
def test_lead_is_the_largest_active_member() -> None:
    ordered = rules.rank([b("prop_A", 50), b("prop_B", 400), b("prop_C", 200)])
    assert [x.public_id for x in ordered] == ["prop_B", "prop_C", "prop_A"]


def test_a_tie_on_capacity_goes_to_the_latest_filing_then_the_public_id() -> None:
    ordered = rules.rank(
        [
            b("prop_A", 100, filing_date="2021-01-01"),
            b("prop_B", 100, filing_date="2024-06-30"),
            b("prop_C", 100, filing_date=None),
            b("prop_D", 100, filing_date="2024-06-30"),
        ]
    )
    assert [x.public_id for x in ordered] == ["prop_B", "prop_D", "prop_A", "prop_C"]


def test_withdrawn_members_never_lead_while_an_active_one_exists() -> None:
    """The re-filing case: a 1 GW withdrawn request does not head the 300 MW live re-filing."""
    ordered = rules.rank(
        [b("prop_OLD", 1000, "withdrawn"), b("prop_NEW", 300, "filed"), b("prop_X", 2000, "cancelled")]
    )
    assert ordered[0].public_id == "prop_NEW"


def test_built_members_can_lead() -> None:
    assert rules.rank([b("prop_OP", 500, "built"), b("prop_ADD", 100, "filed")])[0].public_id == "prop_OP"


def test_when_every_member_is_inactive_the_largest_leads() -> None:
    ordered = rules.rank(
        [b("prop_A", 100, "withdrawn"), b("prop_B", 900, "withdrawn"), b("prop_C", None, "cancelled")]
    )
    assert ordered[0].public_id == "prop_B"


def test_a_member_without_capacity_ranks_after_every_stated_one() -> None:
    assert rules.rank([b("prop_A", None), b("prop_B", 0.5)])[0].public_id == "prop_B"


# -------------------------------------------------------------------------------------- labels
def test_lead_label() -> None:
    lead = b("prop_L")
    assert rules.label(lead, lead) == rules.LEAD_LABEL


def test_unit_of_is_a_shared_eia_plant_and_wins_over_technology() -> None:
    lead = b("prop_L", families=("nuclear",), plant_ids=("69798",))
    unit = b("prop_U", families=("nuclear",), plant_ids=("69798",))
    gas_unit_same_plant = b("prop_G", families=("thermal",), plant_ids=("69798",))
    assert rules.label(unit, lead) == rules.Label("unit_of", "shared_eia_plant", "high")
    assert rules.label(gas_unit_same_plant, lead).relation == "unit_of"


def test_co_located_is_a_disjoint_technology_family() -> None:
    lead = b("prop_L", families=("nuclear",), plant_ids=("69798",), stems=("matador",))
    gas = b("prop_G", families=("thermal",), plant_ids=("69799",), stems=("matador",))
    assert rules.label(gas, lead) == rules.Label("co_located", "different_technology_family", "high")
    # Solar + storage at a hybrid lead is not co-location: the families overlap.
    hybrid = b("prop_H", families=("solar", "storage"), stems=("darden",))
    storage = b("prop_S", families=("storage",), stems=("darden",), phases=("2",))
    assert rules.label(storage, hybrid).relation == "phase_of"


def test_co_located_without_direct_kinship_is_one_step_less_sure() -> None:
    lead = b("prop_L", families=("solar",), stems=("alpha",), sponsor_key="org-1")
    other = b("prop_O", families=("wind",), stems=("beta",), sponsor_key="org-2")
    assert rules.label(other, lead).confidence == "medium"


def test_phase_of_needs_a_shared_stem_and_differing_markers() -> None:
    lead = b("prop_L", families=("solar",), stems=("darden",), phases=("4",))
    both = b("prop_2", families=("solar",), stems=("darden",), phases=("2",))
    one = b("prop_1", families=("solar",), stems=("darden",), phases=())
    unrelated_name = b("prop_X", families=("solar",), stems=("robinson",), phases=("2",), sponsor_key="s")
    assert rules.label(both, lead) == rules.Label("phase_of", "differing_phase_markers", "high")
    assert rules.label(one, lead) == rules.Label("phase_of", "differing_phase_markers", "medium")
    assert rules.label(unrelated_name, lead).relation == "same_site"


def test_expansion_stated_by_the_source_named_or_inferred_from_an_operating_member() -> None:
    operating = b("prop_OP", 69, "built", families=("wind",), stems=("dersalloch",))
    stated = b("prop_EXT", 85, "permitted", families=("wind",), stems=("dersalloch",), mw_increase=True)
    assert rules.label(stated, operating) == rules.Label("expansion_of", "source_states_mw_increase", "high")
    assert rules.label(operating, stated) == rules.Label("expanded_by", "source_states_mw_increase", "high")
    named = b("prop_N", 20, "filed", families=("wind",), stems=("dersalloch",), expansion_marker=True)
    assert rules.label(named, operating) == rules.Label("expansion_of", "name_marks_expansion", "medium")
    later = b("prop_LATER", 50, "studied", families=("wind",), stems=("dersalloch",))
    assert rules.label(later, operating) == rules.Label(
        "expansion_of", "later_filing_at_operating_member", "medium"
    )
    big_new = b("prop_BIG", 500, "studied", families=("wind",), stems=("dersalloch",))
    assert rules.label(operating, big_new).relation == "expanded_by"


def test_refiling_superseded_and_unclear() -> None:
    lead = b("prop_NEW", 300, "filed", filing_date="2024-07-31", families=("storage",), stems=("roma",))
    earlier = b(
        "prop_OLD", 255, "withdrawn", filing_date="2023-05-18", families=("storage",), stems=("roma",)
    )
    later = b("prop_RE", 255, "withdrawn", filing_date="2025-01-01", families=("storage",), stems=("roma",))
    undated = b("prop_U", 255, "withdrawn", families=("storage",), stems=("roma",))
    twin = b("prop_T", 100, "filed", families=("storage",), stems=("roma",))
    assert rules.label(earlier, lead) == rules.Label("superseded_by", "withdrawn_earlier_filing", "medium")
    assert rules.label(later, lead) == rules.Label("refiling_of", "withdrawn_later_filing", "low")
    assert rules.label(undated, lead) == rules.Label("superseded_by", "withdrawn_filing_dates_unknown", "low")
    assert rules.label(twin, lead) == rules.Label("same_site", "grouped_relation_unclear", "low")


def test_label_site_puts_the_lead_first_with_rank_zero() -> None:
    out = rules.label_site([b("prop_A", 10), b("prop_B", 20)])
    assert [(x.basis.public_id, x.label.relation, x.rank, x.parent) for x in out] == [
        ("prop_B", "lead", 0, None),
        ("prop_A", "same_site", 1, None),
    ]
    assert [x.group_key for x in out] == ["proposal:prop_B", "proposal:prop_A"]
    assert rules.label_site([]) == []


def test_units_nest_under_their_own_plant_never_under_another_plants_lead() -> None:
    """Project Matador: the lead is a nuclear unit of plant 69798; the gas plant 69799 is a group of
    its own headed by its largest unit, co-located with the lead's group; each gas unit is
    `unit_of` the gas head, not of the nuclear lead."""
    nuclear = [
        b(f"prop_N{i}", 1117, "announced", plant_ids=("69798",), families=("nuclear",), stems=("matador",))
        for i in (1, 2)
    ]
    gas = [
        b("prop_G1", 260, "announced", plant_ids=("69799",), families=("thermal",), stems=("matador",)),
        b("prop_G2", 40, "announced", plant_ids=("69799",), families=("thermal",), stems=("matador",)),
        b("prop_G3", 40, "announced", plant_ids=("69799",), families=("thermal",), stems=("matador",)),
    ]
    loose = b("prop_Z", 5, "filed", families=("solar",), stems=("matador",), phases=("2",))
    out = {x.basis.public_id: x for x in rules.label_site([*gas, loose, *nuclear])}
    assert [x.basis.public_id for x in sorted(out.values(), key=lambda x: x.rank)] == [
        "prop_N1", "prop_N2", "prop_G1", "prop_G2", "prop_G3", "prop_Z",
    ]  # fmt: skip
    assert (out["prop_N1"].label.relation, out["prop_N1"].group_key) == ("lead", "eia:69798")
    assert (out["prop_N2"].label, out["prop_N2"].parent) == (rules.UNIT_LABEL, "prop_N1")
    assert (out["prop_G1"].label.relation, out["prop_G1"].parent) == ("co_located", None)
    for unit in ("prop_G2", "prop_G3"):
        assert (out[unit].label, out[unit].parent, out[unit].group_key) == (
            rules.UNIT_LABEL,
            "prop_G1",
            "eia:69799",
        )
    assert (out["prop_Z"].label.relation, out["prop_Z"].group_key) == ("co_located", "proposal:prop_Z")


def test_a_record_bridging_two_plants_joins_them_in_one_group() -> None:
    """Darden as merged today: one record holding generators of plants 69661-69664."""
    darden = b("prop_D", 1150, plant_ids=("69661", "69662"))
    unit = b("prop_U", 300, plant_ids=("69662",))
    other = b("prop_O", 300, plant_ids=("69661",))
    groups = rules.plant_groups([darden, unit, other])
    assert set(groups.values()) == {"eia:69661,69662"}


def test_group_labels_use_every_members_technology() -> None:
    """A storage head of a plant whose other units are solar is not co-located with a solar lead."""
    lead = b("prop_L", 500, families=("solar",), stems=("alpha",))
    head = b("prop_H", 200, plant_ids=("1",), families=("storage",), stems=("alpha",), phases=("2",))
    unit = b("prop_S", 100, plant_ids=("1",), families=("solar",), stems=("alpha",))
    out = {x.basis.public_id: x for x in rules.label_site([lead, head, unit])}
    assert out["prop_H"].label.relation == "phase_of"


def test_basis_round_trips_through_json() -> None:
    basis = b(
        "prop_A",
        12.5,
        "built",
        filing_date="2020-01-02",
        plant_ids=("1", "2"),
        stems=("x",),
        phases=("2",),
        expansion_marker=True,
        mw_increase=True,
        families=("solar", "storage"),
        sponsor_key="org",
    )
    assert Basis.from_json(basis.as_json()) == basis
    assert Basis.from_json({"public_id": "p", "families": None}).families is None


# ---------------------------------------------------------------------------------- membership
def test_components_are_transitive_and_keep_each_members_strongest_rule() -> None:
    edges = [
        Edge(0, 1, "poi_stem", {"stem": "darden"}),
        Edge(1, 2, "eia_plant", {"eia_plant_id": "69661"}),
        Edge(1, 2, "poi_sponsor", {"sponsor": "org"}),
        Edge(3, 4, "exact_point_sponsor", {"sponsor": "org2"}),
        Edge(5, 5, "eia_plant"),
    ]
    comps = rules.components(7, edges)
    assert [c.members for c in comps] == [[0, 1, 2], [3, 4]]
    first = comps[0]
    assert first.rule_of == {0: "poi_stem", 1: "eia_plant", 2: "eia_plant"}
    assert first.evidence_of[1]["rules"] == ["eia_plant", "poi_sponsor", "poi_stem"]
    assert first.evidence_of[1]["eia_plant_id"] == ["69661"]


def test_components_do_not_depend_on_edge_order() -> None:
    edges = [Edge(4, 2, "poi_sponsor"), Edge(0, 4, "eia_plant"), Edge(1, 3, "poi_stem")]
    assert [c.members for c in rules.components(5, edges)] == [
        c.members for c in rules.components(5, list(reversed(edges)))
    ]


def test_review_counts_one_plant_once() -> None:
    """Project Matador: 157 + 4 generators of two EIA plants is two things, not 161."""
    matador = [("69799",)] * 157 + [("69798",)] * 4
    assert rules.effective_size(matador) == 2
    assert not rules.needs_review(matador)
    chain = [()] * (rules.OVERSIZE_MEMBERS + 1)
    assert rules.needs_review(chain)
    assert not rules.needs_review([()] * rules.OVERSIZE_MEMBERS)


def test_vocabularies_match_the_check_constraints() -> None:
    assert rules.GROUPING_RULES == models.SITE_GROUPING_RULES
    assert rules.RELATIONS == models.SITE_RELATIONS
    assert rules.CONFIDENCES == models.SITE_CONFIDENCES
    assert rules.REVIEW_FLAGS == models.SITE_REVIEW_FLAGS


# ------------------------------------------------------------------------------------ stable ids
T0 = dt.datetime(2026, 10, 1, tzinfo=UTC)
T1 = dt.datetime(2026, 10, 2, tzinfo=UTC)


def test_a_rerun_keeps_every_id() -> None:
    prior = [PriorSite("s1", T0, frozenset({"a", "b"})), PriorSite("s2", T0, frozenset({"c", "d"}))]
    out = rules.inherit([frozenset({"c", "d"}), frozenset({"a", "b"})], prior)
    assert [p.site_id if p else None for p in out.assigned] == ["s2", "s1"]
    assert out.retired == []


def test_a_split_keeps_the_id_on_the_larger_part() -> None:
    prior = [PriorSite("s1", T0, frozenset({"a", "b", "c", "d", "e"}))]
    out = rules.inherit([frozenset({"a", "b"}), frozenset({"c", "d", "e"})], prior)
    assert [p.site_id if p else None for p in out.assigned] == [None, "s1"]


def test_a_merge_keeps_the_older_site_and_retires_the_other_with_a_successor() -> None:
    prior = [PriorSite("young", T1, frozenset({"a", "b"})), PriorSite("old", T0, frozenset({"c", "d"}))]
    out = rules.inherit([frozenset({"a", "b", "c", "d"})], prior)
    assert out.assigned[0] is not None and out.assigned[0].site_id == "old"
    assert [(p.site_id, succ) for p, succ in out.retired] == [("young", 0)]


def test_a_merge_with_unequal_overlap_keeps_the_larger_share() -> None:
    prior = [PriorSite("old", T0, frozenset({"a", "b"})), PriorSite("young", T1, frozenset({"c", "d", "e"}))]
    out = rules.inherit([frozenset({"a", "b", "c", "d", "e"})], prior)
    assert out.assigned[0] is not None and out.assigned[0].site_id == "young"


def test_a_site_that_lost_every_member_is_retired_without_successor_and_never_reused() -> None:
    prior = [PriorSite("gone", T0, frozenset({"a", "b"}))]
    out = rules.inherit([frozenset({"x", "y"})], prior)
    assert out.assigned == [None]
    assert [(p.site_id, succ) for p, succ in out.retired] == [("gone", None)]
