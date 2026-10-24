"""`postponed`: where the new lifecycle state sits in every count that groups states.

Owner decision 2026-10-24 (docs/00-PLAN.md, row "Owner decisions on the weekend list", item 3): EIA-860M's
"canceled or indefinitely postponed" units get their own state, counted neither as active nor as withdrawn or
cancelled. docs/21 §7.1 lists each grouping and the answer for it; this file holds the code to that list. Each
group is read from the module that counts with it, so a later edit that drops `postponed` into an active,
withdrawn or built group, or leaves it out of a vocabulary that must name it, fails here.
"""

from __future__ import annotations

import datetime as dt
import pathlib
from typing import Any

import pandas as pd
import pytest
import yaml

from services.resolve.survivorship import Member

REPO = pathlib.Path(__file__).resolve().parents[1]
STATE = "postponed"


# ------------------------------------------------------------------------------------- vocabularies
def test_every_vocabulary_that_lists_lifecycle_states_names_postponed() -> None:
    from services.api.admin_posts import LIFECYCLE_STATES as INTAKE_STATES
    from services.api.app import LIFECYCLE_STATE_VALUES
    from services.api.change_rule import TO_STATES
    from services.db.models import LIFECYCLE_STATES
    from services.labels import LIFECYCLE_LABELS
    from web.submit import LIFECYCLE_CHOICES

    spec = yaml.safe_load((REPO / "api/openapi.yaml").read_text(encoding="utf-8"))
    status_map = yaml.safe_load((REPO / "pipeline/status_map.yaml").read_text(encoding="utf-8"))
    vocabulary = yaml.safe_load((REPO / "data/vocabulary/lifecycle_states.yaml").read_text(encoding="utf-8"))
    to_state = spec["components"]["parameters"]["ToState"]["schema"]["items"]["enum"]
    for name, values in {
        "models.LIFECYCLE_STATES": LIFECYCLE_STATES,
        "app.LIFECYCLE_STATE_VALUES": LIFECYCLE_STATE_VALUES,
        "admin_posts.LIFECYCLE_STATES (intake)": INTAKE_STATES,
        "submit.LIFECYCLE_CHOICES": LIFECYCLE_CHOICES,
        "change_rule.TO_STATES": TO_STATES,
        "openapi LifecycleState": spec["components"]["schemas"]["LifecycleState"]["enum"],
        "openapi ToState (to_state)": to_state,
        "status_map canonical_states": status_map["canonical_states"],
        "vocabulary lifecycle_states": vocabulary["lifecycle_states"],
    }.items():
        assert STATE in values, name
    assert LIFECYCLE_LABELS[STATE] == "Postponed"
    # Appended, as an addition to an open vocabulary is (docs/04 API-7): no existing position moves.
    assert LIFECYCLE_STATES[-1] == STATE and list(LIFECYCLE_STATE_VALUES)[-1] == STATE


# ------------------------------------------------------------------------- the groups it is not in
def test_postponed_is_in_no_active_withdrawn_or_built_group() -> None:
    import pipeline.diff as diff
    import services.api.interconnection_points as points
    import services.api.my_projects as my_projects
    import services.api.org_pipeline as org_pipeline
    import services.api.slippage as slippage
    import services.resolve.survivorship as survivorship
    import services.sites.rules as site_rules
    import services.social.editorial as editorial
    import services.trends.snapshot as trends
    import web.my_projects as web_my_projects
    import web.viewmodels as viewmodels

    groups = {
        "web ACTIVE_PROPOSAL_STATES (the default view)": viewmodels.ACTIVE_PROPOSAL_STATES,
        "web WITHDRAWN_PROPOSAL_STATES (include_withdrawn)": viewmodels.WITHDRAWN_PROPOSAL_STATES,
        "org_pipeline ACTIVE_LIFECYCLE_STATES (active pipeline)": org_pipeline.ACTIVE_LIFECYCLE_STATES,
        "slippage SLIP_ACTIVE_STATES": slippage.SLIP_ACTIVE_STATES,
        "interconnection points active": points.ACTIVE_LIFECYCLE_STATES,
        "interconnection points withdrawn": points.WITHDRAWN_LIFECYCLE_STATES,
        "interconnection points built": points.BUILT_LIFECYCLE_STATES,
        "trends ACTIVE_STATES": trends.ACTIVE_STATES,
        "trends WITHDRAWN_STATES": trends.WITHDRAWN_STATES,
        "trends BUILT_STATES": trends.BUILT_STATES,
        "trends UNKNOWN_STATES": trends.UNKNOWN_STATES,
        "diff TERMINAL (a `withdrawn` event)": diff.TERMINAL,
        "survivorship TERMINAL_STATES": survivorship.TERMINAL_STATES,
        "survivorship LIFECYCLE_RANK (the ladder)": survivorship.LIFECYCLE_RANK,
        "sites INACTIVE_STATES": site_rules.INACTIVE_STATES,
        "tracker CLOSED_PROPOSAL_STATES (api)": my_projects.CLOSED_PROPOSAL_STATES,
        "tracker CLOSED_PROPOSAL_STATES (web)": web_my_projects.CLOSED_PROPOSAL_STATES,
        "social LIFECYCLE_LADDER": editorial.LIFECYCLE_LADDER,
    }
    for name, group in groups.items():
        assert STATE not in group, name


