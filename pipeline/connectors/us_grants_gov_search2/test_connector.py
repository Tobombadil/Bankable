"""Parser tests for us.grants_gov.search2 against a recorded Search2 response."""

from __future__ import annotations

import json
from collections.abc import Callable

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


# ------------------------------------------------- funding figures (lane rfp-value, 2026-10-22)
DETAILS = "grants_gov_fetch_opportunity.json"
#: The contact fields every recorded response carries (their values replaced in the fixture).
CONTACT_KEYS = {
    "agencyContactName",
    "agencyContactEmail",
    "agencyContactEmailDesc",
    "agencyContactPhone",
    "agencyContactDesc",
    "agencyName",
    "agencyPhone",
    "agencyAddressDesc",
    "contactInfo",
}
#: Fields that name or reach a person wherever they sit in a response: the contact fields (but
#: `agencyName`, which inside `agencyDetails` is the agency's own name), and the user id of the
#: Grants.gov staff member who posted the notice (`data.publisherUid`, derived from their name).
PERSONAL_ANYWHERE = {*CONTACT_KEYS - {"agencyName"}, "publisherUid"}
READ_AT = "2026-10-22T01:00:00Z"
IDS = ["363396", "356623", "279638", "361526"]


def test_the_recorded_details_carry_no_personal_value():
    """Review of lane rfp-value (2026-10-22), item 3: the fixture still held four `publisherUid`
    values, user ids of Grants.gov staff derived from their names. Every personal field in the
    committed file is the placeholder, wherever it sits, and so is every contact field of a
    synopsis or forecast block."""
    from conftest import fixture_path

    found: list[tuple[str, object]] = []

    def walk(node: object) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key in PERSONAL_ANYWHERE:
                    found.append((key, value))
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    doc = json.loads(fixture_path(DETAILS).read_text(encoding="utf-8"))
    walk(doc)
    for response in doc.values():
        for name in ("synopsis", "forecast"):
            block = response["data"].get(name) or {}
            found.extend((key, block[key]) for key in CONTACT_KEYS & set(block))
    assert {key for key, _ in found} >= {"publisherUid", "agencyContactName", "agencyName"}
    assert {value for _, value in found} == {"[removed from fixture]"}


def _responses() -> dict[str, dict]:
    from conftest import fixture_path

    loaded: dict[str, dict] = json.loads(fixture_path(DETAILS).read_text(encoding="utf-8"))
    return loaded


def _hit(hid: str, **over: str) -> dict:
    doc = json.loads(_fixture())
    for page in doc["pages"]:
        for h in page["data"]["oppHits"]:
            if h["id"] == hid:
                return {**h, **over}
    return {"id": hid, "oppStatus": "forecasted", "openDate": "", "closeDate": "", **over}


def _bundle_with_details(ids: list[str]) -> bytes:
    from pipeline.connectors.us_grants_gov_search2.connector import detail_subset

    doc = json.loads(_fixture())
    responses = _responses()
    doc["details"] = {i: detail_subset(responses[i], READ_AT, _hit(i)) for i in ids}
    return json.dumps(doc).encode()


def test_only_the_funding_figures_and_ids_are_kept_from_a_detail():
    from pipeline.connectors.us_grants_gov_search2.connector import (
        DETAIL_BLOCK_KEYS,
        DETAIL_TOP_KEYS,
        detail_subset,
    )

    # Every recorded response carries the contact fields, so dropping them is what is tested.
    blocks = [r["data"].get("synopsis") or r["data"].get("forecast") for r in _responses().values()]
    assert all(CONTACT_KEYS & set(b) for b in blocks)
    for hid, response in _responses().items():
        kept = detail_subset(response, READ_AT, _hit(hid))
        assert kept is not None
        assert set(kept) <= {*DETAIL_TOP_KEYS, "synopsis", "forecast", "retrieved_at", "for_hit"}
        for name in ("synopsis", "forecast"):
            assert set(kept.get(name) or {}) <= set(DETAIL_BLOCK_KEYS)
        text = json.dumps(kept)
        assert "removed from fixture" not in text and "@" not in text
        assert kept["retrieved_at"] == READ_AT and kept["id"] == int(hid)
    error = {"errorcode": 1, "msg": "no such opportunity", "data": {}}
    assert detail_subset(error, READ_AT, _hit("1")) is None


