"""Parser tests for us.epa.class_vi against a recorded Qlik payload, plus the fetch-time scrape.

The fixture `tests/fixtures/epa_class_vi_tracker.json` is a real 2026-09-22 read of EPA's public
Qlik Sense app, trimmed (see its `_fixture_note`). `fetch()` never runs in CI: the two fetch tests
drive a scripted page response and a scripted Engine JSON API exchange.
"""

from __future__ import annotations

import json
from typing import ClassVar

import pytest

from conftest import connector_for, fixture_path, snapshot
from pipeline.connectors.base import ConnectorError, ParseError
from pipeline.connectors.canonical import classify_tech
from pipeline.connectors.us_epa_class_vi.connector import (
    FIELDS,
    PAGE_URL,
    find_app_id,
    find_tracker_pdf,
)

SOURCE_ID = "us.epa.class_vi"
APP_ID = "8c074297-7f9e-4217-82f0-fb05f54f28e7"
URL = f"https://awsedap.epa.gov/public/single/?appid={APP_ID}"
FIXTURE = "epa_class_vi_tracker.json"


@pytest.fixture(scope="module")
def parsed():
    c = connector_for(SOURCE_ID)
    raw = snapshot(FIXTURE, URL, content_type="application/json")
    rows = c.parse(raw)
    return c, raw, rows, c.normalize(rows, raw)


@pytest.fixture(scope="module")
def payload():
    return json.loads(fixture_path(FIXTURE).read_text())


# ------------------------------------------------------------------ parse
def test_blank_spreadsheet_padding_rows_are_dropped(parsed, payload):
    """The app's table is a spreadsheet export: 175 of its 243 rows carry no project."""
    _, _, rows, _ = parsed
    blanks = [r for r in payload["rows"] if not (r.get("Project Name") or "").strip()]
    assert blanks, "fixture must retain some padding rows"
    assert len(rows) == len(payload["rows"]) - len(blanks)
    assert all(r["Project Name"] for r in rows)


def test_nod_and_rai_ladders_keep_only_the_rounds_that_happened(parsed):
    _, _, rows, _ = parsed
    longest = max(rows, key=lambda r: len(r["rais"]))
    assert [r["round"] for r in longest["rais"]] == list(range(1, len(longest["rais"]) + 1))
    assert all(r["sent"] or r["response_received"] for row in rows for r in row["rais"])
    assert all(r["sent"] or r["response_received"] for row in rows for r in row["nods"])


def test_a_payload_that_is_not_the_tracker_is_a_parse_error():
    c = connector_for(SOURCE_ID)
    raw = snapshot(FIXTURE, URL, content_type="application/json")
    raw.content = b'{"rows": [{"Something Else": 1}]}'
    with pytest.raises(ParseError):
        c.parse(raw)


# ------------------------------------------------------------------ identity
def test_record_id_prefers_epas_own_gsdt_project_id(parsed):
    _, _, rows, df = parsed
    with_gsdt = [(i, r) for i, r in enumerate(rows) if r["GSDT Project ID"]]
    assert with_gsdt, "fixture must cover the GSDT identity path"
    for i, r in with_gsdt:
        assert df["source_record_id"].iloc[i] == r["GSDT Project ID"]
        assert df["cross_refs"].iloc[i] == f"GSDT:{r['GSDT Project ID'].upper()}"
    assert not df["record_id"].duplicated().any()
    assert (df["record_id"] == SOURCE_ID + ":" + df["source_record_id"]).all()


def test_a_project_without_a_gsdt_id_falls_back_to_a_content_hash(parsed):
    _, _, rows, df = parsed
    without = [(i, r) for i, r in enumerate(rows) if not r["GSDT Project ID"]]
    assert without, "fixture must cover the hashed identity path"
    for i, _ in without:
        assert df["source_record_id"].iloc[i].startswith("h")
        assert df["cross_refs"].iloc[i] == ""


def test_row_order_is_never_the_identity(parsed):
    """`RowOrder` is a spreadsheet position; using it would rename every project on a re-sort."""
    _, _, rows, df = parsed
    assert all(r["RowOrder"] for r in rows)
    assert not set(df["source_record_id"]) & {r["RowOrder"] for r in rows}


