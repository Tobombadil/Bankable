"""Parser tests for gb.find_a_tender against a recorded OCDS release package."""

from __future__ import annotations

import json

import pytest

from conftest import connector_for, snapshot
from pipeline.connectors.base import ParseError
from pipeline.connectors.gb_find_a_tender.connector import is_energy, release_cpvs, strip_contacts

SOURCE_ID = "gb.find_a_tender"
URL = "https://www.find-tender.service.gov.uk/api/1.0/ocdsReleasePackages"


@pytest.fixture(scope="module")
def parsed():
    c = connector_for(SOURCE_ID)
    raw = snapshot("find_a_tender_ocds.json", URL, "application/json")
    rows = c.parse(raw)
    return c, raw, rows, c.normalize(rows, raw)


def test_only_energy_cpv_releases_are_kept(parsed):
    _, _, rows, df = parsed
    assert len(rows) == 8  # 11 releases in the fixture, 3 of them non-energy
    assert all(is_energy(r["cpv"]) for r in rows)
    assert len(df) == 8


def test_cpv_codes_are_collected_from_items_and_classification():
    rel = {
        "tender": {
            "classification": {"scheme": "CPV", "id": "09310000"},
            "items": [{"additionalClassifications": [{"scheme": "CPV", "id": "45231400"}]}],
        }
    }
    assert release_cpvs(rel) == ["09310000", "45231400"]
    assert is_energy(["79710000"]) is False


def test_ocid_is_the_record_id(parsed):
    _, _, rows, df = parsed
    assert set(df["source_record_id"]) == {r["ocid"] for r in rows}
    assert not df["record_id"].duplicated().any()


def test_status_and_tags_map_onto_the_opportunity_vocabulary(parsed):
    _, _, _, df = parsed
    assert set(df["status"]) <= {"announced", "open", "closed", "cancelled", "awarded", "unknown"}
    assert df["status_rule"].str.startswith("find_a_tender.").all()
    assert set(df["jurisdiction"]) == {"GB"}


def test_contact_points_are_stripped_before_the_snapshot_is_stored():
    package = {
        "releases": [
            {
                "parties": [{"name": "A Council", "contactPoint": {"name": "Jane", "email": "j@x.gov.uk"}}],
                "buyer": {"name": "A Council", "contactPoint": {"email": "j@x.gov.uk"}},
            }
        ]
    }
    cleaned = strip_contacts(package)
    assert "contactPoint" not in cleaned["releases"][0]["parties"][0]
    assert "contactPoint" not in cleaned["releases"][0]["buyer"]
    assert cleaned["releases"][0]["parties"][0]["name"] == "A Council"


def test_the_recorded_fixture_holds_no_contact_personal_data():
    raw = snapshot("find_a_tender_ocds.json", URL, "application/json")
    assert b"contactPoint" not in raw.content


def test_the_latest_release_per_ocid_wins():
    c = connector_for(SOURCE_ID)
    raw = snapshot("find_a_tender_ocds.json", URL, "application/json")
    doc = json.loads(raw.content)
    rel = doc["pages"][0]["releases"][0]
    older = json.loads(json.dumps(rel))
    older["date"] = "2000-01-01T00:00:00Z"
    older["tender"]["title"] = "stale"
    doc["pages"][0]["releases"].append(older)
    raw.content = json.dumps(doc).encode()
    rows = c.parse(raw)
    row = next(r for r in rows if r["ocid"] == rel["ocid"])
    assert row["tender"]["title"] != "stale"


def test_a_page_without_releases_is_a_parse_error():
    c = connector_for(SOURCE_ID)
    raw = snapshot("find_a_tender_ocds.json", URL, "application/json")
    raw.content = json.dumps({"pages": [{"detail": "error"}]}).encode()
    with pytest.raises(ParseError):
        c.parse(raw)