@pytest.mark.parametrize(
    ("hid", "primary", "also", "expected_awards"),
    [
        # total programme funding, ceiling and floor, and an expected number of awards
        (
            "363396",
            ("total_programme_funding", 410_000_000.0, "synopsis.estimatedFunding"),
            [("award_ceiling", 100_000_000.0), ("award_floor", 1_000_000.0)],
            100,
        ),
        (
            "279638",
            ("total_programme_funding", 35_400_000.0, "synopsis.estimatedFunding"),
            [("award_ceiling", 500_000.0), ("award_floor", 1_500.0)],
            1000,
        ),
        # no total: the ceiling leads, the floor beside it
        (
            "356623",
            ("award_ceiling", 20_000_000.0, "synopsis.awardCeiling"),
            [("award_floor", 5_000_000.0)],
            None,
        ),
        # a total without a number of awards
        (
            "361526",
            ("total_programme_funding", 293_760_000.0, "synopsis.estimatedFunding"),
            [("award_ceiling", 16_000_000.0), ("award_floor", 500_000.0)],
            None,
        ),
        # a forecast: its own block, a ceiling only
        ("322667", ("award_ceiling", 20_000.0, "forecast.awardCeiling"), [], None),
    ],
)
def test_the_stated_value_says_what_each_figure_is(hid, primary, also, expected_awards):
    from pipeline.connectors.us_grants_gov_search2.connector import (
        DETAIL_API_URL,
        detail_subset,
        stated_value,
    )

    detail = detail_subset(_responses()[hid], READ_AT, _hit(hid))
    value = stated_value({"id": hid, "detail": detail})
    assert value is not None
    assert (value.primary.kind, value.primary.amount, value.primary.source_field) == primary
    assert value.currency == "USD"
    assert [(f.kind, f.amount) for f in value.also_stated] == also
    assert value.expected_awards == expected_awards
    assert value.retrieved_at == READ_AT and value.endpoint == DETAIL_API_URL


def test_a_floor_alone_or_zeros_state_no_value():
    from pipeline.connectors.us_grants_gov_search2.connector import stated_value

    assert stated_value({"id": "1"}) is None  # no detail read yet
    zeros = {
        "docType": "synopsis",
        "synopsis": {"estimatedFunding": "0", "awardCeiling": "0", "awardFloor": "0"},
    }
    assert stated_value({"id": "1", "detail": zeros}) is None
    floor_only = {"docType": "synopsis", "synopsis": {"awardFloor": "5000"}}
    assert stated_value({"id": "1", "detail": floor_only}) is None  # the least one award can be is not a size


def test_a_placeholder_total_yields_to_the_real_ceiling_beside_it():
    """Review of lane rfp-value (2026-10-22): Grants.gov 363912 states a total of 2,147,483,647 (a
    32-bit field maximum) beside a real award ceiling of 250,000,000; it is ranked on the ceiling,
    labelled as one, with the placeholder listed. A notice whose only figure is a placeholder keeps
    it, shown as stated, and the floor never leads."""
    from pipeline.connectors.us_grants_gov_search2.connector import stated_value

    funding = {"estimatedFunding": "2147483647", "awardCeiling": "250000000", "awardFloor": "500000"}
    value = stated_value({"id": "363912", "detail": {"docType": "synopsis", "synopsis": funding}})
    assert value is not None and (value.primary.kind, value.amount) == ("award_ceiling", 250_000_000.0)
    assert [(f.kind, f.amount) for f in value.also_stated] == [
        ("total_programme_funding", 2_147_483_647.0),
        ("award_floor", 500_000.0),
    ]
    nines = {"docType": "synopsis", "synopsis": {"awardCeiling": "999999999", "awardFloor": "0"}}
    value = stated_value({"id": "361080", "detail": nines})
    assert value is not None and (value.primary.kind, value.amount, value.also_stated) == (
        "award_ceiling",
        999_999_999.0,
        (),
    )
    floor = {"docType": "synopsis", "synopsis": {"estimatedFunding": "2147483647", "awardFloor": "5000"}}
    value = stated_value({"id": "1", "detail": floor})
    assert value is not None and value.primary.kind == "total_programme_funding"


