"""Parser and fetch tests for us.lbnl.queued_up against the recorded 2026 edition (never live)."""

from __future__ import annotations

import datetime as dt
import hashlib
import io

import openpyxl
import pytest

from conftest import connector_for, fixture_path, snapshot
from pipeline.connectors.base import BlockedError, ConnectorError, ParseError, PreviousSnapshot
from pipeline.connectors.us_lbnl_queued_up.connector import (
    EDITION_PAGE,
    FIRST_HAND_REGIONS,
    KNOWN_EDITION,
    LICENCE_SENTENCES,
    NOTES_SHA256,
    OPERATOR_TERMS_SOURCE,
    PUBLISHED_REGIONS,
    WITHHELD_REGIONS,
    data_file_url,
    licence_problems,
    name_or_none,
    newer_editions,
    notes_sha256,
    record_key,
    technology_of,
)

SOURCE_ID = "us.lbnl.queued_up"
PAGE = EDITION_PAGE.format(year=2026)
DATA_URL = (
    "https://eta-publications.lbl.gov/sites/default/files/2026-05/lbnl_ix_queue_data_file_thru2025.xlsx"
)
WORKBOOK = "lbnl_queued_up_thru2025.xlsx"
PAGE_HTML = fixture_path("lbnl_queued_up_publication.html").read_text(encoding="utf-8")


def _lbnl(**kwargs: object):
    """The connector as a quarantine run builds it: the source is gated while it is held
    (`reuse: unknown`, docs/13 §2.1), so the registry refuses it without `allow_restricted`."""
    return connector_for(SOURCE_ID, allow_restricted=True, **kwargs)


@pytest.fixture(scope="module")
def parsed():
    c = _lbnl()
    raw = snapshot(WORKBOOK, DATA_URL)
    rows = c.parse(raw)
    return c, raw, rows, c.normalize(rows, raw)


# ------------------------------------------------------------------ scope
def test_only_the_non_iso_regions_are_kept(parsed):
    _, raw, rows, df = parsed
    assert {r["region"] for r in rows} == PUBLISHED_REGIONS
    assert len(df) == 33  # 34 West and Southeast rows, less TVA 0's unlocated copy
    assert raw.meta["rows_by_region"] == {"CAISO": 1, "ERCOT": 1, "NYISO": 1, "Southeast": 8, "West": 26}
    assert raw.meta["dropped_first_hand"] == {"ERCOT": 1, "CAISO": 1, "NYISO": 1}
    assert raw.meta["dropped_withheld"] == {} and raw.meta["dropped_unrecognised_region"] == {}


def _edited(column: str, values: dict[int, object]) -> bytes:
    """The fixture with some rows' `column` rewritten in memory (fixture rows are never edited on
    disk, and no PJM, MISO, SPP or ISO-NE row is ever committed as a fixture: tests/fixtures/README.md).
    Keys are worksheet row numbers (the header is row 2)."""
    wb = openpyxl.load_workbook(fixture_path(WORKBOOK))
    ws = wb["03. Complete Queue Data"]
    col = [c.value for c in ws[2]].index(column) + 1
    for row, value in values.items():
        ws.cell(row=row, column=col).value = value  # `value=None` would leave the cell as it was
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _with_regions(regions: dict[int, str]) -> bytes:
    return _edited("region", dict(regions))


def _parse_edited(column: str, values: dict[int, object]):
    c = _lbnl()
    raw = snapshot(WORKBOOK, DATA_URL)
    raw.content = _edited(column, values)
    rows = c.parse(raw)
    return raw, rows, c.normalize(rows, raw)


def test_pjm_miso_spp_and_isone_rows_never_become_records():
    """CLAUDE.md: PJM rows are not public until a licence exists, whatever LBNL's licence says; MISO,
    SPP and ISO-NE follow their operators' recorded status. They are dropped in `parse`, so no
    record, no `raw` payload and no event can carry them."""
    c = _lbnl()
    raw = snapshot(WORKBOOK, DATA_URL)
    raw.content = _with_regions({3: "PJM", 4: "MISO", 5: "SPP", 6: "ISO-NE", 7: "Mid-Atlantic"})
    rows = c.parse(raw)
    assert len(rows) == 33 - 5
    assert raw.meta["dropped_withheld"] == {"PJM": 1, "MISO": 1, "SPP": 1, "ISO-NE": 1}
    assert raw.meta["dropped_unrecognised_region"] == {"Mid-Atlantic": 1}
    df = c.normalize(rows, raw)
    assert not df["raw"].str.contains('"region": "(?:PJM|MISO|SPP|ISO-NE|Mid-Atlantic)"', regex=True).any()


