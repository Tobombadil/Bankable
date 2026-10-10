"""Parser tests for us.grants_gov.search2 against a recorded Search2 response."""

from __future__ import annotations

import json

import pytest

from conftest import connector_for, snapshot
from pipeline.connectors.base import ParseError

SOURCE_ID = "us.grants_gov.search2"
URL = "https://api.grants.gov/v1/api/search2"


@pytest.fixture(scope="module")
def parsed():
    c = connector_for(SOURCE_ID)
    raw = snapshot("grants_gov_search2.json", URL, "application/json")
    rows = c.parse(raw)
    return c, raw, rows, c.normalize(rows, raw)


def test_opportunity_fields_are_the_docs21_ones(parsed):
    c, _, _, df = parsed
    assert c.kind == "opportunity"
    for col in ("kind", "issuer", "title", "jurisdiction", "technologies", "open_at", "due_at", "status"):
        assert col in df.columns
    assert "capacity_mw" not in df.columns  # proposal fields do not leak in
    assert set(df["kind"]) == {"foa"}
    assert set(df["jurisdiction"]) == {"US"}


def test_status_vocabulary_is_the_opportunity_one(parsed):
    _, _, _, df = parsed
    assert set(df["status"]) <= {"announced", "open", "closed", "cancelled", "awarded", "unknown"}
    assert df["status_rule"].str.startswith("grants_gov.").all()


def test_a_posted_notice_past_its_deadline_is_closed(parsed):
    _, _, _, df = parsed
    posted = df[df["status_raw"] == "posted"]
    passed = posted[posted["due_at"] < posted["retrieved_at"].iloc[0]]
    assert (passed["status"] == "closed").all()
    assert set(passed["status_rule"]) <= {"grants_gov.posted_deadline_passed"}


def test_identifiers_carry_the_opportunity_number(parsed):
    _, _, rows, df = parsed
    ids = json.loads(df["identifiers"].iloc[0])
    assert ids["grants_gov_number"] == rows[0]["number"]
    assert df["source_record_id"].iloc[0] == str(rows[0]["id"])


def test_html_entities_in_titles_are_unescaped(parsed):
    _, _, _, df = parsed
    assert not df["title"].str.contains("&ndash;|&amp;", regex=True, na=False).any()


def test_technologies_are_tokens_not_prose(parsed):
    _, _, _, df = parsed
    tokens = {t for v in df["technologies"] for t in str(v).split("|") if t}
    assert tokens <= {
        "solar_pv",
        "wind",
        "wind_offshore",
        "bess",
        "hydro",
        "nuclear",
        "hydrogen",
        "geothermal",
        "biomass",
        "ccs",
        "gas",
        "transmission",
        "heat",
        "ev_charging",
        "efficiency",
        "microgrid",
        "metering",
        "pumped_storage",
    }


def test_an_error_body_with_http_200_fails_closed():
    """docs/04 E-6: the source's "HTTP 200 with an error body" failure mode."""
    c = connector_for(SOURCE_ID)
    raw = snapshot("grants_gov_search2_error.json", URL, "application/json")
    with pytest.raises(ParseError):
        c.parse(raw)


# ---------------------------------------------------------------- response token (review §2.7 #6)
def _with_tokens(body: bytes, tag: str) -> bytes:
    """The fixture as the live API returns it: a fresh JWT on every page (the recorded fixture has
    it removed; the 2026-10-09 snapshots on the operator's data root differ only in it)."""
    doc = json.loads(body)
    for i, page in enumerate(doc["pages"]):
        page["token"] = f"eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.{tag}{i}.c2lnbmF0dXJl"
    return json.dumps(doc, ensure_ascii=False).encode("utf-8")


def _gg_run(store, body: bytes, hour: int):
    import datetime as dt

    from pipeline.connectors.runner import run

    raw = snapshot(
        "grants_gov_search2.json",
        URL,
        "application/json",
        retrieved_at=dt.datetime(2026, 9, 12, hour, tzinfo=dt.UTC),
    )
    raw.content = body
    return run(SOURCE_ID, store=store, raw=raw)