def test_normalize_carries_the_value_and_its_provenance():
    c = connector_for(SOURCE_ID)
    raw = snapshot("grants_gov_search2.json", URL, "application/json")
    raw.content = _bundle_with_details(IDS[:3])
    df = c.normalize(c.parse(raw), raw).set_index("source_record_id")
    assert df.loc["363396", "budget_amount"] == 410_000_000.0
    assert df.loc["363396", "budget_currency"] == "USD"
    assert df.loc["356623", "budget_amount"] == 20_000_000.0
    value = json.loads(df.loc["363396", "identifiers"])["value"]
    assert value == {
        "kind": "total_programme_funding",
        "source_field": "synopsis.estimatedFunding",
        "also_stated": [
            {
                "kind": "award_ceiling",
                "amount": 100_000_000.0,
                "currency": "USD",
                "source_field": "synopsis.awardCeiling",
            },
            {
                "kind": "award_floor",
                "amount": 1_000_000.0,
                "currency": "USD",
                "source_field": "synopsis.awardFloor",
            },
        ],
        "expected_awards": 100,
        "retrieved_at": READ_AT,
        "endpoint": "https://api.grants.gov/v1/api/fetchOpportunity",
    }
    # A notice whose detail was not read states no amount; nothing is inferred for it.
    assert int(df["budget_amount"].isna().sum()) == len(df) - 3
    assert "value" not in json.loads(df.loc["348258", "identifiers"])
    # The stored raw carries the figures and when they were read, never a contact field.
    raw_row = json.loads(df.loc["363396", "raw"])
    assert raw_row["detail"]["synopsis"]["estimatedFunding"] == "410000000"
    assert raw_row["detail"]["retrieved_at"] == READ_AT
    assert not CONTACT_KEYS & set(raw_row["detail"]["synopsis"])


class _Resp:
    def __init__(self, body: dict, status: int = 200) -> None:
        self._body, self.status_code = body, status

    def json(self) -> dict:
        return self._body


class _Http:
    """Answers fetchOpportunity from the recorded responses and records each id asked for."""

    def __init__(
        self,
        fail: frozenset[str] = frozenset(),
        blocked: bool = False,
        status: dict[str, int] | None = None,
        on_call: Callable[[], None] | None = None,
    ) -> None:
        self.responses = _responses()
        self.calls: list[str] = []
        self.fail, self.blocked = fail, blocked
        self.status = status or {}
        self.on_call = on_call

    def post(self, url: str, **kwargs: object) -> _Resp:
        from pipeline.connectors.http import HttpBlocked, HttpFailed
        from pipeline.connectors.us_grants_gov_search2.connector import DETAIL_API_URL

        assert url == DETAIL_API_URL
        body = kwargs["json"]
        assert isinstance(body, dict)
        hid = str(body["opportunityId"])
        self.calls.append(hid)
        if self.on_call is not None:
            self.on_call()
        if self.blocked:
            raise HttpBlocked("challenge page")
        if hid in self.fail:
            raise HttpFailed("HTTP 503 after 1 attempts")
        if hid in self.status:
            return _Resp({}, self.status[hid])
        return _Resp(self.responses.get(hid, {"errorcode": 1, "msg": "not found", "data": {}}))