def test_the_region_tables_name_the_register_rows_that_explain_them(registry):
    for source_id in (*FIRST_HAND_REGIONS.values(), *WITHHELD_REGIONS.values()):
        assert source_id in registry
    assert registry.get(FIRST_HAND_REGIONS["CAISO"]).implemented
    for source_id in WITHHELD_REGIONS.values():
        assert registry.get(source_id).publication == "none"


def test_the_kept_rows_stay_unpublished_while_the_utilities_terms_are_unread(registry):
    """Review finding 1, 2026-10-23: the West and Southeast rows restate the non-ISO utilities' own
    OASIS queues, whose terms are recorded as unknown. The rule that withholds MISO's rows (a
    compiler's CC BY cannot license what the operator's terms withhold) holds them too, so the
    source carries the rows' class (`unknown`, `publication: none`: gated, never loaded) until those
    terms are read or the owner decides otherwise, and this test changes with that decision."""
    operators = registry.get(OPERATOR_TERMS_SOURCE)
    assert operators.category == "generation_queue"
    source = registry.get(SOURCE_ID)
    assert source.implemented
    if operators.reuse in ("unknown", "restricted") or operators.publication == "none":
        assert source.gated and source.publication == "none"


def test_a_workbook_with_no_publishable_row_fails_closed():
    c = _lbnl()
    raw = snapshot(WORKBOOK, DATA_URL)
    raw.content = _with_regions(dict.fromkeys(range(3, 40), "PJM"))
    with pytest.raises(ParseError, match="no row in the regions"):
        c.parse(raw)


# ------------------------------------------------------------------ layout
def test_a_missing_column_fails_the_parse_closed():
    wb = openpyxl.load_workbook(fixture_path(WORKBOOK))
    ws = wb["03. Complete Queue Data"]
    for cell in ws[2]:
        if cell.value == "q_status":
            cell.value = "status"
    buf = io.BytesIO()
    wb.save(buf)
    c = _lbnl()
    raw = snapshot(WORKBOOK, DATA_URL)
    raw.content = buf.getvalue()
    with pytest.raises(ParseError, match="q_status"):
        c.parse(raw)


def test_a_renamed_sheet_or_a_non_xlsx_fails_closed():
    wb = openpyxl.load_workbook(fixture_path(WORKBOOK))
    wb["03. Complete Queue Data"].title = "03. Queue"
    buf = io.BytesIO()
    wb.save(buf)
    c = _lbnl()
    raw = snapshot(WORKBOOK, DATA_URL)
    raw.content = buf.getvalue()
    with pytest.raises(ParseError, match="complete queue data"):
        c.parse(raw)
    raw.content = b"<!doctype html><title>Attention Required! | Cloudflare</title>"
    with pytest.raises(ParseError, match="not an xlsx"):
        c.parse(raw)


# ------------------------------------------------------------------ normalise
def test_provenance_quartet_and_licence(parsed):
    c, _, _, df = parsed
    assert set(df["source_id"]) == {SOURCE_ID}
    assert set(df["source_url"]) == {DATA_URL}
    assert set(df["licence_id"]) == {c.source.licence_id}
    assert df["retrieved_at"].notna().all()
    assert (
        c.source.credit_text and "GridTracker" in c.source.credit_text and "CC BY 4.0" in c.source.credit_text
    )


