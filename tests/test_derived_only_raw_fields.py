"""A derived-only licence publishes no raw field (docs/21 §8: "no raw, no `status_raw`, no exact
coordinates"; L-4 of the 2026-09-30 legal audit).

CAISO and NYISO are `publication: derived_only` (`licence.allows_raw_publication = false`), yet
`status_raw` and `technology_raw` were served for 3,996 such records on the API, the CSV and the
record page -- directly above the page's own line "Derived fields only; the raw source row is
withheld under licence." One CAISO point name also carried a coordinate the county-centroid rule
withholds. The withholding is the served view's (`services/api/visibility.py::GatedRecord`), so it
holds on every surface at once.
"""

from __future__ import annotations

import csv
import io
import json
import pathlib
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from services.api.conftest import (
    make_attribution_licence,
    make_open_licence,
    make_public_source,
    make_visible_proposal,
)
from services.db.models import InterconnectionPoint, ProposalSource
from services.ids import public_id
from services.ingest.interconnection import COORDINATES_WITHHELD, withhold_coordinates
from tests.conftest import login, make_account, make_api_key, make_user

CAISO_STATUS = "ACTIVE"
CAISO_TECH = "Wind Turbine + Storage"
HERDLYN = "Herdlyn - Tracy 70 kV - Long: -121.577185 Lat: 37.800654"