class _Previous:
    """The previous snapshot as the runner hands it over: its bytes, and its run record's meta
    (`details` is the previous `fetch_details` counts, `checked` included)."""

    def __init__(self, details: dict, stats: dict | None = None) -> None:
        self._body = json.dumps({"pages": [], "details": details}).encode()
        self.meta = {"details": stats} if stats else {}

    def content(self) -> bytes:
        return self._body


def _connector(
    http: _Http,
    now: str = "2026-10-22T01:00:00+00:00",
    previous: dict | None = None,
    stats: dict | None = None,
):
    import datetime as dt

    c = connector_for(SOURCE_ID)
    c.http = http
    c.clock = lambda: dt.datetime.fromisoformat(now)
    c.previous = _Previous(previous, stats) if previous is not None else None
    return c


def test_details_are_read_once_and_reused_while_fresh_and_unchanged():
    http = _Http()
    first, stats = _connector(http).fetch_details([_hit(i) for i in IDS])
    assert http.calls == IDS and stats["fetched"] == 4 and stats["requested"] == 4
    assert list(first) == IDS
    # An hour later, same hits: nothing is asked for.
    again = _Http()
    second, stats = _connector(again, "2026-10-22T02:00:00+00:00", first).fetch_details(
        [_hit(i) for i in IDS]
    )
    assert again.calls == [] and stats["reused"] == 4 and second == first
    # Eight days later every reading is stale.
    week = _Http()
    _connector(week, "2026-10-30T02:00:00+00:00", first).fetch_details([_hit(i) for i in IDS])
    assert sorted(week.calls) == sorted(IDS)
    # A changed close date is a revised notice: read again the same day. Its figures are the same,
    # so the reading stands; only the hit it was read for follows the notice.
    moved = _Http()
    hits = [_hit(i) for i in IDS[:3]] + [_hit(IDS[3], closeDate="12/31/2026")]
    revised, stats = _connector(moved, "2026-10-22T02:00:00+00:00", first).fetch_details(hits)
    assert moved.calls == [IDS[3]] and stats["reused"] == 3 and stats["unchanged"] == 1
    assert revised[IDS[3]]["retrieved_at"] == first[IDS[3]]["retrieved_at"]
    assert revised[IDS[3]]["for_hit"]["closeDate"] == "12/31/2026"


def test_a_re_read_with_the_same_figures_keeps_the_reading_and_records_only_the_check():
    """Review of lane rfp-value (2026-10-22): the weekly re-read gave every detail a new
    `retrieved_at`, which reached the served `identifiers.value` and moved `last_changed` on all 105
    value-bearing notices of the store, with no figure changed. Now the reading stands, and the
    time of the check goes to the snapshot's meta, which the next run reads back."""
    hits = [_hit(i) for i in IDS]
    first, _ = _connector(_Http()).fetch_details(hits)
    week = _Http()
    again, stats = _connector(week, "2026-10-30T02:00:00+00:00", first).fetch_details(hits)
    assert sorted(week.calls) == sorted(IDS) and stats["fetched"] == 4 and stats["unchanged"] == 4
    assert again == first  # `retrieved_at` included: the bundle, and so the record, is unchanged
    assert stats["checked"] == {"2026-10-30T02:00:00Z": [279638, 356623, 361526, 363396]}
    # An hour later the check counts as a read: nothing is asked for, and the check is carried on.
    later = _Http()
    kept, carried = _connector(later, "2026-10-30T03:00:00+00:00", again, stats).fetch_details(hits)
    assert later.calls == [] and carried["reused"] == 4 and kept == first
    assert carried["checked"] == stats["checked"]
    # Without the check (a record written before it existed) the readings are stale again.
    stale = _Http()
    _connector(stale, "2026-10-30T03:00:00+00:00", again).fetch_details(hits)
    assert sorted(stale.calls) == sorted(IDS)
    # A changed figure is a new reading, with its own time; the others stand.
    changed = _Http()
    changed.responses["363396"]["data"]["synopsis"]["estimatedFunding"] = "420000000"
    new, stats = _connector(changed, "2026-11-08T02:00:00+00:00", kept, carried).fetch_details(hits)
    assert new["363396"]["retrieved_at"] == "2026-11-08T02:00:00Z"
    assert new["363396"]["synopsis"]["estimatedFunding"] == "420000000"
    assert new["279638"] == first["279638"] and stats["unchanged"] == 3
    assert stats["checked"] == {"2026-11-08T02:00:00Z": [279638, 356623, 361526]}