# ------------------------------------------------------------------ lifecycle
def test_every_phase_maps_onto_the_documented_vocabulary(parsed):
    _, _, _, df = parsed
    assert set(df["lifecycle_state"]) <= {"filed", "studied", "permitted", "withdrawn"}
    assert (df["status_rule"] == "epa_class_vi.map").all()
    assert not df["status_raw"].isna().any()


def test_a_final_permit_decision_is_permitted_not_built(parsed):
    _, _, _, df = parsed
    issued = df[df["status_raw"] == "Final Permit Decisions Issued"]
    assert len(issued)
    assert set(issued["lifecycle_state"]) == {"permitted"}


def test_an_unknown_phase_is_flagged_rather_than_guessed(parsed):
    c, raw, rows, _ = parsed
    mutated = [dict(r) for r in rows]
    mutated[0]["Phase"] = "Awaiting Regional Concurrence"
    df = c.normalize(mutated, raw)
    assert df["lifecycle_state"].iloc[0] == "unknown"
    assert df["status_rule"].iloc[0] == "epa_class_vi.unmapped"


# ------------------------------------------------------------------ fields
def test_kind_is_ccs_and_the_battery_storage_misclassification_is_avoided(parsed):
    """`classify_tech` would call a CO2 storage project a battery: the connector bypasses it."""
    _, _, _, df = parsed
    assert set(df["kind"]) == {"ccs"}
    assert set(df["technology"]) == {"co2_geologic_sequestration"}
    assert classify_tech("CO2 geologic storage")[0] == "storage"  # the trap, asserted explicitly


def test_capacity_and_cod_stay_null_because_the_tracker_carries_neither(parsed):
    _, _, _, df = parsed
    assert df["capacity_mw"].isna().all()
    assert df["storage_mwh"].isna().all()
    assert df["proposed_cod"].isna().all()
    assert df["queue_date"].notna().any()


def test_a_tribal_jurisdiction_leaves_state_null_rather_than_guessing(parsed):
    _, _, rows, df = parsed
    tribal = [i for i, r in enumerate(rows) if r["State"] in ("Osage Nation", "All Other Indian Tribes")]
    assert tribal, "fixture must cover EPA's tribal jurisdiction labels"
    for i in tribal:
        assert df["state"].iloc[i] is None
        assert df["county"].iloc[i]


def test_provenance_quartet_and_per_project_source_url(parsed):
    c, raw, rows, df = parsed
    assert (df["source_id"] == SOURCE_ID).all()
    assert (df["retrieved_at"] == raw.retrieved_at_iso).all()
    assert (df["licence_id"] == c.source.licence_id).all()
    assert df["source_url"].notna().all()
    linked = [i for i, r in enumerate(rows) if r["URL - Vlookup"]]
    assert linked
    for i in linked:
        assert df["source_url"].iloc[i] == rows[i]["URL - Vlookup"]


def test_epa_staff_names_are_never_requested_or_stored(payload, parsed):
    """`Primary Permit Writer` exists in the app and is deliberately outside FIELDS (docs/13 §5)."""
    assert "Primary Permit Writer" not in FIELDS
    assert all("Primary Permit Writer" not in row for row in payload["rows"])
    _, _, _, df = parsed
    assert not df["raw"].str.contains("Primary Permit Writer").any()


# ------------------------------------------------------------------ fetch-time scrape
PAGE_HTML = (
    "<p>The dashboard below contains information on Class VI permit applications.</p>"
    f'<a href="https://awsedap.epa.gov/public/single/?appid={APP_ID}&amp;sheet=5131&amp;opt=ctxmenu">tab</a>'
    '<a href="/system/files/documents/2026-05/permit-tracker_5-22-26.pdf">'
    "UIC Class VI Permit Tracker (pdf)</a>"
    '<a href="https://azdeq.gov/UIC">Arizona</a>'
)


def test_the_dashboard_app_id_and_tracker_pdf_are_read_off_the_page():
    assert find_app_id(PAGE_HTML) == APP_ID
    assert find_tracker_pdf(PAGE_HTML) == (
        "https://www.epa.gov/system/files/documents/2026-05/permit-tracker_5-22-26.pdf"
    )
    assert find_app_id("<p>no dashboard here</p>") is None