def test_a_position_listed_twice_is_one_record_the_located_one(parsed):
    """Review finding 4, 2026-10-23: LBNL lists TVA position 0 twice, once with Choctaw County, MS
    and once with no location, every other field equal. It is one request, so one record."""
    _, raw, _, df = parsed
    assert not df["record_id"].duplicated().any()
    assert "BPA/G0203" in set(df["source_record_id"])
    tva = df[df["source_record_id"] == "TVA/0"]
    assert len(tva) == 1 and not tva["record_id"].str.contains("#h").any()
    assert (tva["county"].iloc[0], tva["state"].iloc[0]) == ("Choctaw", "MS")
    assert raw.meta["repeats_collapsed"] == 1 and df.attrs["duplicates_resolved"] == 0


def test_a_repeat_that_disagrees_stays_two_suffixed_records():
    """Duke Energy Florida's position 2 is listed at 1,701 MW with Polk County and at 701 MW with no
    location: which is right is not ours to say, so both stay, suffixed, and the DQ gate warns."""
    c = _lbnl()
    raw, _, df = _parse_edited("mw_1", {39: 481})  # the unlocated copy of TVA 0
    tva = df[df["source_record_id"] == "TVA/0"]
    assert len(tva) == 2 and tva["record_id"].str.contains("#h").all()
    assert {c.project_root(k.split(":", 1)[1]) for k in tva["record_id"]} == {"TVA/0"}
    assert raw.meta["repeats_collapsed"] == 0 and df.attrs["duplicates_resolved"] == 2


def test_placeholder_developers_and_names_are_no_name():
    """Review finding 3, 2026-10-23: 'N/A' (863 rows), 'Masked' (203) and 'Third Party' (28) went
    through as sponsors, so the loader made organisations of them and named proposals after them."""
    placeholders = {4: "N/A", 5: "Masked", 6: "Third Party", 8: "TBD", 21: "Confidential", 22: " unknown "}
    _, _, df = _parse_edited("developer", placeholders)
    by_id = df.set_index("source_record_id")
    for key in ("APS/Q274", "APS/Q311", "APS/Q399", "BHCT/BHCT G19", "EPE/LG-17-7-1", "EPE/LG-17-9-1"):
        assert by_id.loc[key, "sponsor_name"] is None, key
        assert by_id.loc[key, "sponsor_norm"] is None, key
    assert by_id.loc["BPA/G0203", "sponsor_name"] == "Avangrid Power, LLC"
    _, _, named = _parse_edited("project_name", {21: "N/A", 22: "Masked"})
    named = named.set_index("source_record_id")
    assert named.loc["EPE/LG-17-7-1", "name_canonical"] is None
    assert named.loc["EPE/LG-17-9-1", "name_canonical"] is None
    assert named.loc["EPE/LG-17-9-3", "name_canonical"] == "AC120SB Project"


@pytest.mark.parametrize(
    "placeholder",
    [
        *("N/A", "n/a", "NA", "Masked", "MASKED", "Third Party", "Third-Party", "TBD", "TBA", "Unknown"),
        *("Confidential", "-", "  ", None, "Not Available."),
    ],
)
def test_placeholders_are_no_name(placeholder):
    assert name_or_none(placeholder) is None


@pytest.mark.parametrize(
    "name", ["Nautilus Solar", "NA Wind, LLC", "Third Party Energy Partners, LLC", "Masked Hill Solar"]
)
def test_names_that_contain_a_placeholder_word_are_kept(name):
    assert name_or_none(name) == name


def test_a_developer_who_is_a_natural_person_is_not_stored():
    """Review nit, 2026-10-23 (CLAUDE.md: store the minimum personal data): a developer named as a
    person (the 2026 edition has seven such rows) is removed from the row, sponsor and `raw` alike."""
    raw, _, df = _parse_edited("developer", {4: "Alan Quillfeather"})
    q274 = df[df["source_record_id"] == "APS/Q274"].iloc[0]
    assert q274["sponsor_name"] is None
    assert not df["raw"].str.contains("Quillfeather").any()
    assert raw.meta["developers_withheld_as_persons"] == 1
    assert df.set_index("source_record_id").loc["BPA/G0203", "sponsor_name"] == "Avangrid Power, LLC"