def test_an_unreadable_previous_snapshot_reads_the_details_afresh():
    """An object-store failure reading the previous snapshot never fails the run (review of lane
    rfp-value: `StoreReadError` was not caught)."""
    from pipeline.connectors.objectstore import StoreReadError

    class _Unreadable:
        def __init__(self) -> None:
            self.meta: dict = {}

        def content(self) -> bytes:
            raise StoreReadError("read of s3://bucket/snapshots/us.grants_gov.search2/x.json failed")

    c = _connector(_Http())
    c.previous = _Unreadable()
    details, stats = c.fetch_details([_hit(i) for i in IDS])
    assert stats["fetched"] == 4 and list(details) == IDS


def test_a_failed_read_stops_the_reads_keeps_the_previous_figures_and_never_fails_the_run():
    """Review of lane rfp-value (2026-10-22): a failed read was followed by the next one,
    each through the session's retries, so an outage of the detail endpoint outlasted the
    scheduler's timeout and cost the search its run. Now the first failure stops the reads; what
    was not read keeps its previous reading and waits for the next run."""
    first, _ = _connector(_Http()).fetch_details([_hit(i) for i in IDS])
    flaky = _Http(fail=frozenset({"356623"}))
    details, stats = _connector(flaky, "2026-10-30T02:00:00+00:00", first).fetch_details(
        [_hit(i) for i in IDS]
    )
    assert flaky.calls == IDS[:2]  # 363396 read, 356623 failed, the other two not asked for
    assert (stats["fetched"], stats["failed"], stats["deferred"]) == (1, 1, 2)
    assert str(stats["stopped"]).startswith("HttpFailed") and stats["blocked"] is False
    assert details == first  # every earlier reading kept, each with its own read time
    unknown, stats = _connector(_Http()).fetch_details([_hit("999999")])
    assert unknown == {} and stats["failed"] == 1  # an error body is not stored ...
    assert stats["stopped"] is None  # ... and one does not stop the reads


def test_a_notice_whose_read_failed_waits_a_day_before_it_is_asked_again():
    """Review of lane rfp-value (2026-10-22): a failed read stored nothing, so the notice counted as
    new and was asked for first on every hourly run. Its failure time now goes to the snapshot's
    meta, and it is not asked again for `DETAIL_RETRY_AFTER`."""
    import datetime as dt

    from pipeline.connectors.us_grants_gov_search2.connector import DETAIL_RETRY_AFTER

    hits = [_hit("999999"), _hit(IDS[0])]
    first = _Http()
    details, stats = _connector(first).fetch_details(hits)
    assert first.calls == ["999999", IDS[0]] and stats["failed"] == 1
    assert stats["failed_at"] == {"2026-10-22T01:00:00Z": [999999]} and list(details) == [IDS[0]]
    # An hour later neither is asked for: one is fresh, the other waits; the failure is carried on.
    hour = _Http()
    kept, waiting = _connector(hour, "2026-10-22T02:00:00+00:00", details, stats).fetch_details(hits)
    assert hour.calls == [] and (waiting["reused"], waiting["waiting"]) == (1, 1)
    assert waiting["failed_at"] == stats["failed_at"] and kept == details
    # A day after the failure it is asked again, and a second failure starts a new wait.
    assert DETAIL_RETRY_AFTER == dt.timedelta(days=1)
    day = _Http()
    _, again = _connector(day, "2026-10-23T01:00:00+00:00", kept, waiting).fetch_details(hits)
    assert day.calls == ["999999"] and again["failed_at"] == {"2026-10-23T01:00:00Z": [999999]}
    # An outage stops the reads at the first failure: only that notice waits, the rest are read
    # (newest first) on the next runs, one request an hour while the endpoint is down.
    down = _Http(fail=frozenset(IDS))
    _, out = _connector(down).fetch_details([_hit(i) for i in IDS])
    assert down.calls == IDS[:1] and out["failed_at"] == {"2026-10-22T01:00:00Z": [int(IDS[0])]}
    still = _Http(fail=frozenset(IDS))
    _, out = _connector(still, "2026-10-22T02:00:00+00:00", {}, out).fetch_details([_hit(i) for i in IDS])
    assert still.calls == IDS[1:2] and out["waiting"] == 1
    assert out["failed_at"] == {
        "2026-10-22T01:00:00Z": [int(IDS[0])],
        "2026-10-22T02:00:00Z": [int(IDS[1])],
    }


