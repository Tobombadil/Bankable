"""`GET /v1/documents/{document_id}` and its bytes route (US-302 AC1; docs/21 §3.8; docs/02 §4).
Nothing writes `document` rows yet, so every case here is a fixture row."""

from __future__ import annotations

import datetime as dt
import hashlib
import pathlib
from typing import Any

import jsonschema
import pytest
import yaml

from services.api.conftest import (
    make_attribution_licence,
    make_open_licence,
    make_org,
    make_public_source,
    make_visible_opportunity,
    make_visible_proposal,
)
from services.db.models import Document
from services.ids import public_id

UTC = dt.UTC
REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
PDF = b"%PDF-1.4 fixture\n"


@pytest.fixture(scope="module")
def spec() -> dict[str, Any]:
    return yaml.safe_load((REPO_ROOT / "api" / "openapi.yaml").read_text())


def assert_valid(spec: dict[str, Any], schema_name: str, instance: object) -> None:
    schema = spec["components"]["schemas"][schema_name]
    resolver = jsonschema.validators.RefResolver.from_schema(spec)
    validator = jsonschema.validators.validator_for(schema)(schema, resolver=resolver)
    errors = sorted(validator.iter_errors(instance), key=str)
    assert not errors, "\n".join(f"{e.message} at {list(e.absolute_path)}" for e in errors)


@pytest.fixture(autouse=True)
def document_dir(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    path = tmp_path / "documents"
    path.mkdir()
    (path / "order.pdf").write_bytes(PDF)
    monkeypatch.setenv("DOCUMENT_DIR", str(path))
    return path


def make_document(db, source, *, subject=None, subject_type: str | None = None, **fields: Any) -> Document:
    values: dict[str, Any] = {
        "public_id": "",
        "subject_type": subject_type or ("none" if subject is None else subject.__tablename__),
        "subject_id": subject.id if subject is not None else None,
        "source_id": source.id,
        "source_url": "https://example.org/docket/123/order.pdf",
        "retrieved_at": dt.datetime(2026, 9, 20, tzinfo=UTC),
        "licence_id": source.licence_id,
        "title": "Order granting interconnection",
        "doc_type": "order",
        "published_date": dt.date(2026, 9, 1),
        "identifiers": {"docket_id": "ER26-123"},
        "storage_policy": "stored",
        "object_key": "order.pdf",
        "content_type": "application/pdf",
        "byte_size": len(PDF),
        "sha256": hashlib.sha256(PDF).hexdigest(),
        "page_count": 1,
    }
    values.update(fields)
    doc = Document(**values)
    db.add(doc)
    db.flush()
    doc.public_id = public_id("doc", doc.id)
    db.commit()
    return doc


def _source(db, *, licence=None, id_: str = "us.test.public_source", publish_state: str = "public"):
    lic = licence or make_open_licence(db)
    src = make_public_source(db, lic, id_=id_)
    src.publish_state = publish_state
    db.flush()
    return src


def test_stored_document_with_a_permissive_licence_links_its_bytes(client, db, spec):
    src = _source(db)
    prop = make_visible_proposal(db, src)
    doc = make_document(db, src, subject=prop)
    resp = client.get(f"/v1/documents/{doc.public_id}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert_valid(spec, "DocumentDetailResponse", body)
    data = body["data"]
    assert data["subject_id"] == prop.public_id
    assert data["provenance"]["source_id"] == src.id
    assert data["provenance"]["licence_id"] == src.licence_id
    assert data["provenance"]["source_url"] == doc.source_url
    assert data["download_url"].endswith(f"/v1/documents/{doc.public_id}/content")
    assert data["download_expires_at"] is None
    assert body["redactions"] == []
    assert body["licence_summary"]["sources"][0]["source_id"] == src.id
    content = client.get(f"/v1/documents/{doc.public_id}/content")
    assert content.status_code == 200
    assert content.content == PDF
    assert content.headers["content-type"] == "application/pdf"
    assert content.headers["x-source-url"] == doc.source_url


def test_link_only_document_links_out_and_serves_no_bytes(client, db, spec):
    src = _source(db)
    doc = make_document(
        db, src, storage_policy="link_only", object_key=None, doc_type="news_article", content_type=None
    )
    body = client.get(f"/v1/documents/{doc.public_id}").json()
    assert_valid(spec, "DocumentDetailResponse", body)
    assert body["data"]["download_url"] is None
    assert body["data"]["subject_id"] is None
    assert body["redactions"] == []
    assert client.get(f"/v1/documents/{doc.public_id}/content").status_code == 404


@pytest.mark.parametrize("reason", ["licence", "personal_data"])
def test_stored_bytes_are_withheld_with_a_redaction(client, db, spec, reason):
    if reason == "licence":
        lic = make_attribution_licence(db, id_="no-raw")
        lic.allows_raw_publication = False
        doc = make_document(db, _source(db, licence=lic))
    else:
        doc = make_document(db, _source(db), personal_data_flag=True)
    body = client.get(f"/v1/documents/{doc.public_id}").json()
    assert_valid(spec, "DocumentDetailResponse", body)
    assert body["data"]["download_url"] is None
    assert [r["reason"] for r in body["redactions"]] == [reason]
    assert body["redactions"][0]["field"] == "download_url"
    assert client.get(f"/v1/documents/{doc.public_id}/content").status_code == 404


def test_stored_policy_without_a_file_has_no_link(client, db):
    doc = make_document(db, _source(db), object_key="missing.pdf")
    assert client.get(f"/v1/documents/{doc.public_id}").json()["data"]["download_url"] is None
    assert client.get(f"/v1/documents/{doc.public_id}/content").status_code == 404


def test_object_key_outside_the_document_dir_is_never_served(client, db, document_dir):
    (document_dir.parent / "secret.txt").write_text("nope")
    doc = make_document(db, _source(db), object_key="../secret.txt")
    assert client.get(f"/v1/documents/{doc.public_id}").json()["data"]["download_url"] is None
    assert client.get(f"/v1/documents/{doc.public_id}/content").status_code == 404


def test_document_visibility_follows_source_licence_and_subject(client, db):
    open_lic = make_open_licence(db)
    src = _source(db, licence=open_lic)
    hidden_src = _source(db, licence=open_lic, id_="us.test.ingest_only", publish_state="ingest_only")
    now = dt.datetime.now(UTC)
    future_prop = make_visible_proposal(db, src, public_id_suffix="7", public_at=now + dt.timedelta(days=2))
    restricted = make_attribution_licence(db, id_="restricted-lic")
    restricted.reuse_class = "restricted"
    restricted_src = _source(db, licence=restricted, id_="us.test.restricted")
    opp = make_visible_opportunity(db, src)
    org = make_org(db)
    cases = {
        "ingest_only source": make_document(db, hidden_src),
        "restricted licence": make_document(db, restricted_src),
        "subject not public yet": make_document(db, src, subject=future_prop),
    }
    for label, doc in cases.items():
        assert client.get(f"/v1/documents/{doc.public_id}").status_code == 404, label
        assert client.get(f"/v1/documents/{doc.public_id}/content").status_code == 404, label
    assert client.get("/v1/documents/doc_0000000000").status_code == 404
    opp_doc = make_document(db, src, subject=opp)
    assert client.get(f"/v1/documents/{opp_doc.public_id}").json()["data"]["subject_id"] == opp.public_id
    org_doc = make_document(db, src, subject=org)
    assert client.get(f"/v1/documents/{org_doc.public_id}").json()["data"]["subject_id"] == org.public_id