def test_the_operator_is_the_transmission_provider_by_name(parsed):
    """Review nit, 2026-10-23: `iso` printed LBNL's entity codes ('SRP_ANPP', 'Duke' for three
    Duke utilities) under "ISO / operator". BPA's `utility` column names the customer instead."""
    _, _, _, df = parsed
    by_id = df.set_index("source_record_id")
    assert by_id.loc["BPA/G0203", "iso"] == "Bonneville Power Administration"  # utility: Avangrid
    assert by_id.loc["SOCO/IC-236", "iso"] == "Southern Company"
    assert by_id.loc["Duke/283", "iso"] == "Duke Energy Florida"
    assert by_id.loc["IP/BPA", "iso"] == "Idaho Power Company"
    # A blank `utility` takes the one name the entity's other rows give, else the fallback.
    _, _, edited = _parse_edited("utility", {4: None, 20: None})
    edited = edited.set_index("source_record_id")
    assert edited.loc["APS/Q274", "iso"] == "Arizona Public Service"
    assert edited.loc["Duke/283", "iso"] == "Duke Energy"


def test_a_position_lbnl_did_not_assign_gets_a_content_key():
    row = {"entity": "APS", "q_id": "not assigned", "q_date": dt.date(2019, 2, 15), "county": "Coconino"}
    key = record_key(row)
    assert key.startswith("APS/h") and key == record_key(dict(row))
    assert record_key({"entity": "TVA", "q_id": 0.0}) == "TVA/0"


def test_lifecycle_follows_status_then_study_phase(parsed):
    _, _, _, df = parsed
    by_rule = dict(
        zip(df["source_record_id"], zip(df["lifecycle_state"], df["status_rule"], strict=True), strict=True)
    )
    assert by_rule["GTC/GI-128"] == ("under_construction", "lbnl_queued_up.active_construction")
    assert by_rule["EPE/LG-17-7-1"] == ("contracted", "lbnl_queued_up.active_ia_executed")
    assert by_rule["EPE/LG-17-9-1"] == ("contracted", "lbnl_queued_up.suspended_ia_executed")
    assert by_rule["BHCT/BHCT G19"] == ("studied", "lbnl_queued_up.active_ia_pending")
    assert by_rule["APS/Q274"] == ("studied", "lbnl_queued_up.active_cluster_study")
    assert by_rule["WAPA-SN/2024-G2"] == ("filed", "lbnl_queued_up.map")  # Not Started
    assert by_rule["PacifiCorp/642"] == ("filed", "lbnl_queued_up.map")  # active, phase "Suspended"
    assert by_rule["Duke/283"] == ("filed", "lbnl_queued_up.map")  # suspended, no agreement
    assert by_rule["SOCO/IC-236"] == ("built", "lbnl_queued_up.map")  # Vogtle Unit 3
    # LBNL's own `unknown` falls through, so the DQ gate counts it (`unknown` is never a map target).
    assert by_rule["IP/BPA"] == ("unknown", "lbnl_queued_up.unmapped")
    assert list(df.loc[df["status_rule"].str.endswith(".unmapped"), "source_record_id"]) == ["IP/BPA"]


def test_an_unknown_status_stays_unknown_whatever_its_study_phase():
    """Review nit, 2026-10-23: `unknown` with 'IA Executed' was mapped to `contracted`, a positive
    claim from a status LBNL itself could not read."""
    _, _, df = _parse_edited("IA_phase_clean", {27: "IA Executed"})
    row = df[df["source_record_id"] == "IP/BPA"].iloc[0]
    assert (row["lifecycle_state"], row["status_rule"]) == ("unknown", "lbnl_queued_up.unmapped")


def test_restating_status_from_raw_reproduces_the_states(parsed):
    c, _, _, df = parsed
    restated = c.restate_status(df)
    assert restated is not None
    assert list(restated["lifecycle_state"]) == list(df["lifecycle_state"])


def test_hybrids_keep_their_class_and_firm_generation_with_storage_is_the_generation():
    assert technology_of("Solar+Battery") == ("solar_storage", "generation")
    assert technology_of("Wind+Battery") == ("wind_storage", "generation")
    assert technology_of("Gas+Battery") == ("gas_other", "generation")
    assert technology_of("Battery") == ("storage", "storage")
    assert technology_of("Other Storage") == ("storage", "storage")
    assert technology_of(None) == ("unknown", "other")


