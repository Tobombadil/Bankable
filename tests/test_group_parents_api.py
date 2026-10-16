"""A parent's page lists its subsidiaries' proposals once the curated group file has loaded
(`services/ingest/organizations.py::load_group_parents`, docs/22 §24): the loader, then the two reads
the company page makes at its default scope, `GET /v1/organizations/{id}/pipeline?scope=all` and
`.../proposals?scope=all`, against one store. The measured case: `/organizations/nextera-energy-inc`
read "No proposals sponsored by this company" on 2026-10-11 because no NextEra record had a parent.
"""

from __future__ import annotations

import pathlib

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from services.api.conftest import make_open_licence, make_org, make_public_source, make_visible_proposal
from services.ingest.organizations import load_group_parents

EX21 = "https://www.sec.gov/Archives/edgar/data/37634/000075330826000015/nee-q42025xex21.htm"
GROUPS = f"""
groups:
  - parent: NextEra Energy, Inc.
    parent_country: US
    parent_names: [NextEra]
    children:
      - name: NextEra Energy Resources - ERCOT
        evidence: sec_ex21
        source_url: {EX21}
        retrieved_at: '2026-10-15T22:09:03Z'
      - name: Florida Power & Light Co
        evidence: sec_ex21
        source_url: {EX21}
        retrieved_at: '2026-10-15T22:09:03Z'
        quote: 1. Florida Power & Light Company (100%-owned) Florida
"""


def test_a_parent_page_lists_its_subsidiaries_proposals(
    client: TestClient, db: Session, tmp_path: pathlib.Path
) -> None:
    source = make_public_source(db, make_open_licence(db))
    parent = make_org(db, "NEXTERA ENERGY INC")
    ercot = make_org(db, "NextEra Energy Resources - ERCOT")
    fpl = make_org(db, "Florida Power & Light Co")
    spv = make_org(db, "Flat Hill Solar LLC")  # no parent name, not in the file: stays out
    make_visible_proposal(db, source, public_id_suffix="1", sponsor=ercot, lifecycle_state="studied")
    make_visible_proposal(db, source, public_id_suffix="2", sponsor=fpl, lifecycle_state="filed")
    make_visible_proposal(db, source, public_id_suffix="3", sponsor=spv, lifecycle_state="filed")
    db.commit()

    before = client.get(f"/v1/organizations/{parent.public_id}/pipeline", params={"scope": "all"})
    assert before.status_code == 200 and before.json()["data"]["totals"]["records"] == 0

    path = tmp_path / "developer_parents.yaml"
    path.write_text(GROUPS, encoding="utf-8")
    assert load_group_parents(db, path).children_linked == 2

    pipeline = client.get(f"/v1/organizations/{parent.public_id}/pipeline", params={"scope": "all"})
    data = pipeline.json()["data"]
    assert data["scope"]["organizations"] == 3
    assert data["totals"]["records"] == 2
    assert data["totals"]["capacity_mw"] == 101.0 + 102.0
    assert data["list_query"] == {"sponsor_id": parent.public_id, "sponsor_scope": "all"}

    rows = client.get(
        f"/v1/organizations/{parent.public_id}/proposals", params={"scope": "all", "limit": 100}
    ).json()["data"]
    # Each row names the subsidiary that filed it: what the page prints beside the record.
    assert sorted(row["sponsor"]["name_canonical"] for row in rows) == [
        "Florida Power & Light Co",
        "NextEra Energy Resources - ERCOT",
    ]
    # The scope is the parent's own choice: `self` still reads the parent alone.
    alone = client.get(f"/v1/organizations/{parent.public_id}/pipeline").json()["data"]
    assert alone["totals"]["records"] == 0