def test_a_refusal_or_a_run_of_error_bodies_stops_the_reads():
    refused = _Http(status={"356623": 403})
    _, stats = _connector(refused).fetch_details([_hit(i) for i in IDS])
    assert refused.calls == IDS[:2] and stats["stopped"] == "HTTP 403" and stats["deferred"] == 2
    # Error bodies (HTTP 200, errorcode != 0) for ids the API does not know: ten in a row stop it.
    unknown = [str(900000 + n) for n in range(12)]
    errors = _Http()
    details, stats = _connector(errors).fetch_details([_hit(i) for i in unknown])
    assert len(errors.calls) == 10 and stats["failed"] == 10 and stats["deferred"] == 2
    assert stats["stopped"] == "10 error bodies in a row" and details == {}
    # A good answer between them resets the count.
    mixed = _Http()
    _, stats = _connector(mixed).fetch_details([_hit(i) for i in [*unknown[:9], IDS[0], *unknown[9:]]])
    assert len(mixed.calls) == 13 and stats["stopped"] is None


def test_no_read_starts_after_the_time_budget(monkeypatch):
    from pipeline.connectors.us_grants_gov_search2 import connector as gg

    clock = [0.0]
    monkeypatch.setattr(gg, "_monotonic", lambda: clock[0])
    slow = _Http(on_call=lambda: clock.__setitem__(0, clock[0] + 120.0))  # each read takes 2 minutes
    details, stats = _connector(slow).fetch_details([_hit(i) for i in IDS], started=0.0)
    assert gg.DETAIL_BUDGET_S == 300.0
    assert slow.calls == IDS[:3] and stats["deferred"] == 1  # reads began at 0, 120 and 240 s
    assert stats["stopped"] == "time budget of 300 s spent" and list(details) == IDS[:3]
    # The budget counts from the start of the run's fetch: a slow search leaves less of it.
    clock[0] = 290.0
    late = _Http(on_call=lambda: clock.__setitem__(0, clock[0] + 20.0))
    _, stats = _connector(late).fetch_details([_hit(i) for i in IDS], started=0.0)
    assert late.calls == IDS[:1] and stats["deferred"] == 3


def test_a_challenge_stops_the_detail_reads_for_the_run():
    http = _Http(blocked=True)
    details, stats = _connector(http).fetch_details([_hit(i) for i in IDS])
    assert http.calls == [IDS[0]] and stats["blocked"] is True
    assert stats["deferred"] == 3 and details == {}


def test_reads_are_capped_per_run_new_notices_first(monkeypatch):
    from pipeline.connectors.us_grants_gov_search2 import connector as gg

    old, _ = _connector(_Http()).fetch_details([_hit(IDS[0])])
    monkeypatch.setattr(gg, "DETAIL_CAP", 2)
    http = _Http()
    details, stats = _connector(http, "2026-10-30T02:00:00+00:00", old).fetch_details([_hit(i) for i in IDS])
    assert http.calls == IDS[1:3]  # the two new notices, not the stale one listed first
    assert stats["deferred"] == 2 and details[IDS[0]] == old[IDS[0]]
    assert IDS[3] not in details