def test_fields_map_onto_the_canonical_columns(parsed):
    _, _, _, df = parsed
    vogtle = df[df["source_record_id"] == "SOCO/IC-236"].iloc[0]
    assert vogtle["name_canonical"] == "Vogtle Unit 3"
    assert (
        vogtle["technology"] == "nuclear" and vogtle["iso"] == "Southern Company" and vogtle["state"] == "GA"
    )
    assert vogtle["capacity_mw"] > 0 and vogtle["queue_id"] == "IC-236"
    juniper = df[df["source_record_id"] == "BPA/G0203"].iloc[0]
    assert juniper["sponsor_name"] == "Avangrid Power, LLC" and juniper["study_phase"] == "IA Executed"
    assert df["capacity_mw"].notna().all()
    assert df["queue_date"].notna().sum() > 25


# ------------------------------------------------------------------ licence and page
def test_the_recorded_page_carries_the_licence_and_links_the_data_file():
    assert licence_problems(PAGE_HTML) == []
    assert data_file_url(PAGE_HTML, PAGE) == DATA_URL


def test_a_changed_licence_sentence_is_reported():
    changed = PAGE_HTML.replace("You may use, share, or adapt", "You may view")
    assert licence_problems(changed) == [LICENCE_SENTENCES[1]]
    assert licence_problems(PAGE_HTML.replace("creativecommons.org/licenses/by/4.0", "example.org")) == [
        "a link to creativecommons.org/licenses/by/4.0"
    ]


def test_the_recorded_notes_block_is_the_reviewed_one():
    assert notes_sha256(PAGE_HTML) == NOTES_SHA256


def test_an_added_restriction_passes_the_sentence_check_but_not_the_notes_hash():
    """Review finding 5, 2026-10-23: the sentence check alone lets a new condition through."""
    added = PAGE_HTML.replace(
        "All rights reserved.</p>",
        "All rights reserved.</p><p>Fields sourced from GridTracker may not be used commercially.</p>",
    )
    assert licence_problems(added) == []
    assert notes_sha256(added) != NOTES_SHA256
    relinked = PAGE_HTML.replace("licenses/by/4.0/deed.en", "licenses/by/4.0/legalcode.en")
    assert licence_problems(relinked) == [] and notes_sha256(relinked) != NOTES_SHA256
    assert notes_sha256("<html><body>no notes row</body></html>") is None


def test_only_editions_after_the_reviewed_one_are_probed():
    assert newer_editions(dt.datetime(2026, 1, 2, tzinfo=dt.UTC)) == [2027]
    assert newer_editions(dt.datetime(2026, 10, 23, tzinfo=dt.UTC)) == [2027]
    assert newer_editions(dt.datetime(2028, 6, 2, tzinfo=dt.UTC)) == [2027, 2028, 2029]
    assert newer_editions(dt.datetime(2031, 6, 2, tzinfo=dt.UTC)) == [2027, 2028, 2029]


def test_the_manifest_credit_and_notice_name_the_reviewed_edition(registry):
    """Moving KNOWN_EDITION without the manifest would publish a new edition under the old notice."""
    source = registry.get(SOURCE_ID)
    assert f"({KNOWN_EDITION} edition)" in str(source.attribution)
    assert f"Copyright (c) {KNOWN_EDITION}," in str(source.notice)
    assert EDITION_PAGE.format(year=KNOWN_EDITION) in str(source.license)


class _Answer:
    def __init__(self, status: int, content: bytes = b"", headers: dict[str, str] | None = None) -> None:
        self.status_code = status
        self.content = content
        self.text = content.decode("utf-8", "replace")
        self.headers = headers or {}


class _Http:
    def __init__(self, answers: dict[str, list[_Answer]]) -> None:
        self.answers = answers
        self.calls: list[tuple[str, dict[str, str]]] = []

    def get(self, url: str, **kwargs: object) -> _Answer:
        headers = kwargs.get("headers") or {}
        self.calls.append((url, dict(headers)))  # type: ignore[call-overload]
        return self.answers[url].pop(0)


def _connector(http: _Http):
    c = _lbnl(http=http)
    c.clock = lambda: dt.datetime(2026, 10, 23, 12, tzinfo=dt.UTC)
    return c


