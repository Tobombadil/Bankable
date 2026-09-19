"""Parser tests for gb.neso.tec_register against a recorded TEC register CSV."""

from __future__ import annotations

import pytest

from conftest import connector_for, snapshot
from pipeline.connectors.base import ParseError

SOURCE_ID = "gb.neso.tec_register"
URL = "https://api.neso.energy/dataset/resource/tec-register.csv"


@pytest.fixture(scope="module")
def parsed():
    c = connector_for(SOURCE_ID)
    raw = snapshot("neso_tec_register.csv", URL, "text/csv")
    rows = c.parse(raw)
    return c, raw, rows, c.normalize(rows, raw)


def test_bom_prefixed_csv_parses(parsed):
    _, _, rows, _ = parsed
    assert {"Project Name", "Customer Name", "Project ID", "Project Status", "Plant Type"} <= set(rows[0])


def test_status_map_is_the_per_connector_one(parsed):
    c, _, _, df = parsed
    assert c.status_map_path is not None and c.status_map_path.name == "status_map.yaml"
    assert set(df["lifecycle_state"]) <= {"filed", "studied", "permitted", "under_construction", "built"}
    assert df["status_rule"].str.startswith("neso_tec.").all()


def test_staged_projects_key_on_project_id_plus_stage(parsed):
    _, _, _, df = parsed
    staged = df[df["source_record_id"].str.contains("/1.00|/2.00", regex=True)]
    assert len(staged) >= 2
    assert not df["record_id"].duplicated().any()


def test_unstaged_rows_sharing_a_project_id_get_distinct_hashed_keys(parsed):
    _, _, rows, df = parsed
    immingham = [i for i, r in enumerate(rows) if r["Project ID"] == "a0l4L0000005im7QAA"]
    assert len(immingham) == 2
    keys = set(df.iloc[immingham]["source_record_id"])
    assert len(keys) == 2


def test_multi_technology_plant_types_classify(parsed):
    _, _, _, df = parsed
    assert df["technology"].notna().all()
    assert set(df["iso"]) == {"NESO"}


def test_html_instead_of_csv_is_a_parse_error():
    c = connector_for(SOURCE_ID)
    raw = snapshot("neso_tec_register.csv", URL, "text/csv")
    raw.content = b"<html><head><title>302 Found</title></head></html>"
    with pytest.raises(ParseError):
        c.parse(raw)
