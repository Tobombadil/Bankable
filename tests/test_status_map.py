"""Status harmonisation: pipeline/status_map.yaml + normalize.harmonise_status."""

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from pipeline.normalize import (
    classify_tech,
    harmonise_status,
    load_status_map,
    norm_county,
    norm_name,
    norm_org,
    norm_state,
)

SM = load_status_map()
CANON = set(SM["canonical_states"])


def test_every_mapped_value_is_canonical():
    for src, cfg in SM["sources"].items():
        for raw, state in cfg["map"].items():
            assert state in CANON, f"{src}: {raw!r} -> {state!r} not canonical"
        for rule in cfg.get("refine") or []:
            assert rule["then"] in CANON, f"{src}: rule {rule['id']} -> {rule['then']!r}"
            assert rule["id"].startswith(src + "."), rule["id"]


def test_all_six_sources_present():
    assert set(SM["sources"]) == {"caiso", "ercot", "spp", "nyiso", "isone", "eia860m"}


@pytest.mark.parametrize(
    "src,ctx,expected,rule",
    [
        ("caiso", {"status_raw": "ACTIVE"}, "studied", "caiso.map"),
        (
            "caiso",
            {"status_raw": "ACTIVE", "ia_status": "Executed"},
            "contracted",
            "caiso.active_ia_executed",
        ),
        (
            "caiso",
            {"status_raw": "ACTIVE", "ia_status": "Filed Unexecuted"},
            "permitted",
            "caiso.active_ia_filed",
        ),
        ("caiso", {"status_raw": "COMPLETED", "ia_status": "Executed"}, "built", "caiso.map"),
        ("caiso", {"status_raw": "WITHDRAWN", "ia_status": "Executed"}, "withdrawn", "caiso.map"),
        ("ercot", {"status_raw": "Active"}, "studied", "ercot.map"),
        (
            "ercot",
            {"status_raw": "Active", "approved_for_energization": "yes"},
            "under_construction",
            "ercot.energized",
        ),
        ("ercot", {"status_raw": "Completed", "ia_signed": "yes"}, "built", "ercot.map"),
        ("nyiso", {"status_raw": "Withdrawn"}, "withdrawn", "nyiso.map"),
        ("nyiso", {"status_raw": "Completed"}, "built", "nyiso.map"),
        (
            "isone",
            {"status_raw": "Withdrawn", "project_status": "Under Study"},
            "withdrawn",
            "isone.withdrawn_wins",
        ),
        ("isone", {"status_raw": "Completed", "project_status": "In Service"}, "built", "isone.in_service"),
        (
            "isone",
            {"status_raw": "Completed", "project_status": "Under Construction"},
            "under_construction",
            "isone.under_construction",
        ),
        ("isone", {"status_raw": "Active", "ia_status": "Executed"}, "contracted", "isone.ia_executed"),
        ("isone", {"status_raw": "Active"}, "studied", "isone.map"),
        (
            "eia860m",
            {"status_raw": "(P) Planned for installation, but regulatory approvals not initiated"},
            "announced",
            "eia860m.map",
        ),
        (
            "eia860m",
            {"status_raw": "(L) Regulatory approvals pending. Not under construction"},
            "filed",
            "eia860m.map",
        ),
        (
            "eia860m",
            {"status_raw": "(T) Regulatory approvals received. Not under construction"},
            "permitted",
            "eia860m.map",
        ),
        (
            "eia860m",
            {"status_raw": "(U) Under construction, less than or equal to 50 percent complete"},
            "under_construction",
            "eia860m.map",
        ),
        (
            "eia860m",
            {"status_raw": "(V) Under construction, more than 50 percent complete"},
            "under_construction",
            "eia860m.map",
        ),
        (
            "eia860m",
            {"status_raw": "(TS) Construction complete, but not yet in commercial operation"},
            "under_construction",
            "eia860m.map",
        ),
    ],
)
def test_harmonise(src, ctx, expected, rule):
    assert harmonise_status(src, ctx, SM) == (expected, rule)


@pytest.mark.parametrize(
    "orig,status,expected",
    [
        ("WITHDRAWN", "Withdrawn", "withdrawn"),
        ("TERMINATED", None, "cancelled"),
        ("IA FULLY EXECUTED/COMMERCIAL OPERATION", "Completed", "built"),
        ("IA FULLY EXECUTED/ON SCHEDULE", "Completed", "contracted"),  # NOT built (gridstatus says Completed)
        ("IA FULLY EXECUTED/ON SUSPENSION", "Completed", "contracted"),
        ("IA PENDING", "Active", "studied"),
        ("DISIS STAGE", "Active", "studied"),
        ("FACILITY STUDY STAGE", None, "studied"),
        ("SPECIAL STUDY", None, "studied"),
        ("ERAS", None, "studied"),
        ("ICS", None, "studied"),
        (None, "Active", "studied"),  # falls back to gridstatus Status when original is blank
        (None, None, "unknown"),
    ],
)
def test_spp_uses_original_status(orig, status, expected):
    state, _ = harmonise_status("spp", {"status_raw": status, "status_original": orig}, SM)
    assert state == expected


def test_unmapped_and_blank_and_unknown_source():
    assert harmonise_status("caiso", {"status_raw": "SOMETHING NEW"}, SM) == ("unknown", "caiso.unmapped")
    assert harmonise_status("caiso", {"status_raw": ""}, SM) == ("unknown", "caiso.blank")
    assert harmonise_status("caiso", {"status_raw": None}, SM) == ("unknown", "caiso.blank")
    assert harmonise_status("pjm", {"status_raw": "Active"}, SM) == ("unknown", "no_source_config")


def test_refine_rules_take_precedence_over_map_and_run_in_order():
    # isone: a withdrawn row with Project Status "In Service" must stay withdrawn (rule order)
    assert (
        harmonise_status("isone", {"status_raw": "Withdrawn", "project_status": "In Service"}, SM)[0]
        == "withdrawn"
    )


@pytest.mark.parametrize(
    "raw,tech,kind",
    [
        ("Solar - Photovoltaic Solar", "solar", "generation"),
        ("Storage + Photovoltaic", "solar_storage", "generation"),
        ("Photovoltaic + Storage", "solar_storage", "generation"),
        ("Other - Battery Energy Storage", "storage", "storage"),
        ("SUN BAT", "solar_storage", "generation"),
        ("BAT", "storage", "storage"),
        ("WND", "wind", "generation"),
        ("Gas - Combined-Cycle", "gas_cc", "generation"),
        ("Gas - Combustion (gas) Turbine, but not part of a Combined-Cycle", "gas_ct", "generation"),
        ("Natural Gas Fired Combined Cycle", "gas_cc", "generation"),
        ("Hydroelectric Pumped Storage", "pumped_storage", "storage"),
        ("AC Transmission", "transmission", "transmission"),
        ("Batteries", "storage", "storage"),
        (None, "unknown", "other"),
        ("", "unknown", "other"),
    ],
)
def test_classify_tech(raw, tech, kind):
    assert classify_tech(raw) == (tech, kind)


def test_normalisers():
    assert norm_state("Texas") == "TX" and norm_state("tx") == "TX" and norm_state(None) is None
    assert (
        norm_county("Weld County") == "WELD"
        and norm_county("Kit Carson/Cheyenne Counties") == "KIT CARSON/CHEYENNE"
    )
    assert norm_org("Wichita Solar I, LLC") == norm_org("Wichita Solar I LLC")
    assert norm_name("Chazy Lake BESS (NYISO-C24-308)") == "CHAZY LAKE"
