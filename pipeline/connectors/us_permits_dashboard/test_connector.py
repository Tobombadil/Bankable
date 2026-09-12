"""Parser tests for us.permits_dashboard against a recorded Permitting Dashboard CSV."""

from __future__ import annotations

import json

import pytest

from conftest import connector_for, snapshot
from pipeline.connectors.base import ParseError

SOURCE_ID = "us.permits_dashboard"
URL = "https://data.permits.performance.gov/api/views/mcm3-xbid/rows.csv?accessType=DOWNLOAD"


@pytest.fixture(scope="module")
def parsed():
    c = connector_for(SOURCE_ID)
    raw = snapshot("permits_dashboard_projects.csv", URL, "text/csv")
    rows = c.parse(raw)
    return c, raw, rows, c.normalize(rows, raw)


def test_csv_groups_milestone_rows_into_one_row_per_project(parsed):
    """The fixture carries 12 distinct Project IDs; 2 (Aviation, Surface Transportation) are
    out of scope, so 10 project-level records come out of 224 milestone-level CSV rows."""
    _, _, rows, df = parsed
    assert len(rows) == 10
    assert len(df) == 10
    assert df["record_id"].is_unique


def test_out_of_scope_sectors_are_dropped(parsed):
    _, _, rows, _ = parsed
    assert {r["Project Sector"] for r in rows} <= {
        "Renewable Energy Production",
        "Electricity Transmission",
        "Pipelines",
        "Energy Storage",
        "Carbon Capture and Sequestration",
    }
    assert "72011" not in [r["Project ID"] for r in rows]  # Aviation
    assert "72861" not in [r["Project ID"] for r in rows]  # Surface Transportation


def test_milestones_are_nested_in_the_raw_payload(parsed):
    _, _, _rows, df = parsed
    row = df[df["source_record_id"] == "72996"].iloc[0]
    raw = json.loads(row["raw"])
    assert len(raw["milestones"]) == 21
    assert {"Milestone ID", "Milestone Type", "Action Status"} <= set(raw["milestones"][0])


def test_lifecycle_state_derives_from_project_status(parsed):
    _, _, _, df = parsed
    by_id = df.set_index("source_record_id")
    assert by_id.loc["72996", "lifecycle_state"] == "permitted"  # Complete
    assert by_id.loc["73756", "lifecycle_state"] == "studied"  # Paused
    assert by_id.loc["119321", "lifecycle_state"] == "announced"  # Planned
    assert by_id.loc["71031", "lifecycle_state"] == "cancelled"  # Cancelled
    assert by_id.loc["74081", "lifecycle_state"] == "studied"  # In Progress
    assert df["status_rule"].str.startswith("permits_dashboard.").all()
    assert set(df["lifecycle_state"]) <= {"announced", "filed", "studied", "permitted", "cancelled"}


def test_class_of_action_changed_maps_to_filed(parsed):
    c, _, _, _ = parsed
    from pipeline.connectors.canonical import harmonise_status

    state, rule = harmonise_status(c.status_key, {"status_raw": "Class of Action Changed"}, c.status_map)
    assert (state, rule) == ("filed", "permits_dashboard.map")


def test_location_is_the_state_column(parsed):
    _, _, _, df = parsed
    by_id = df.set_index("source_record_id")
    assert by_id.loc["72996", "state"] == "WY"


def test_kind_and_technology_come_from_project_type(parsed):
    _, _, _, df = parsed
    by_id = df.set_index("source_record_id")
    assert by_id.loc["71286", "kind"] == "transmission"
    assert by_id.loc["73651", "technology_raw"] == "Interstate Natural Gas Pipelines"


def test_capacity_is_never_populated_and_not_a_required_field(parsed):
    c, _, _, df = parsed
    assert df["capacity_mw"].isna().all()
    assert "capacity_mw" not in c.dq_required_fields


def test_source_url_is_the_per_project_permits_page(parsed):
    _, _, _, df = parsed
    row = df[df["source_record_id"] == "72996"].iloc[0]
    assert row["source_url"].startswith("https://www.permits.performance.gov/permitting-project/")


def test_a_layout_change_fails_closed():
    c = connector_for(SOURCE_ID)
    raw = snapshot("permits_dashboard_projects.csv", URL, "text/csv")
    bad = raw.__class__(
        content=b"Not,The,Right,Columns\n1,2,3,4\n",
        content_type="text/csv",
        url=URL,
        retrieved_at=raw.retrieved_at,
        http_status=200,
        ext="csv",
    )
    with pytest.raises(ParseError):
        c.parse(bad)