# -------------------------------------------------------------------- where a live project is needed
def test_postponed_is_not_live_where_a_live_project_is_needed() -> None:
    """Not in an active group is not enough: the matcher and the site lead read "live" as "not in an
    excluded or inactive set", so `postponed` must be named in each of those sets or tiers."""
    import services.resolve.survivorship as survivorship
    import services.sites.rules as site_rules
    import services.social.editorial as editorial
    from services.match.rules import load_rules

    # Matching (match-rules@v3): a postponed proposal is never offered against an open notice.
    assert STATE in load_rules().timing.proposal_excluded_states
    # A record's MW and name: a postponed generator never joins a live record's (survivorship `_quantity`).
    assert STATE in survivorship.POSTPONED_STATES
    # Social: a record a source first lists as postponed earns no "Newly listed" post.
    assert STATE in editorial.UNPOSTED_NEW_STATES
    # Site lead order: its own tier, after every live member and before withdrawn or cancelled ones;
    # and never a live addition (`expansion_of`, `later_filing_at_operating_member`).
    assert STATE in site_rules.POSTPONED_STATES
    member = site_rules.Basis(public_id="p", capacity_mw=1.0, lifecycle_state=STATE)
    assert not member.live and member.lead_tier == site_rules.LEAD_TIER_POSTPONED
    assert site_rules.LEAD_TIER_LIVE < site_rules.LEAD_TIER_POSTPONED < site_rules.LEAD_TIER_INACTIVE


# ------------------------------------------------------------------------------ where it is counted
def test_where_postponed_is_counted() -> None:
    import services.api.front as front
    import services.api.interconnection_points as points
    import services.api.org_pipeline as org_pipeline
    import web.viewmodels as viewmodels
    from web.unlisted import unlisted_rule_applies

    # A grid point's and a site's buckets: `other`, with `unknown` (docs/21 §7.1).
    assert points.bucket_of(STATE) == "other"
    # The front page counts it as an "other" state: every record, listed or not, never as active.
    assert STATE in front.OTHER_LIFECYCLE_STATES
    assert STATE in front.PULSE_STATES  # a move to it is reported in the pulse, under its own state
    assert STATE in org_pipeline.STATE_ORDER  # the company pipeline lists it, after the active states
    assert org_pipeline.STATE_ORDER.index(STATE) > max(
        org_pipeline.STATE_ORDER.index(s) for s in org_pipeline.ACTIVE_LIFECYCLE_STATES
    )
    # The list and map: one status box of its own; every page and the sitemap can reach it.
    assert ("postponed", "Postponed", ("postponed",)) in viewmodels.PROPOSAL_STATUS_GROUPS
    assert STATE in viewmodels.ALL_PROPOSAL_LIFECYCLE_STATES
    # A view that selects it hides nothing (the unlisted rule is for active-only views).
    assert not unlisted_rule_applies({"lifecycle_state": STATE})
    assert not unlisted_rule_applies({"lifecycle_state": "announced,postponed"})