class _Api(_Http):
    """Both endpoints of the API: search2 answers the recorded search page (with a fresh token on
    every response, as the live API does) and fetchOpportunity the recorded details."""

    def __init__(self) -> None:
        super().__init__()
        self.searches = 0

    def post(self, url: str, **kwargs: object) -> _Resp:
        from pipeline.connectors.us_grants_gov_search2.connector import API_URL

        if url == API_URL:
            self.searches += 1
            page = json.loads(_with_tokens(_fixture(), f"S{self.searches}"))["pages"][0]
            return _Resp(page)
        return super().post(url, **kwargs)


def test_fetch_reads_search_then_details_and_a_check_with_the_same_figures_stores_nothing(tmp_path):
    """`fetch()` end to end through the runner with a fake session (review of lane rfp-value: no
    test drove it): the search, one detail read per hit, nothing personal stored, the value
    normalised; then an hourly run that asks for no detail, a weekly check that finds the same
    figures and so stores no snapshot and queues no load, and a changed figure that does."""
    import datetime as dt

    import pandas as pd

    from pipeline.connectors.runner import run
    from pipeline.connectors.store import Store

    store = Store(tmp_path)
    t0 = dt.datetime(2026, 10, 22, 1, tzinfo=dt.UTC)
    recorded = set(IDS)

    api = _Api()
    first = run(SOURCE_ID, store=store, http=api, now=t0)  # type: ignore[arg-type]
    assert first.status == "ok", first.run.get("error")
    assert api.searches == 1 and len(api.calls) == 12 and recorded <= set(api.calls)
    assert first.run["snapshot"]["requests_made"] == 13
    stored = first.paths["snapshot"].read_bytes()
    assert (
        b"agencyContact" not in stored and b"removed from fixture" not in stored and b"eyJ0eXAi" not in stored
    )
    frame = pd.read_parquet(first.paths["normalized"]).set_index("source_record_id")
    value = json.loads(frame.loc["363396", "identifiers"])["value"]
    assert value["kind"] == "total_programme_funding" and value["retrieved_at"] == "2026-10-22T01:00:00Z"
    assert frame.loc["363396", "budget_amount"] == 410_000_000.0

    hourly = _Api()
    second = run(SOURCE_ID, store=store, http=hourly, now=t0 + dt.timedelta(hours=1))  # type: ignore[arg-type]
    assert second.status == "unchanged" and not recorded & set(hourly.calls)

    weekly = _Api()
    third = run(SOURCE_ID, store=store, http=weekly, now=t0 + dt.timedelta(days=8))  # type: ignore[arg-type]
    assert recorded <= set(weekly.calls)
    assert third.status == "unchanged", "same figures: no snapshot, no load, no last_changed"
    checked = third.run["snapshot"]["meta"]["details"]["checked"]
    assert checked == {"2026-10-30T01:00:00Z": sorted(int(i) for i in IDS)}

    after = _Api()
    fourth = run(SOURCE_ID, store=store, http=after, now=t0 + dt.timedelta(days=8, hours=1))  # type: ignore[arg-type]
    assert fourth.status == "unchanged" and not recorded & set(after.calls)

    revised = _Api()
    revised.responses["363396"]["data"]["synopsis"]["estimatedFunding"] = "420000000"
    fifth = run(SOURCE_ID, store=store, http=revised, now=t0 + dt.timedelta(days=16))  # type: ignore[arg-type]
    assert fifth.status == "ok", fifth.run.get("error")
    later = pd.read_parquet(fifth.paths["normalized"]).set_index("source_record_id")
    assert later.loc["363396", "budget_amount"] == 420_000_000.0
    assert json.loads(later.loc["363396", "identifiers"])["value"]["retrieved_at"] == "2026-11-07T01:00:00Z"
    for hid in ("279638", "356623", "361526"):
        assert (
            json.loads(later.loc[hid, "identifiers"])["value"]
            == json.loads(frame.loc[hid, "identifiers"])["value"]
        )


