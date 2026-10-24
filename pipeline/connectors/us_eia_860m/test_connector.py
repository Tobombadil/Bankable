"""Parser tests for us.eia.860m against a recorded Planned sheet, plus the index-page scrape, and
against a recorded September 2026 workbook holding both the Planned and the "Canceled or
Postponed" sheet (`fixtures/`, its README says how it was cut)."""

from __future__ import annotations

import copy
import datetime as dt
import io
import json
import pathlib

import openpyxl
import pandas as pd
import pytest

from conftest import connector_for, snapshot
from pipeline.connectors.base import ConnectorError, ParseError, RawSnapshot
from pipeline.connectors.http import HttpBlocked
from pipeline.connectors.us_eia_860m.connector import (
    CANCELED_SHEET,
    INDEX_URL,
    canceled_sheet_state,
    find_xlsx_links,
    read_sheets,
)
from pipeline.diff import diff_snapshots
from pipeline.normalize import EIA_BA_ISO_TOKENS, STATUS_MAP_PATH, iso_token_from_eia_ba, technology_family

SOURCE_ID = "us.eia.860m"
URL = "https://www.eia.gov/electricity/data/eia860m/xls/july_generator2026.xlsx"


@pytest.fixture(scope="module")
def parsed():
    c = connector_for(SOURCE_ID)
    raw = snapshot("eia860m_planned.xlsx", URL)
    rows = c.parse(raw)
    return c, raw, rows, c.normalize(rows, raw)


def test_planned_sheet_header_is_on_the_third_row(parsed):
    _, _, rows, _ = parsed
    assert {"Plant ID", "Generator ID", "Status", "Technology"} <= set(rows[0])


def test_record_id_is_plant_plus_generator(parsed):
    _, _, rows, df = parsed
    expected = f"{int(rows[0]['Plant ID'])}-{str(rows[0]['Generator ID']).strip()}"
    assert df["source_record_id"].iloc[0] == expected
    assert not df["record_id"].duplicated().any()
    assert df["eia_plant_id"].notna().all()


def test_planned_statuses_map_onto_the_documented_vocabulary(parsed):
    _, _, _, df = parsed
    assert set(df["lifecycle_state"]) <= {"announced", "filed", "permitted", "under_construction"}
    assert (df["status_rule"] == "eia860m.map").all()


def test_construction_complete_is_not_built(parsed):
    _, _, _, df = parsed
    ts = df[df["status_raw"].str.startswith("(TS)", na=False)]
    assert set(ts["lifecycle_state"]) <= {"under_construction"}


def test_index_links_are_returned_in_page_order_without_archive_paths():
    """eia.gov robots.txt disallows `/*archive/`; those links must never be requested."""
    html = (
        '<a href="/electricity/data/eia860m/xls/december_generator2026.xlsx">dec</a>'
        '<a href="/electricity/data/eia860m/archive/xls/november_generator2026.xlsx">nov</a>'
        '<a href="/electricity/data/eia860m/xls/december_generator2026.xlsx">dup</a>'
        '<a href="/electricity/data/eia860m/xls/july_generator2026.xlsx">jul</a>'
    )
    links = find_xlsx_links(html)
    assert links == [
        "https://www.eia.gov/electricity/data/eia860m/xls/december_generator2026.xlsx",
        "https://www.eia.gov/electricity/data/eia860m/xls/july_generator2026.xlsx",
    ]


class _Answer:
    def __init__(self, content: bytes, content_type: str) -> None:
        self.status_code = 200
        self.content = content
        self.text = content.decode()
        self.headers = {"Content-Type": content_type}


class _Http:
    """Index page, one robots-blocked candidate, one HTML placeholder, then the real workbook."""

    def __init__(self, blocked: str) -> None:
        self.blocked = blocked
        self.urls: list[str] = []

    def get(self, url: str, **_: object) -> _Answer:
        self.urls.append(url)
        if url == INDEX_URL:
            return _Answer(
                b'<a href="/electricity/data/eia860m/xls/blocked_generator2026.xlsx">b</a>'
                b'<a href="/electricity/data/eia860m/xls/december_generator2026.xlsx">d</a>'
                b'<a href="/electricity/data/eia860m/xls/july_generator2026.xlsx">j</a>',
                "text/html",
            )
        if url == self.blocked:
            raise HttpBlocked(f"robots.txt disallows {url}")
        if url == URL:
            return _Answer(b"PK\x03\x04real", "application/octet-stream")
        return _Answer(b"<html>placeholder</html>", "text/html")