class _Page:
    status_code = 200
    text = PAGE_HTML
    headers: ClassVar[dict[str, str]] = {"Content-Type": "text/html"}

    @property
    def content(self) -> bytes:
        return self.text.encode()


class _Http:
    def __init__(self) -> None:
        self.urls: list[str] = []

    def get(self, url: str, **_: object) -> _Page:
        self.urls.append(url)
        return _Page()


class _Engine:
    """A scripted Qlik Engine JSON API socket: two greetings, then one reply per request."""

    def __init__(self, fields: list[str], rows: list[dict[str, object]]) -> None:
        self.fields = fields
        self.rows = rows
        self.outbox: list[str] = [
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "method": "OnAuthenticationInformation",
                    "params": {"mustAuthenticate": False},
                }
            ),
            json.dumps(
                {"jsonrpc": "2.0", "method": "OnConnected", "params": {"qSessionState": "SESSION_CREATED"}}
            ),
        ]
        self.sent: list[dict[str, object]] = []
        self.cube: list[str] = []
        self.closed = False

    def send(self, text: str) -> None:
        request = json.loads(text)
        self.sent.append(request)
        rid, method = request["id"], request["method"]
        if method == "OpenDoc":
            result = {"qReturn": {"qType": "Doc", "qHandle": 1}}
        elif method == "GetTablesAndKeys":
            result = {"qtr": [{"qName": "External", "qFields": [{"qName": f} for f in self.fields]}]}
        elif method == "CreateSessionObject":
            # The engine answers with the columns the caller asked for, not the app's whole table.
            self.cube = [
                d["qDef"]["qFieldDefs"][0].strip("[]")
                for d in request["params"][0]["qHyperCubeDef"]["qDimensions"]
            ]
            result = {"qReturn": {"qType": "GenericObject", "qHandle": 2}}
        elif method == "GetHyperCubeData":
            top = request["params"][1][0]["qTop"]
            window = self.rows[top : top + request["params"][1][0]["qHeight"]]
            matrix = [[{"qText": r.get(f) or "-"} for f in self.cube] for r in window]
            result = {"qDataPages": [{"qMatrix": matrix}]}
        else:  # pragma: no cover - the connector sends nothing else
            raise AssertionError(method)
        self.outbox.append(json.dumps({"jsonrpc": "2.0", "id": rid, "result": result}))

    def recv(self) -> str:
        return self.outbox.pop(0)

    def close(self) -> None:
        self.closed = True


def _fetch_with(engine: _Engine, http: _Http | None = None):
    c = connector_for(SOURCE_ID, http=http or _Http())
    c.connect = lambda _url, _timeout: engine
    return c.fetch()


def test_fetch_scrapes_the_page_then_reads_the_engine(payload):
    engine = _Engine([*FIELDS, "Primary Permit Writer"], payload["rows"])
    http = _Http()
    raw = _fetch_with(engine, http)
    body = json.loads(raw.content)
    assert http.urls == [PAGE_URL]
    assert raw.meta["app_id"] == APP_ID
    assert raw.meta["anonymous_access"] is True
    assert raw.meta["rows_returned"] == len(payload["rows"])
    assert body["fields"] == list(FIELDS)
    assert "Primary Permit Writer" not in json.dumps(body["rows"])
    assert [m["method"] for m in engine.sent][:3] == ["OpenDoc", "GetTablesAndKeys", "CreateSessionObject"]
    assert engine.closed
    assert len(connector_for(SOURCE_ID, http=_Http()).parse(raw)) == len(
        [r for r in payload["rows"] if r["Project Name"]]
    )


def test_fetch_fails_closed_when_the_dashboard_drops_a_field(payload):
    engine = _Engine([f for f in FIELDS if f != "Phase"], payload["rows"])
    with pytest.raises(ConnectorError, match="dropped"):
        _fetch_with(engine)


def test_fetch_fails_closed_when_the_page_no_longer_embeds_the_dashboard(payload):
    class _Bare(_Http):
        def get(self, url: str, **_: object) -> _Page:
            self.urls.append(url)
            page = _Page()
            page.text = "<p>This tracker has moved.</p>"
            return page

    engine = _Engine(list(FIELDS), payload["rows"])
    with pytest.raises(ConnectorError, match="appid"):
        _fetch_with(engine, _Bare())