def test_a_run_with_details_stores_no_contact_field_and_normalises_the_value(tmp_path):
    import datetime as dt

    import pandas as pd

    from pipeline.connectors.runner import run
    from pipeline.connectors.store import Store

    raw = snapshot(
        "grants_gov_search2.json",
        URL,
        "application/json",
        retrieved_at=dt.datetime(2026, 10, 22, 1, tzinfo=dt.UTC),
    )
    raw.content = _bundle_with_details(IDS)
    result = run(SOURCE_ID, store=Store(tmp_path), raw=raw)
    assert result.status == "ok", result.run.get("error")
    stored = result.paths["snapshot"].read_bytes()
    assert b"removed from fixture" not in stored and b"agencyContact" not in stored
    frame = pd.read_parquet(result.paths["normalized"]).set_index("source_record_id")
    assert frame.loc["361526", "budget_amount"] == 293_760_000.0


class _Wire:
    """A `requests.Session` stand-in under a real `PoliteSession`: search2 answers the recorded
    search page, fetchOpportunity answers `detail_status` (with `Retry-After`, if given)."""

    def __init__(self, detail_status: int, retry_after: str | None = None) -> None:
        self.headers: dict[str, str] = {}
        self.detail_status, self.retry_after = detail_status, retry_after
        self.detail_posts = 0

    def request(self, method: str, url: str, **kwargs: object) -> object:
        from types import SimpleNamespace

        from pipeline.connectors.us_grants_gov_search2.connector import API_URL, DETAIL_API_URL

        if url == API_URL:
            body = json.dumps(json.loads(_fixture())["pages"][0]).encode()
            return SimpleNamespace(status_code=200, headers={}, content=body, json=lambda: json.loads(body))
        assert url == DETAIL_API_URL and method == "POST"
        self.detail_posts += 1
        headers = {"Retry-After": self.retry_after} if self.retry_after else {}
        return SimpleNamespace(status_code=self.detail_status, headers=headers, content=b"{}", json=dict)


@pytest.mark.parametrize(("status", "retry_after"), [(503, None), (429, "3600")])
def test_an_outage_of_the_detail_endpoint_costs_one_request_and_the_search_is_stored(
    tmp_path, status, retry_after
):
    """Review of lane rfp-value (2026-10-22), as measured there: with fetchOpportunity answering
    503 for every notice, the run made 4 attempts per notice with backoff (544 POSTs, ~1,593 s for
    136 notices), past the scheduler's 600 s timeout, so the search snapshot was never stored; and
    a 429 was followed by more requests. Through the real session: one POST, no retry, no backoff
    or `Retry-After` wait, the session's own attempts restored, and the run stores the search."""
    import datetime as dt

    from pipeline.connectors.http import PoliteSession
    from pipeline.connectors.runner import run
    from pipeline.connectors.store import Store

    wire = _Wire(status, retry_after)
    sleeps: list[float] = []
    http = PoliteSession(
        rate_limits={"api.grants.gov": 1000.0},
        sleep=sleeps.append,
        session=wire,  # type: ignore[arg-type]
    )
    now = dt.datetime(2026, 10, 22, 1, tzinfo=dt.UTC)
    result = run(SOURCE_ID, store=Store(tmp_path), http=http, now=now)
    assert result.status == "ok", result.run.get("error")
    assert wire.detail_posts == 1 and not [s for s in sleeps if s >= 0.5]  # no backoff, no Retry-After
    details = result.run["snapshot"]["meta"]["details"]
    assert str(details["stopped"]).startswith("HttpFailed") and f"HTTP {status}" in details["stopped"]
    assert (details["requested"], details["failed"], details["deferred"]) == (1, 1, 11)
    assert http.max_attempts == 4  # the search, and every other source, keep their retries
    assert result.paths["snapshot"].exists() and result.paths["normalized"].exists()