def test_fetch_skips_a_robots_blocked_candidate_and_records_it():
    blocked = "https://www.eia.gov/electricity/data/eia860m/xls/blocked_generator2026.xlsx"
    http = _Http(blocked)
    c = connector_for(SOURCE_ID, http=http)
    raw = c.fetch()
    assert raw.url == URL
    assert raw.content.startswith(b"PK")
    tried = raw.meta["candidates"]
    assert tried[0] == {"url": blocked, "blocked": f"robots.txt disallows {blocked}"}
    assert tried[1]["xlsx"] is False and tried[2]["xlsx"] is True
    assert raw.requests_made == 1 + len(tried)


def test_fetch_fails_cleanly_when_every_candidate_is_blocked():
    class _AllBlocked(_Http):
        def get(self, url: str, **_: object) -> _Answer:
            if url == INDEX_URL:
                return super().get(url)
            raise HttpBlocked(f"robots.txt disallows {url}")

    c = connector_for(SOURCE_ID, http=_AllBlocked(""))
    with pytest.raises(ConnectorError):
        c.fetch()


def test_an_html_placeholder_month_is_a_parse_error():
    """A guessed/future month answers HTTP 200 with HTML (docs/02 §7)."""
    c = connector_for(SOURCE_ID)
    raw = snapshot("grants_gov_search2.json", URL)
    raw.content = b"<!doctype html><html><body>Page not found</body></html>"
    with pytest.raises(ParseError):
        c.parse(raw)


def test_rto_balancing_authority_codes_become_the_iso_token_the_filters_use(parsed):
    """EIA-860M's `Balancing Authority Code` is EIA's BA code (`ERCO`, `CISO`, `NYIS`, `SWPP`,
    `ISNE`), which the proposal page printed under "ISO / operator" and `?iso=ERCOT` never matched.
    The seven RTO/ISO codes map to the market-operator token the ISO connectors write
    (`pipeline.normalize.EIA_BA_ISO_TOKENS`); a utility BA keeps its EIA code."""
    _, _, rows, df = parsed
    by_ba = dict(zip((r.get("Balancing Authority Code") for r in rows), df["iso"], strict=True))
    for code, token in {
        "ERCO": "ERCOT",
        "CISO": "CAISO",
        "NYIS": "NYISO",
        "SWPP": "SPP",
        "ISNE": "ISONE",
        "MISO": "MISO",
        "PJM": "PJM",
    }.items():
        if code in by_ba:
            assert by_ba[code] == token, code
    assert "ERCO" in by_ba, "the recorded sheet carries ERCOT rows; the check above would be empty"
    assert not set(df["iso"].dropna()) & {"ERCO", "CISO", "NYIS", "SWPP", "ISNE"}
    utility = {code: iso for code, iso in by_ba.items() if code not in EIA_BA_ISO_TOKENS and code}
    assert all(iso == code for code, iso in utility.items())


def test_the_ba_token_map_is_total_on_odd_input():
    assert iso_token_from_eia_ba(" erco ") == "ERCOT"
    assert iso_token_from_eia_ba("TVA") == "TVA"
    assert iso_token_from_eia_ba(None) is None
    assert iso_token_from_eia_ba("") is None


# ------------------------------------------------------------------ "Canceled or Postponed" (docs/28 F4)
SEPTEMBER_URL = "https://www.eia.gov/electricity/data/eia860m/xls/september_generator2026.xlsx"
SEPTEMBER_FIXTURE = (
    pathlib.Path(__file__).with_name("fixtures") / "september_generator2026_planned_and_canceled.xlsx"
)
#: Every column the two sheets carry (September 2026). None names a person: `Entity Name` is the
#: respondent company (a utility, a cooperative, a project company).
PLANNED_COLUMNS = {
    "Entity ID",
    "Entity Name",
    "Plant ID",
    "Plant Name",
    "Google Map",
    "Bing Map",
    "Plant State",
    "County",
    "Balancing Authority Code",
    "Sector",
    "Generator ID",
    "Unit Code",
    "Nameplate Capacity (MW)",
    "Net Summer Capacity (MW)",
    "Net Winter Capacity (MW)",
    "Technology",
    "Energy Source Code",
    "Prime Mover Code",
    "Planned Operation Month",
    "Planned Operation Year",
    "Status",
    "Latitude",
    "Longitude",
}
CANCELED_COLUMNS = PLANNED_COLUMNS - {"Planned Operation Month", "Planned Operation Year", "Status"}


def _september() -> RawSnapshot:
    return RawSnapshot(
        content=SEPTEMBER_FIXTURE.read_bytes(),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        url=SEPTEMBER_URL,
        retrieved_at=dt.datetime(2026, 10, 23, 21, 41, 36, tzinfo=dt.UTC),
        http_status=200,
        ext="xlsx",
    )


def _with_switch(c, state: str):
    """The connector with the one switch set: the eia860m map names the sheet."""
    status_map = copy.deepcopy(c.status_map)
    status_map["sources"]["eia860m"]["map"][CANCELED_SHEET] = state
    c._status_map = status_map
    return c