def test_fetch_finds_the_edition_checks_the_licence_and_keeps_the_validators():
    book = fixture_path(WORKBOOK).read_bytes()
    http = _Http(
        {
            EDITION_PAGE.format(year=2027): [_Answer(404, b"Page not found")],
            PAGE: [_Answer(200, PAGE_HTML.encode())],
            DATA_URL: [
                _Answer(
                    200, book, {"ETag": '"69fcd4cb-ed9924"', "Last-Modified": "Thu, 07 May 2026 18:07:07 GMT"}
                )
            ],
        }
    )
    raw = _connector(http).fetch()
    assert [u for u, _ in http.calls] == [EDITION_PAGE.format(year=2027), PAGE, DATA_URL]
    assert raw.url == DATA_URL and raw.content == book and raw.requests_made == 3
    assert raw.meta["edition"] == 2026 and raw.meta["etag"] == '"69fcd4cb-ed9924"'
    assert raw.meta["licence_checked"] == list(LICENCE_SENTENCES)
    assert raw.meta["notes_sha256"] == NOTES_SHA256


def test_a_newer_edition_blocks_the_run_until_a_reviewer_records_it():
    """Review finding 5, 2026-10-23: the next edition used to be fetched and published under the
    2026 edition's credit, notice and licence id, with nobody having read its page."""
    page_2027 = PAGE_HTML.replace("2026", "2027").encode()
    http = _Http({EDITION_PAGE.format(year=2027): [_Answer(200, page_2027)]})
    with pytest.raises(BlockedError, match="2027 edition is published"):
        _connector(http).fetch()
    assert [u for u, _ in http.calls] == [EDITION_PAGE.format(year=2027)]


def test_fetch_is_blocked_when_the_notes_block_changed():
    added = PAGE_HTML.replace(
        "All rights reserved.</p>",
        "All rights reserved.</p><p>Commercial use requires a GridTracker licence.</p>",
    )
    http = _Http({EDITION_PAGE.format(year=2027): [_Answer(404)], PAGE: [_Answer(200, added.encode())]})
    with pytest.raises(BlockedError, match="Notes block"):
        _connector(http).fetch()
    assert all(u != DATA_URL for u, _ in http.calls)


def test_fetch_is_blocked_when_the_licence_sentences_are_gone():
    http = _Http(
        {
            EDITION_PAGE.format(year=2027): [_Answer(404)],
            PAGE: [_Answer(200, PAGE_HTML.replace("licensed", "available").encode())],
        }
    )
    with pytest.raises(BlockedError, match="licence statement changed"):
        _connector(http).fetch()
    assert all(u != DATA_URL for u, _ in http.calls)  # the data file is never asked for


def test_fetch_reuses_the_stored_workbook_on_a_304():
    book = fixture_path(WORKBOOK).read_bytes()
    record = {
        "id": "run-1",
        "snapshot": {
            "sha256": hashlib.sha256(book).hexdigest(),
            "fetched_url": DATA_URL,
            "meta": {"data_url": DATA_URL, "etag": '"e1"', "last_modified": "Thu, 07 May 2026 18:07:07 GMT"},
        },
    }
    http = _Http(
        {
            EDITION_PAGE.format(year=2027): [_Answer(404)],
            PAGE: [_Answer(200, PAGE_HTML.encode())],
            DATA_URL: [_Answer(304)],
        }
    )
    c = _connector(http)
    c.previous = PreviousSnapshot(record=record, load=lambda: book)
    raw = c.fetch()
    assert http.calls[-1] == (
        DATA_URL,
        {"If-None-Match": '"e1"', "If-Modified-Since": "Thu, 07 May 2026 18:07:07 GMT"},
    )
    assert raw.content == book and raw.meta["not_modified"] is True and raw.meta["etag"] == '"e1"'


def test_fetch_fails_when_the_reviewed_edition_page_is_gone():
    http = _Http({EDITION_PAGE.format(year=2027): [_Answer(404)], PAGE: [_Answer(404)]})
    with pytest.raises(ConnectorError, match="queued-2026-edition-characteristics -> HTTP 404"):
        _connector(http).fetch()