def _fixture() -> bytes:
    return snapshot("grants_gov_search2.json", URL, "application/json").content


def test_the_response_token_is_not_stored(tmp_path):
    from pipeline.connectors.store import Store

    result = _gg_run(Store(tmp_path), _with_tokens(_fixture(), "A"), 10)
    assert result.status == "ok", result.run.get("error")
    stored = result.paths["snapshot"].read_bytes()
    assert b"token" not in stored and b"eyJ0eXAi" not in stored
    assert result.run["snapshot"]["redacted"] is True
    # a payload without a token is stored byte for byte
    c = connector_for(SOURCE_ID)
    assert c.redact(_fixture()) == _fixture()


def test_two_fetches_differing_only_in_the_token_are_unchanged(tmp_path):
    from pipeline.connectors.store import Store

    store = Store(tmp_path)
    assert _gg_run(store, _with_tokens(_fixture(), "A"), 10).status == "ok"
    second = _gg_run(store, _with_tokens(_fixture(), "B"), 11)
    assert second.status == "unchanged", second.run.get("error")
    assert len(list((tmp_path / "snapshots" / SOURCE_ID).iterdir())) == 1


def test_a_snapshot_stored_with_its_token_before_the_change_compares_equal(tmp_path, monkeypatch):
    """The first run after the change compares against a snapshot that still holds a token and a
    record with no `canonical_sha256`: it reads the stored bytes once and is `unchanged`, and its
    record names that stored object, so reparse and the conditional-request baseline still find it."""
    from pipeline.connectors import runner
    from pipeline.connectors.store import Store
    from pipeline.connectors.us_grants_gov_search2 import connector as gg

    store = Store(tmp_path)
    monkeypatch.setattr(gg.Connector, "redact", lambda self, content: content)
    first = _gg_run(store, _with_tokens(_fixture(), "A"), 10)
    monkeypatch.undo()
    assert first.status == "ok" and b"eyJ0eXAi" in first.paths["snapshot"].read_bytes()
    record_path = first.paths["run"]
    legacy = json.loads(record_path.read_text())
    del legacy["snapshot"]["canonical_sha256"]
    record_path.write_text(json.dumps(legacy))
    store = Store(tmp_path)  # drop the cached record

    second = _gg_run(store, _with_tokens(_fixture(), "B"), 11)
    assert second.status == "unchanged", second.run.get("error")
    assert second.run["snapshot"]["sha256"] == first.run["snapshot"]["sha256"]
    assert store.last_snapshot(SOURCE_ID) is not None
    assert runner.reparse_skip_reason(SOURCE_ID, store=store) == "up_to_date"


def test_a_held_run_fetched_again_with_a_new_token_is_rechecked_from_the_stored_object(tmp_path):
    from pipeline.connectors.store import Store

    store = Store(tmp_path)
    assert _gg_run(store, _with_tokens(_fixture(), "A"), 10).status == "ok"
    doc = json.loads(_fixture())
    doc["pages"][0]["data"]["oppHits"] = doc["pages"][0]["data"]["oppHits"][:1]  # 12 hits -> 1: held
    shrunk = json.dumps(doc).encode()
    held = _gg_run(store, _with_tokens(shrunk, "B"), 11)
    assert held.status == "partial", held.run.get("dq_status")
    again = _gg_run(store, _with_tokens(shrunk, "C"), 12)
    assert again.status == "partial"
    assert again.run["rechecked_hold"]["run_id"] == held.run["id"]
    assert again.run["snapshot"]["object_key"] == held.run["snapshot"]["object_key"]
    assert again.run["snapshot"]["sha256"] == held.run["snapshot"]["sha256"]
    assert "snapshot" not in again.paths