def test_the_switch_is_off_today_and_the_status_map_says_how_to_turn_it_on():
    """No lifecycle state says what the sheet says (canceled *or* postponed, unsaid which): the
    coordinator's default keeps a postponed unit out of `cancelled`, a `postponed` state needs a
    migration, and `unknown` takes no mapping. So the map names no state for the sheet yet."""
    c = connector_for(SOURCE_ID)
    assert canceled_sheet_state(c.status_map) is None
    text = STATUS_MAP_PATH.read_text(encoding="utf-8")
    assert '"Canceled or Postponed": postponed' in text
    assert '"Canceled or Postponed": cancelled' in text


#: Every place the lifecycle states are listed, as the status map's recipe for a `postponed` state
#: names them: (file, the text that lists the states there, the name the recipe uses).
POSTPONED_RECIPE: tuple[tuple[str, str, str], ...] = (
    (
        "services/db/migrations/versions/0001_initial_schema.py",
        "ck_proposal_lifecycle_state_vocab",
        "ck_proposal_lifecycle_state_vocab",
    ),
    ("services/db/models.py", "LIFECYCLE_STATES = (", "LIFECYCLE_STATES"),
    ("pipeline/status_map.yaml", "canonical_states:", "canonical_states"),
    ("data/vocabulary/lifecycle_states.yaml", "lifecycle_states:", "lifecycle_states"),
    ("services/labels.py", "LIFECYCLE_LABELS: dict[str, str] = {", "LIFECYCLE_LABELS"),
    ("services/api/app.py", "LIFECYCLE_STATE_VALUES = [", "LIFECYCLE_STATE_VALUES"),
    ("api/openapi.yaml", "    LifecycleState:", "LifecycleState"),
    ("services/trends/snapshot.py", "WITHDRAWN_STATES: tuple[str, ...] =", "WITHDRAWN_STATES"),
)


def test_the_recipe_for_a_postponed_state_names_every_place_the_states_are_listed():
    """Review of lane F4: the recipe left out `canonical_states` and the API's enum, and the
    `postponed` case below runs on a copy of the map, so nothing would have caught the gap."""
    root = pathlib.Path(__file__).resolve().parents[3]
    text = STATUS_MAP_PATH.read_text(encoding="utf-8")
    start = text.index("THE SWITCH")
    recipe = text[start : text.index("caveats:", start)]
    for path, listed, name in POSTPONED_RECIPE:
        assert listed in (root / path).read_text(encoding="utf-8"), (path, listed)
        assert name in recipe, name


def test_the_canceled_sheet_is_parsed_and_counted_but_not_loaded_while_the_switch_is_off(caplog):
    c = connector_for(SOURCE_ID)
    raw = _september()
    with caplog.at_level("INFO", logger="pipeline.connectors.us_eia_860m.connector"):
        rows = c.parse(raw)
    assert len(rows) == 28
    assert not any("_sheet" in r for r in rows)
    assert "Canceled or Postponed sheet: 27 units, not loaded" in caplog.text
    df = c.normalize(rows, raw)
    assert set(df["lifecycle_state"]) <= {"announced", "filed", "permitted", "under_construction"}


@pytest.mark.parametrize("state", ["cancelled", "postponed"])
def test_the_switch_loads_every_canceled_unit_with_the_mapped_state(state):
    """The two values the status map documents: `cancelled` (the owner's other answer) and
    `postponed` (once a migration adds the state)."""
    c = _with_switch(connector_for(SOURCE_ID), state)
    raw = _september()
    df = c.normalize(c.parse(raw), raw)
    canceled = df[df["status_raw"] == CANCELED_SHEET]
    assert len(df) == 28 + 27
    assert len(canceled) == 27
    assert set(canceled["lifecycle_state"]) == {state}
    assert set(canceled["status_rule"]) == {"eia860m.map"}
    assert canceled["proposed_cod"].isna().all()  # the sheet states no planned date
    assert all(json.loads(r)["_sheet"] == CANCELED_SHEET for r in canceled["raw"])
    planned = df[df["status_raw"] != CANCELED_SHEET]
    assert all("_sheet" not in json.loads(r) for r in planned["raw"])  # Planned payloads unchanged
    assert not df["record_id"].duplicated().any()