def test_a_zero_or_negative_value_is_an_unknown_budget(parsed):
    c, raw, rows, _ = parsed
    edited = [{**r, "tender": dict(r.get("tender") or {})} for r in rows]
    edited[0]["tender"]["value"] = {"amount": 0, "currency": "GBP"}
    edited[1]["tender"]["value"] = {"amount": -1, "currency": "GBP"}
    edited[2]["tender"]["value"] = {"amount": 42000, "currency": "GBP"}
    df = c.normalize(edited, raw)
    assert df["budget_amount"].isna()[0] and df["budget_amount"].isna()[1]
    assert df.loc[2, "budget_amount"] == 42000.0
    assert list(df.loc[:1, "budget_currency"]) == ["GBP", "GBP"]


# ---------------------------------------------------------------- unchanged check (review §2.7 #6)
def _with_request_echo(body: bytes, updated_to: str) -> bytes:
    """The fixture as the live API returns it: every page echoes the request, `updatedTo=<now>`
    included, in `uri` and in the `links.next` cursor (the 2026-10-09 snapshots on the operator's
    data root differ in exactly these two keys and nothing else)."""
    doc = json.loads(body)
    query = f"updatedFrom={doc['request']['updatedFrom']}&limit=100&updatedTo={updated_to}"
    for page in doc["pages"]:
        page["uri"] = f"{URL}?{query}"
        page["links"] = {"next": f"{URL}?{query}&cursor=Y3Vyc29yfHt1cGRhdGVkVG99"}
    return json.dumps(doc, ensure_ascii=False).encode("utf-8")


def _fts_run(store, body: bytes, hour: int):
    import datetime as dt

    from pipeline.connectors.runner import run

    raw = snapshot(
        "find_a_tender_ocds.json",
        URL,
        "application/json",
        retrieved_at=dt.datetime(2026, 9, 12, hour, 11, tzinfo=dt.UTC),
    )
    raw.content = body
    return run(SOURCE_ID, store=store, raw=raw)


def test_the_canonical_form_drops_the_request_echo_and_keeps_the_releases():
    c = connector_for(SOURCE_ID)
    body = snapshot("find_a_tender_ocds.json", URL, "application/json").content
    a = _with_request_echo(body, "2026-10-09T20:57:19")
    b = _with_request_echo(body, "2026-10-09T21:11:03")
    assert a != b
    assert c.canonical_content(a) == c.canonical_content(b)
    doc = json.loads(b)
    doc["pages"][0]["releases"][0]["date"] = "2026-09-12T09:00:00+01:00"
    assert c.canonical_content(json.dumps(doc).encode()) != c.canonical_content(a)
    assert c.canonical_content(b"not json") == b"not json"


def test_two_fetches_differing_only_in_the_request_echo_are_unchanged(tmp_path):
    """Each hourly run used to end `ok`, store a 4.7 MB snapshot and queue a load. The second fetch
    is now `unchanged`, stores nothing and names the snapshot it matched; a real change still runs."""
    from pipeline.connectors.store import Store

    store = Store(tmp_path)
    body = snapshot("find_a_tender_ocds.json", URL, "application/json").content
    first = _fts_run(store, _with_request_echo(body, "2026-09-12T10:11:00"), 10)
    assert first.status == "ok", first.run.get("error")
    second = _fts_run(store, _with_request_echo(body, "2026-09-12T11:11:00"), 11)
    assert second.status == "unchanged", second.run.get("error")
    assert "snapshot" not in second.paths and "normalized" not in second.paths
    snap1, snap2 = first.run["snapshot"], second.run["snapshot"]
    assert snap2["canonical_sha256"] == snap1["canonical_sha256"]
    assert snap2["sha256"] == snap1["sha256"] and snap2["fetched_sha256"] != snap1["sha256"]
    assert len(list((tmp_path / "snapshots" / SOURCE_ID).iterdir())) == 1
    # the store still resolves the baseline through the unchanged run's record
    assert store.last_snapshot(SOURCE_ID) is not None

    doc = json.loads(body)
    doc["pages"][0]["releases"][0]["date"] = "2026-09-12T11:30:00+01:00"
    third = _fts_run(store, _with_request_echo(json.dumps(doc).encode(), "2026-09-12T12:11:00"), 12)
    assert third.status == "ok", third.run.get("error")
    assert "fetched_sha256" not in third.run["snapshot"]