def test_every_status_group_has_a_chip_family() -> None:
    """The company page reads `STATUS_GROUP_FAMILY[key]` for each group with records: a group with no
    entry is a KeyError on the page of any company with a postponed project."""
    import web.build_data as build_data
    import web.org_pipeline as web_org_pipeline
    import web.viewmodels as viewmodels

    assert {k for k, _l, _s in viewmodels.PROPOSAL_STATUS_GROUPS} <= set(web_org_pipeline.STATUS_GROUP_FAMILY)
    assert web_org_pipeline.STATUS_GROUP_FAMILY["postponed"] == "danger"
    # Stopped, like a frozen notice: the danger family, its own word beside the glyph (D-5).
    assert viewmodels.LIFECYCLE_FAMILY[STATE] == build_data.LIFECYCLE_FAMILY[STATE] == "danger"


# --------------------------------------------------------------------------------- events and merge
def _frame(state: str) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "record_id": "us.eia.860m:1-1",
                "source_id": "us.eia.860m",
                "lifecycle_state": state,
                "status_raw": state,
                "capacity_mw": 10.0,
                "proposed_cod": pd.NaT,
            }
        ]
    )


@pytest.mark.parametrize("before", ["studied", "permitted", "under_construction", "announced"])
def test_a_move_to_postponed_is_a_status_change_never_a_withdrawal(before: str) -> None:
    from pipeline.diff import diff_snapshots

    events = diff_snapshots(_frame(before), _frame(STATE), observed_at="2026-10-24T00:00:00Z")
    lifecycle = events[events["field"] == "lifecycle_state"]
    assert list(lifecycle["event_type"]) == ["status_change"]
    assert list(lifecycle["after"]) == [STATE]


def _member(source_id: str, category: str, record_id: str, state: str, mw: float) -> Member:
    return Member(
        source_id=source_id,
        category=category,
        record_id=record_id,
        retrieved_at=dt.datetime(2026, 10, 24, tzinfo=dt.UTC),
        licence_id=f"{source_id}#l",
        values={"lifecycle_state": state, "status_raw": state, "capacity_mw": mw, "technology": "solar"},
    )


def test_a_member_on_the_ladder_outranks_a_postponed_one_and_a_postponed_one_alone_stands() -> None:
    """Survivorship (docs/22 §23.1): `postponed` is off the ladder, so any member still on it decides the
    record's state; a record whose only members are postponed is postponed."""
    from services.resolve.survivorship import survive

    queue = _member("us.iso.caiso.gen_queue", "generation_queue", "caiso:1", "studied", 100.0)
    unit = _member("us.eia.860m", "registry", "us.eia.860m:1-1", STATE, 100.0)
    assert survive([queue, unit])["lifecycle_state"].value == "studied"
    assert survive([unit])["lifecycle_state"].value == STATE


# ------------------------------------------------------------------------------------ the notice
def test_the_view_notice_counts_postponed_with_unknown_and_names_both() -> None:
    """The list's and the map's notice count every state outside the view in four calls (`lifecycle_counts`);
    its `unknown` bucket is every other state, so it holds `postponed` too. The words say so: a postponed
    project is never called unknown-state."""
    import re

    from web.app import OTHER_PROPOSAL_STATES
    from web.page import templates

    assert set(OTHER_PROPOSAL_STATES) == {"built", "unknown", "postponed"}
    macros: Any = templates.env.get_template("_macros.html").module
    breakdown = {"active": 10, "withdrawn": 2, "built": 3, "unknown": 1745, "other": 1748}
    for surface in ("list", "map"):
        html = str(macros.active_states_notice(breakdown, False, surface=surface))
        text = " ".join(re.sub(r"<[^>]+>", "", html).split())
        assert "1,745 postponed or unknown-state" in text, (surface, text)
    with_withdrawn = str(macros.active_states_notice(breakdown, True, surface="list"))
    text = " ".join(re.sub(r"<[^>]+>", "", with_withdrawn).split())
    assert "1,745 postponed or unknown-state proposals are outside this view" in text