def test_a_unit_keeps_its_record_when_it_moves_from_planned_to_canceled():
    """The identity is `<Plant ID>-<Generator ID>` on both sheets, so a move is a lifecycle change of
    one record (a `withdrawn` diff event under `cancelled`), never a removal plus a new record."""
    c = _with_switch(connector_for(SOURCE_ID), "cancelled")
    raw = _september()
    planned = [r for r in c.parse(raw) if "_sheet" not in r][:1]
    dropped = ("Status", "Planned Operation Year", "Planned Operation Month")
    moved = [{**{k: v for k, v in planned[0].items() if k not in dropped}, "_sheet": CANCELED_SHEET}]
    before, after = c.normalize(planned, raw), c.normalize(moved, raw)
    assert list(before["record_id"]) == list(after["record_id"])
    events = diff_snapshots(before, after, observed_at="2026-10-24T00:00:00Z")
    assert "withdrawn" in set(events["event_type"])
    assert not {"new", "removed"} & set(events["event_type"])


def test_a_changed_canceled_sheet_fails_the_run_only_when_the_sheet_is_loaded():
    book = openpyxl.load_workbook(io.BytesIO(SEPTEMBER_FIXTURE.read_bytes()))
    book[CANCELED_SHEET]["C3"] = "Plant Number"  # the header loses `Plant ID`
    buffer = io.BytesIO()
    book.save(buffer)
    raw = _september()
    raw.content = buffer.getvalue()
    off = connector_for(SOURCE_ID)
    assert len(off.parse(raw)) == 28
    found = raw.meta["canceled_or_postponed"]
    assert found["units"] is None and "layout changed" in found["error"]
    with pytest.raises(ParseError):
        _with_switch(connector_for(SOURCE_ID), "cancelled").parse(raw)


def test_any_failure_reading_the_optional_sheet_leaves_the_planned_load_alone(monkeypatch):
    """Review of lane F4: only a missing `Plant ID` was caught, so any other error reading the sheet
    (a corrupt sheet, an engine error) failed the Planned load the sheet is not part of."""
    real_parse = pd.ExcelFile.parse

    def parse(self, sheet_name=0, *args, **kwargs):
        if sheet_name == CANCELED_SHEET:
            raise ValueError("unreadable sheet")
        return real_parse(self, sheet_name, *args, **kwargs)

    monkeypatch.setattr(pd.ExcelFile, "parse", parse)
    raw = _september()
    rows = connector_for(SOURCE_ID).parse(raw)
    assert len(rows) == 28
    assert raw.meta["canceled_or_postponed"]["error"] == "ValueError: unreadable sheet"
    with pytest.raises(ValueError, match="unreadable sheet"):
        _with_switch(connector_for(SOURCE_ID), "cancelled").parse(_september())


def test_what_the_canceled_sheet_held_is_kept_in_the_run_record(tmp_path):
    """Review of lane F4: the count was only logged. It is written to the snapshot's `meta`, which
    the run record keeps and `Store.last_snapshot` reads back (fixture: 27 units, 25 plants)."""
    from pipeline.connectors.runner import run
    from pipeline.connectors.store import Store

    st = Store(tmp_path)
    result = run(SOURCE_ID, store=st, raw=_september())
    expected = {"units": 27, "plants": 25, "nameplate_mw": 3448.9, "loaded_as": None, "error": None}
    assert result.run["snapshot"]["meta"]["canceled_or_postponed"] == expected
    stored = st.last_snapshot(SOURCE_ID)
    assert stored is not None
    assert stored.meta["canceled_or_postponed"] == expected


def test_no_column_of_either_sheet_names_a_person():
    """CLAUDE.md: no natural person's name is stored. Both sheets carry plant, generator, place and
    company columns only; a new column would show here first."""
    sheets = read_sheets(SEPTEMBER_FIXTURE.read_bytes(), ("Planned", CANCELED_SHEET))
    assert {k for r in sheets["Planned"] or [] for k in r} == PLANNED_COLUMNS
    assert {k for r in sheets[CANCELED_SHEET] or [] for k in r} == CANCELED_COLUMNS
    person_words = ("contact", "owner name", "person", "phone", "email", "first name", "last name")
    assert not any(w in col.lower() for col in PLANNED_COLUMNS for w in person_words)


def test_every_technology_on_the_canceled_sheet_reaches_a_family():
    """The fixture holds the first unit of each of the sheet's 27 technologies (September 2026)."""
    c = _with_switch(connector_for(SOURCE_ID), "cancelled")
    raw = _september()
    df = c.normalize(c.parse(raw), raw)
    canceled = df[df["status_raw"] == CANCELED_SHEET]
    assert canceled["technology_raw"].nunique() == 27
    assert all(technology_family(t) is not None for t in canceled["technology"])
    by_raw = dict(zip(canceled["technology_raw"], canceled["technology"], strict=True))
    assert by_raw["Coal Integrated Gasification Combined Cycle"] == "coal"
    assert by_raw["Solar Thermal without Energy Storage"] == "solar_thermal"
    assert by_raw["Flywheels"] == "storage"
    assert by_raw["Other Gases"] == "gas_other"