@pytest.fixture(autouse=True)
def _export_dir(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXPORT_DIR", str(tmp_path / "exports"))


def _derived_only_record(db: Session) -> tuple[str, str, str]:
    lic = make_attribution_licence(db, id_="caiso-terms")
    lic.allows_raw_publication = False
    caiso = make_public_source(db, lic, id_="us.test.caiso")
    prop = make_visible_proposal(db, caiso, public_id_suffix="5")
    prop.status_raw = CAISO_STATUS
    prop.technology_raw = CAISO_TECH
    prop.field_provenance = {
        f: {"source_id": caiso.id, "licence_id": lic.id, "retrieved_at": "2026-09-12"}
        for f in ("status_raw", "technology_raw", "name_canonical")
    }
    point = InterconnectionPoint(
        public_id="",
        operator="CAISO",
        name_display=HERDLYN,
        name_key="line:herdlyn~tracy|70",
        key_rule="test",
        kind="line_tap",
        source_id=caiso.id,
        source_url=caiso.url,
        retrieved_at=prop.first_seen,
        licence_id=lic.id,
    )
    db.add(point)
    db.flush()
    point.public_id = public_id("poi", point.id)
    prop.interconnection_point_id = point.id
    db.commit()
    return prop.public_id, prop.slug, point.public_id


@pytest.fixture()
def site(client: TestClient) -> Iterator[TestClient]:
    from web.api_client import build_client
    from web.app import app as web_app

    previous = web_app.state.__dict__.get("api_client")
    web_app.state.api_client = build_client(api_base_url="")
    try:
        with TestClient(web_app) as w:
            yield w
    finally:
        if previous is None:
            web_app.state.__dict__.pop("api_client", None)
        else:
            web_app.state.api_client = previous


def test_raw_fields_of_a_derived_only_source_are_withheld_with_a_redaction(
    client: TestClient, db: Session
) -> None:
    pid, _slug, _point = _derived_only_record(db)
    detail = client.get(f"/v1/proposals/{pid}").json()
    assert detail["data"]["status_raw"] is None
    assert detail["data"]["technology_raw"] is None
    assert {(r["field"], r["reason"], r["source_id"]) for r in detail["redactions"]} == {
        ("status_raw", "licence", "us.test.caiso"),
        ("technology_raw", "licence", "us.test.caiso"),
    }
    listed = client.get("/v1/proposals").json()["data"]
    assert [(r["status_raw"], r["technology_raw"]) for r in listed] == [(None, None)]


def test_raw_fields_of_a_derived_only_source_are_absent_from_the_csv_bulk_and_page(
    client: TestClient, db: Session, site: TestClient
) -> None:
    pid, slug, _point = _derived_only_record(db)
    account = make_account(db, entitlement="api")
    user = make_user(db, account)
    _key, secret = make_api_key(db, account, user, scopes=["read:live", "read:bulk"])
    db.commit()

    bulk = client.get("/v1/bulk/proposals", headers={"Authorization": f"Bearer {secret}"})
    lines = [json.loads(line) for line in bulk.iter_lines() if line]
    assert (lines[1]["status_raw"], lines[1]["technology_raw"]) == (None, None)
    assert {r["field"] for r in lines[0]["redactions"]} >= {"status_raw", "technology_raw"}

    login(client, db, user)
    created = client.post("/v1/exports", json={"entity": "proposal", "query": {}})
    text = client.get(f"/v1/exports/{created.json()['data']['export_id']}/download").text
    assert CAISO_STATUS not in text and CAISO_TECH not in text
    body = "\n".join(line for line in text.splitlines() if not line.startswith("#"))
    row = next(csv.DictReader(io.StringIO(body)))
    assert (row["status_raw"], row["technology_raw"]) == ("", "")

    page = site.get(f"/proposals/{slug}").text
    assert CAISO_STATUS not in page and CAISO_TECH not in page
    assert pid  # the page is the record's own


def test_a_raw_ok_source_still_publishes_its_raw_fields(client: TestClient, db: Session) -> None:
    """The control: an open register's raw status is served as before."""
    src = make_public_source(db, make_open_licence(db), id_="us.test.ercot")
    prop = make_visible_proposal(db, src, public_id_suffix="6")
    prop.status_raw = "Completed"
    db.commit()
    data = client.get(f"/v1/proposals/{prop.public_id}").json()
    assert data["data"]["status_raw"] == "Completed"
    assert data["redactions"] == []


def test_a_merged_records_raw_field_follows_the_licence_of_the_source_that_supplies_it(
    client: TestClient, db: Session
) -> None:
    """A CAISO record also seen in an open register: `status_raw` stored from the open register is
    served; from CAISO, it is not."""
    pid, _slug, _point = _derived_only_record(db)
    open_src = make_public_source(db, make_open_licence(db), id_="us.test.open")
    from services.db.models import Proposal

    prop = db.query(Proposal).filter_by(public_id=pid).one()
    db.add(
        ProposalSource(
            proposal_id=prop.id,
            source_id=open_src.id,
            source_record_id="O-1",
            source_url="https://example.org/open/1",
            retrieved_at=prop.first_seen,
            licence_id=open_src.licence_id,
            normalised={"status_raw": "Operating"},
            first_seen=prop.first_seen,
            last_seen=prop.first_seen,
        )
    )
    prop.status_raw = "Operating"
    prop.field_provenance = {
        **prop.field_provenance,
        "status_raw": {
            "source_id": open_src.id,
            "licence_id": open_src.licence_id,
            "retrieved_at": "2026-09-13",
        },
    }
    db.commit()
    data = client.get(f"/v1/proposals/{pid}").json()["data"]
    assert data["status_raw"] == "Operating"
    assert data["technology_raw"] is None


def test_a_coordinate_typed_into_a_derived_only_point_name_is_withheld(
    client: TestClient, db: Session
) -> None:
    pid, _slug, point = _derived_only_record(db)
    detail = client.get(f"/v1/interconnection-points/{point}").json()["data"]
    assert detail["name"] == f"Herdlyn - Tracy 70 kV - {COORDINATES_WITHHELD}"
    embed = client.get(f"/v1/proposals/{pid}").json()["data"]["interconnection_point"]
    assert "121.577185" not in json.dumps(embed) and "37.800654" not in json.dumps(embed)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (HERDLYN, f"Herdlyn - Tracy 70 kV - {COORDINATES_WITHHELD}"),
        ("pole located at 42.272362°, -76.731007°", f"pole located at {COORDINATES_WITHHELD}"),
        ("Locational coordinates : 33.34414; 99.624637", f"Locational coordinates : {COORDINATES_WITHHELD}"),
        # Voltages, mileages and parcel numbers are not coordinates.
        ("1.78mi from Cayuta 34.5kV substation", "1.78mi from Cayuta 34.5kV substation"),
        ("Parcel ID 151.00-01-35.100", "Parcel ID 151.00-01-35.100"),
        ("The 13.2 kV 403 (1106340) line", "The 13.2 kV 403 (1106340) line"),
    ],
)
def test_withhold_coordinates_takes_coordinates_only(text: str, expected: str) -> None:
    assert withhold_coordinates(text) == expected
