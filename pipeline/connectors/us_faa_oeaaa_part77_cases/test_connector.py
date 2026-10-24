"""Tests for us.faa.oeaaa.part77_cases (FAA OE/AAA Part 77 cases at data-centre sites), never live.

Fixtures (`fixtures/`, README row in docs/08 "Built"): four of the FAA's regional archives as served on
2026-10-23 (data as of 2026-10-17), trimmed to 43 cases with the header and the two trailer lines kept
verbatim and the rows in the FAA's own dialect: a Virginia campus with its linked cranes, operator and
campus-code sites, an existing data centre's maintenance cranes, a reported date built, cases under
study, expired determinations, a crane beside a data centre on a battery site, a substation, two
transmission towers, an antenna, an airport's own project, a church and a cell tower with its
frequency JSON. Person columns are blank (`REPRESENTATIVE NAME`, `SIGNATURE CONTROL NUMBER`, and
`SPONSOR NAME` unless the connector proves an organisation); the tests that drop a person write
made-up names into those cells in memory. `oe3a_home.html` is the OE3A home page's notice block;
`furlough_flag.json` is the answer of the flag the page's script reads to show it, as served on
2026-10-24 (off). `loudoun_pwc_campuses.csv`: the 77 AEA cases (2025 and 2026 archives, read
2026-10-24) that the first site rule put into four records holding several campuses each; person
columns and FREQUENCY_JSON blank, the sponsor kept only as the organisation the current rules
prove, structure names and free text through the connector's own rules. `selection_sample_*`: the
hand-labelled precision and recall samples (labels, and the stored form of each labelled case and
of the cases it cites, only the columns the selection rule reads). Every fetch runs against
`FakeFAA`, which serves these files; nothing touches the network.
"""

from __future__ import annotations

import collections
import csv
import dataclasses
import datetime as dt
import io
import json
import math
import pathlib
from typing import Any

import pandas as pd
import pytest

from conftest import connector_for
from pipeline.connectors.base import RawSnapshot
from pipeline.connectors.http import PoliteSession
from pipeline.connectors.registry import Registry
from pipeline.connectors.runner import run
from pipeline.connectors.store import Store
from pipeline.connectors.us_faa_oeaaa_part77_cases import connector as m

SOURCE_ID = "us.faa.oeaaa.part77_cases"
FIXTURES = pathlib.Path(__file__).with_name("fixtures")
ARCHIVES = {
    p.stem.removeprefix("Part77").removesuffix("List"): p.read_bytes() for p in FIXTURES.glob("Part77*.csv")
}
HOME = (FIXTURES / "oe3a_home.html").read_bytes()
FLAG_OFF = (FIXTURES / "furlough_flag.json").read_bytes()
FLAG_ON = FLAG_OFF.replace(b"false", b"true")
NOW = dt.datetime(2026, 10, 23, 21, 0, tzinfo=dt.UTC)
#: Made-up people (no such persons are known to the fixtures): the names the redaction must drop.
PERSON = "Alan Quillfeather"
REPRESENTATIVE = "Dorothy van Quillfeather"
SIGNATURE = "700000001-700000002"


class FakeResponse:
    def __init__(self, status: int, body: bytes, ctype: str = "") -> None:
        self.status_code = status
        self.content = body
        self.text = body.decode("utf-8", "replace")
        self.headers = {"Content-Type": ctype} if ctype else {}

    def iter_content(self, chunk_size: int) -> Any:
        for i in range(0, len(self.content), chunk_size):
            yield self.content[i : i + chunk_size]

    def close(self) -> None:
        pass


class FakeFAA:
    """oeaaa.faa.gov as measured: robots.txt 404, archives 200 (an unpublished one empty), no
    validators, the furlough flag off (2026-10-24)."""

    def __init__(
        self,
        archives: dict[str, bytes] | None = None,
        home: bytes | None = HOME,
        flag: bytes | None = FLAG_OFF,
    ) -> None:
        self.archives = dict(ARCHIVES if archives is None else archives)
        self.home = home
        self.flag = flag
        self.headers: dict[str, str] = {}
        self.calls: list[str] = []

    def request(self, method: str, url: str, **kw: Any) -> FakeResponse:
        self.calls.append(url)
        if url.endswith("/robots.txt"):
            return FakeResponse(404, b"<html>not found</html>", "text/html")
        if "fname=Part77" in url:
            name = url.split("fname=Part77")[1].removesuffix("List.gzip")
            body = self.archives.get(name, b"")
            return FakeResponse(200, body, "text/csv;charset=UTF-8" if body else "")
        if url == m.FURLOUGH_FLAG_URL:
            if self.flag is None:
                return FakeResponse(503, b"", "text/html")
            return FakeResponse(200, self.flag, "text/plain;charset=ISO-8859-1")
        if url == m.HOME_URL:
            if self.home is None:
                return FakeResponse(503, b"", "text/html")
            return FakeResponse(200, self.home, "text/html;charset=UTF-8")
        raise AssertionError(f"unexpected request {url}")

    def get(self, url: str, **kw: Any) -> FakeResponse:
        return self.request("GET", url, **kw)


def _connector(faa: FakeFAA, now: dt.datetime = NOW) -> m.Connector:
    c = connector_for(SOURCE_ID, http=PoliteSession(session=faa, sleep=lambda _s: None))  # type: ignore[arg-type]
    c.clock = lambda: now
    return c  # type: ignore[no-any-return]


def _parsed(
    faa: FakeFAA | None = None, now: dt.datetime = NOW
) -> tuple[RawSnapshot, list[dict[str, Any]], pd.DataFrame]:
    c = _connector(faa or FakeFAA(), now)
    raw = c.fetch()
    rows = c.parse(raw)
    return raw, rows, c.normalize(rows, raw)


@pytest.fixture(scope="module")
def parsed() -> tuple[RawSnapshot, list[dict[str, Any]], pd.DataFrame]:
    return _parsed()


def _site(df: pd.DataFrame, key: str) -> pd.Series:
    return df[df["source_record_id"] == key].iloc[0]


def _with_people(name: str) -> bytes:
    """An archive with made-up people in its person columns and one free-text field."""
    text = ARCHIVES[name].decode("utf-8")
    rows = list(csv.reader(io.StringIO(text, newline=""), escapechar="\\"))
    header = [h.strip() for h in rows[0]]
    rep, sponsor, sig = (
        header.index(c) for c in ("REPRESENTATIVE NAME", "SPONSOR NAME", "SIGNATURE CONTROL NUMBER")
    )
    desc = header.index("PROPOSAL DESCRIPTION")
    buf = io.StringIO()
    w = csv.writer(buf, escapechar="\\", doublequote=False, lineterminator="\n")
    lines = text.splitlines()
    for rec in rows[1:]:
        if not rec or not rec[0].startswith("20"):
            continue
        rec[rep], rec[sig] = REPRESENTATIVE, SIGNATURE
        if not rec[sponsor]:
            rec[sponsor] = PERSON
        rec[desc] = f"{rec[desc]} Contact {PERSON} at aq@example.com or (555) 201-4567."
        w.writerow(rec)
    return (lines[0] + "\n" + buf.getvalue() + "\n".join(lines[-2:]) + "\n").encode("utf-8")


# ------------------------------------------------------------------ archives
def test_the_archive_dialect_is_read_as_the_faa_writes_it() -> None:
    fh = io.TextIOWrapper(io.BytesIO(ARCHIVES["AEA2026"]), encoding="utf-8", newline="")
    header, rows, info = m.read_archive(fh)
    rows = list(rows)
    assert len(header) == 44 and header[-4:] == [
        "REPRESENTATIVE NAME",
        "SPONSOR NAME",
        "SIGNATURE CONTROL NUMBER",
        "FREQUENCY_JSON",
    ]
    assert len(rows) == info["rows"] == 22
    assert info["data_as_of"] == "2026-10-17T01:32"
    tower = next(r for r in rows if r[m.ASN] == "2026-AEA-9-OE")  # backslash-escaped quotes in the JSON
    assert json.loads(tower["FREQUENCY_JSON"])["frequencies"][0]["band"]["uom"] == "MHz"
    assert tower[m.NOTICE] == "Existing" and tower[m.TYPE].startswith("TOWER")


def test_an_archive_without_its_key_columns_is_refused() -> None:
    with pytest.raises(m.ParseError):
        m.read_archive(io.StringIO("STUDY (ASN),STATUS\n2026-AEA-1-OE,Studying\n"))


def test_the_trailer_timestamp_and_asn_order() -> None:
    assert m.data_as_of("Data as of 10/17/2026 01:15 AM") == "2026-10-17T01:15"
    assert m.data_as_of("Data as of 1/2/2026 3:05 PM") == "2026-01-02T15:05"
    assert m.data_as_of('""') is None
    assert sorted(["2026-AEA-10-OE", "2025-ASW-9-OE", "2026-AEA-9-OE"], key=m.asn_key) == [
        "2025-ASW-9-OE",
        "2026-AEA-9-OE",
        "2026-AEA-10-OE",
    ]


# ------------------------------------------------------------------ fetch
def test_fetch_reads_two_years_of_archives_politely_and_keeps_only_candidates() -> None:
    faa = FakeFAA()
    c = _connector(faa)
    raw = c.fetch()
    doc = json.loads(raw.content)
    archive_calls = [u for u in faa.calls if "fname=" in u]
    assert len(archive_calls) == 18 and len(set(archive_calls)) == 18  # 9 regions x 2025, 2026
    assert archive_calls[0] == m.archive_url("AAL", 2025) and archive_calls[-1] == m.archive_url("AWP", 2026)
    assert raw.requests_made == 20  # robots.txt, 18 archives, the furlough flag (off: no home page)
    assert m.HOME_URL not in faa.calls
    # the session a scheduled run builds paces the host at the manifest's 0.1 rps (one request per 10 s)
    assert connector_for(SOURCE_ID).http.rate_limits == {"oeaaa.faa.gov": 0.1}
    assert raw.meta["years"] == [2025, 2026]
    assert raw.meta["archives_published"] == 4 and len(raw.meta["archives_unpublished"]) == 14
    assert raw.meta["rows_total"] == 43 and raw.meta["candidates_kept"] == 40
    assert doc["columns"] == list(m.SNAPSHOT_COLUMNS) and doc["evaluated_on"] == "2026-10-23"
    kept = {r[0] for r in doc["cases"]}
    # a church, a cell tower and an airport's own project name no data centre: never stored
    assert not kept & {"2026-AEA-1121-OE", "2026-AEA-9-OE", "2025-AEA-4344-NRA"}
    # the cranes that cite the campus's building study are kept with it
    assert {"2026-AEA-10906-OE", "2026-AEA-10916-OE"} <= kept


def test_the_shutdown_notice_is_a_health_note_only_while_the_site_shows_it() -> None:
    on = FakeFAA(flag=FLAG_ON)
    raw = _connector(on).fetch()
    assert raw.meta["site_notice"].startswith(
        "Due to the government shutdown the Federal Aviation Administration"
    )
    assert "presumed to be a hazard to air navigation" in raw.meta["site_notice"]
    assert m.HOME_URL in on.calls and raw.requests_made == 21
    # the banner stays in the page's template after a shutdown: with the flag off it is no notice
    off = FakeFAA()
    assert _connector(off).fetch().meta["site_notice"] is None and m.HOME_URL not in off.calls
    # the flag on and the page unreadable: still a note, saying what is known
    assert _connector(FakeFAA(flag=FLAG_ON, home=None)).fetch().meta["site_notice"] == (
        "OE3A furlough banner on (its text was not read)"
    )
    quiet = _connector(FakeFAA(flag=None)).fetch()  # the flag unreadable: no note, never a failure
    assert quiet.meta["site_notice"] is None and len(json.loads(quiet.content)["cases"]) == 40


def test_an_html_answer_for_an_archive_fails_the_run(tmp_path: pathlib.Path) -> None:
    class HtmlFAA(FakeFAA):
        def request(self, method: str, url: str, **kw: Any) -> FakeResponse:
            if "fname=Part77AEA2026" in url:
                return FakeResponse(200, b"<html>maintenance</html>", "text/html")
            return super().request(method, url, **kw)

    http = PoliteSession(session=HtmlFAA(), sleep=lambda _s: None)  # type: ignore[arg-type]
    result = run(SOURCE_ID, registry=Registry(), store=Store(tmp_path), http=http, now=NOW)
    assert result.run["status"] == "failed"


# ------------------------------------------------------------------ personal data
def test_no_person_name_reaches_the_snapshot_or_any_record() -> None:
    faa = FakeFAA({name: _with_people(name) for name in ARCHIVES})
    raw, rows, df = _parsed(faa)
    for blob in (raw.content.decode("utf-8"), df.to_json(), json.dumps(rows), json.dumps(raw.meta)):
        for secret in (PERSON, "Quillfeather", SIGNATURE, "aq@example.com", "201-4567"):
            assert secret not in blob
    doc = json.loads(raw.content)
    assert not set(m.PERSON_COLUMNS) & set(doc["columns"])
    assert df["sponsor_name"].isna().all()
    # the made-up sponsor stood in for a blank one only; organisations survive
    orgs = {o for r in rows for o in r["sponsor_organisations"]}
    assert "The BlackChamber Group" in orgs and "H5 DATA CENTERS" in orgs


def test_a_sponsor_is_kept_only_when_the_name_rules_prove_an_organisation() -> None:
    assert m.organisation_name(PERSON) is None
    assert m.organisation_name("Quillfeather, Alan") is None
    assert m.organisation_name("Acme Builders LLC / Alan Quillfeather") is None
    assert m.organisation_name("AMAZON") is None  # no person shape, but no proof either
    assert m.organisation_name("QTS DC 1") == "QTS DC 1"
    assert m.organisation_name("Turner  Construction Company") == "Turner Construction Company"
    assert m.organisation_name("EE Reed East Coast - SAGE") == "EE Reed East Coast - SAGE"
    assert m.organisation_name("") is None and m.organisation_name(None) is None


@pytest.mark.parametrize(
    "sponsor",
    [
        f"Acme LLC ({PERSON})",  # brackets: "Acme LLC" alone passed classify, so the whole did
        f"Quillfeather Enterprises L.L.C. (Alan Q. {PERSON.split()[1]})",  # the live 2026-AEA-922-OE shape
        f"Acme Inc {PERSON}",
        f"{PERSON} (Acme Inc)",
        f"{PERSON} Consulting",  # a business named after its owner names a person
        f"Acme LLC [{PERSON}]",
        f"ACME BUILDERS INC {PERSON.upper()}",
    ],
)
def test_a_sponsor_with_a_person_anywhere_in_it_is_refused(sponsor: str) -> None:
    assert m.organisation_name(sponsor) is None
    row = dict.fromkeys(m.KEEP_COLUMNS, "") | {m.ASN: "2026-AEA-922-OE", m.SPONSOR: sponsor}
    cut = dict(zip(m.SNAPSHOT_COLUMNS, m.cut_row(row, "AEA2026"), strict=True))
    assert cut[m.SPONSOR_ORG] == "" and "Quillfeather" not in json.dumps(cut).title()


def test_a_bracketed_suffix_is_cut_and_what_is_left_must_still_be_proven() -> None:
    assert m.organisation_name("Acme Precast, Inc (Alan)") == "Acme Precast, Inc"  # a given name
    assert m.organisation_name("Turner Construction Company (CE)") == "Turner Construction Company"
    assert m.organisation_name("Amazon Data Services, Inc. (ADS)") == "Amazon Data Services, Inc."
    assert m.organisation_name("Quillfeather (CE)") is None  # only the bracket was structure
    assert m.organisation_name("(CE)") is None


def test_free_text_loses_contacts_and_person_names_but_keeps_operators() -> None:
    text = "Call Alan Quillfeather at (555) 201-4567 or aq@example.com; Cyrus One and Data Center stay."
    out = m.scrub_text(text)
    assert "Quillfeather" not in out and "201-4567" not in out and "@" not in out
    assert "Cyrus One" in out and "Data Center" in out
    assert m.scrub_text(None) == ""


def test_free_text_loses_titles_care_of_and_surname_first_but_keeps_places() -> None:
    for text in (
        "Deliveries c/o A. Quillfeather at the gate",
        "Questions to Mr. Quillfeather on site",
        "Site contact QUILLFEATHER, ALAN, superintendent",
        "Site contact Quillfeather, Alan",
    ):
        assert "Quillfeather" not in m.scrub_text(text).title(), text
    kept = "Crane at 22060 Glenn Drive Sterling Va near Santa Clara Ave and Fort Worth; Ashburn, Virginia"
    assert m.scrub_text(kept) == kept


@pytest.mark.parametrize(
    ("name", "stored"),
    [
        (f"Crane 1 - {PERSON}", "Crane 1"),
        (f"Data Center - {PERSON}", "Data Center"),
        (f"Mobile crane (contact {PERSON})", "Mobile crane"),
        (f"Crane 2, {PERSON}", "Crane 2"),
        (f"{PERSON} Crane", ""),
        (PERSON, ""),
        ("IAD 205 Crane Box", "IAD 205 Crane Box"),
    ],
)
def test_a_structure_name_never_keeps_a_person_in_it(name: str, stored: str) -> None:
    row = dict.fromkeys(m.KEEP_COLUMNS, "") | {m.ASN: "2026-AEA-1-OE", m.NAME: name, m.SPONSOR: PERSON}
    cut = dict(zip(m.SNAPSHOT_COLUMNS, m.cut_row(row, "AEA2026"), strict=True))
    assert cut[m.NAME] == stored and cut[m.SPONSOR_ORG] == "" and "Quillfeather" not in json.dumps(cut)
    case = {m.ASN: "2026-AEA-1-OE", m.ENTERED: "2026-01-01", m.NAME: name, m.TYPE: "CRANE"}
    assert "Quillfeather" not in m.site_name([case])


def test_a_reparsed_older_snapshot_never_reaches_a_record_with_a_person(tmp_path: pathlib.Path) -> None:
    """A snapshot written by an older fetch (one that let a bracketed person through) is put through
    the current rules again at parse."""
    c = _connector(FakeFAA())
    raw = c.fetch()
    doc = json.loads(raw.content)
    cols = doc["columns"]
    for rec in doc["cases"]:
        if rec[0] == "2026-AEA-2624-OE":
            rec[cols.index(m.SPONSOR_ORG)] = f"Acme LLC ({PERSON})"
            rec[cols.index(m.NAME)] = f"Data Center - {PERSON}"
    old = dataclasses.replace(raw, content=json.dumps(doc).encode("utf-8"), meta={})
    rows = c.parse(old)
    df = c.normalize(rows, old)
    assert "Quillfeather" not in json.dumps(rows) and "Quillfeather" not in df.to_json()


def test_redact_strips_a_person_column_a_snapshot_should_never_carry() -> None:
    doc = {"columns": ["STUDY (ASN)", "SPONSOR NAME"], "cases": [["2026-AEA-1-OE", PERSON]]}
    out = json.loads(_connector(FakeFAA()).redact(json.dumps(doc).encode()))
    assert out["columns"] == ["STUDY (ASN)"] and PERSON not in json.dumps(out)
    clean = json.dumps({"columns": ["STUDY (ASN)"], "cases": []}).encode()
    assert _connector(FakeFAA()).redact(clean) == clean


# ------------------------------------------------------------------ selection
def test_selection_bases_exclusions_and_neighbours(parsed: Any) -> None:
    raw, rows, _ = parsed
    assert raw.meta["cases_selected"] == 35 and raw.meta["sites"] == 15
    assert raw.meta["sites_inactive"] == 1  # 2025-ANM-67-OE: every determination expired
    assert raw.meta["selected_by_basis"] == {
        "campus_code": 3,
        "description": 14,
        "linked_case": 6,
        "location_description": 3,
        "operator": 6,
        "sponsor": 1,
        "structure_name": 2,
    }
    # two transmission towers and an antenna by type, a substation by its subject
    assert raw.meta["excluded"] == {"power_infrastructure": 1, "structure_type": 3}
    selected = {a for r in rows for a in r["asns"]}
    assert "2025-AEA-6811-OE" not in selected  # a battery site "beside a Data Center"
    assert "2025-AEA-2293-OE" not in selected  # "Concorde Substation Tower"
    assert not selected & {"2026-ASW-15931-OE", "2026-ASW-15932-OE", "2026-AEA-10995-OE"}


def test_the_rule_words() -> None:
    case = {m.ASN: "2026-AEA-1-OE", m.TYPE: "CRANE", m.DESCRIPTION: "", m.LOCATION: "", m.NAME: ""}
    assert m.direct_basis({**case, m.NAME: "QTS DC9 Data Center"}) == "structure_name"
    assert m.direct_basis({**case, m.DESCRIPTION: "Crane for a new hyperscale campus"}) == "description"
    assert m.direct_basis({**case, m.LOCATION: "The site is beside a Data Center"}) is None
    assert m.direct_basis({**case, m.LOCATION: "Data Center construction site"}) == "location_description"
    assert m.direct_basis({**case, m.SPONSOR_SIGNAL: "Y"}) == "sponsor"
    assert m.direct_basis({**case, m.NAME: "Yondr NOV2A r1"}) == "operator"
    assert m.direct_basis({**case, m.NAME: "T5 A bypass point"}) is None
    assert m.direct_basis({**case, m.NAME: "T5@Chicago III"}) == "operator"
    assert m.direct_basis({**case, m.NAME: "IAD 205 Crane Box"}) == "campus_code"
    assert m.direct_basis({**case, m.NAME: "IAD 19R approach light"}) is None
    assert m.direct_basis({**case, m.NAME: "DCA 10 Year Project"}) is None
    assert m.direct_basis({**case, m.ASN: "2026-AEA-1-NRA", m.NAME: "IAD 205 Crane Box"}) is None
    assert m.exclusion({**case, m.TYPE: "TRANSMISSION_LINE$T_L_TOWER"}) == "structure_type"
    assert m.exclusion({**case, m.TYPE: "POLE"}) == "structure_type"
    assert m.exclusion({**case, m.TYPE: "POLE$LIGHTING"}) is None  # a campus's own light pole
    assert m.exclusion({**case, m.NAME: "Rooftop antenna"}) == "telecom_structure"
    assert m.exclusion(
        {**case, m.DESCRIPTION: "SLC1 Substation is a new substation. It powers the data center."}
    ) == ("power_infrastructure")
    assert (
        m.exclusion(
            {**case, m.DESCRIPTION: "Two data center pads, a security building and a substation yard"}
        )
        is None
    )


def test_cited_asns_reads_prior_numbers_and_ranges() -> None:
    row = {
        m.ASN: "2026-AEA-10906-OE",
        m.PRIOR: "2025-AEA-1-OE",
        m.DESCRIPTION: "crane points for development project 2026-AEA-2624:2626-OE",
        m.LOCATION: "see 2024-AEA-13285-OE and 2025-ASW-1871-1876-OE",
    }
    assert m.cited_asns(row) == {
        "2025-AEA-1-OE",
        "2026-AEA-2624-OE",
        "2026-AEA-2625-OE",
        "2026-AEA-2626-OE",
        "2024-AEA-13285-OE",
        *(f"2025-ASW-{n}-OE" for n in range(1871, 1877)),
    }


# ------------------------------------------------------------------ sites
def test_a_campus_and_its_cranes_are_one_site(parsed: Any) -> None:
    _, rows, df = parsed
    campus = next(r for r in rows if r["site_key"] == "2026-AEA-2624-OE")
    assert campus["case_count"] == 9 and campus["bases"] == {"description": 3, "linked_case": 6}
    row = _site(df, "2026-AEA-2624-OE")
    assert row["name_canonical"] == "Tech Park at Dulles - Building A (Sterling data centre)"
    assert row["county"] == "Loudoun" and row["state"] == "VA"
    # Equinix DC17 (2025) and its DC-18 crane (2026) are one campus across two archive years
    assert set(json.loads(_site(df, "2025-AEA-14725-OE")["raw"])["asns"]) == {
        "2025-AEA-14725-OE",
        "2025-AEA-14727-OE",
        "2026-AEA-6396-OE",
    }
    assert df["record_id"].is_unique and len(df) == 15


def test_neighbouring_operators_stay_two_sites() -> None:
    base = {m.STATE: "VA", m.ENTERED: "2026-01-01", m.DESCRIPTION: "", m.LOCATION: "", m.SPONSOR_ORG: ""}
    equinix = {**base, m.ASN: "2026-AEA-1-OE", m.NAME: "Equinix DC-30", m.LAT: "39.0000", m.LON: "-77.4500"}
    crane = {**base, m.ASN: "2026-AEA-2-OE", m.NAME: "Crane 1", m.LAT: "39.0030", m.LON: "-77.4500"}
    ntt = {**base, m.ASN: "2026-AEA-3-OE", m.NAME: "NTT VA9", m.LAT: "39.0060", m.LON: "-77.4500"}
    keys = m.assign_sites([equinix, crane, ntt], {})
    assert keys["2026-AEA-1-OE"] != keys["2026-AEA-3-OE"]  # 0.67 km apart, two companies
    assert keys["2026-AEA-2-OE"] in (keys["2026-AEA-1-OE"], keys["2026-AEA-3-OE"])
    unnamed = [{**c, m.NAME: "Building"} for c in (equinix, crane, ntt)]
    assert len(set(m.assign_sites(unnamed, {}).values())) == 1  # one chain within 1 km
    far = {**crane, m.ASN: "2026-AEA-4-OE", m.LAT: "39.1000"}  # 11 km away
    assert len(set(m.assign_sites([equinix, far], {}).values())) == 2
    other_state = {**crane, m.ASN: "2026-AEA-5-OE", m.STATE: "MD"}
    assert len(set(m.assign_sites([equinix, other_state], {}).values())) == 2


def test_a_chain_never_grows_wider_than_the_cap() -> None:
    base = {m.STATE: "VA", m.ENTERED: "2026-01-01", m.DESCRIPTION: "", m.LOCATION: "", m.SPONSOR_ORG: ""}
    # five unnamed cases 0.6 km apart in a line: single linkage made one 2.4 km site of them
    line = [
        {
            **base,
            m.ASN: f"2026-AEA-{n}-OE",
            m.NAME: "Building",
            m.LAT: f"{39 + n * 0.0054:.4f}",
            m.LON: "-77.45",
        }
        for n in range(1, 6)
    ]
    groups = m.clusters(line)
    assert len(groups) > 1
    for g in groups:
        pts = [(float(line[i][m.LAT]), float(line[i][m.LON])) for i in g]
        assert max(m.km(*p, *q) for p in pts for q in pts) <= m.MAX_SITE_KM
    assert len(m.clusters(line, max_km=math.inf)) == 1


def test_campus_codes_and_companies_keep_neighbours_apart() -> None:
    compatible = m.identities_compatible
    code, amazon, dlr, yondr = (frozenset({k}) for k in (m.CAMPUS_CODE, "amazon", "digital_realty", "yondr"))
    assert compatible(code, frozenset()) and compatible(code, code) and compatible(code, amazon)
    assert compatible(code, dlr) and not compatible(code, yondr) and not compatible(yondr, code)
    assert not compatible(amazon | code, dlr)  # once a group names Amazon, a code no longer bridges to DLR
    assert compatible(amazon | code, code) and not compatible(amazon, dlr)
    assert m.site_identity({m.NAME: "Project IAD 170 crane"}) == code
    assert m.site_identity({m.NAME: "Yondr NOV2A r1"}) == yondr
    assert m.operators_named({m.NAME: "Cloud LC11 Mobile Crane"}) == {"cloudhq"}  # CloudHQ's own code
    assert m.operators_named({m.NAME: "BZI Crane (ECX CMH03)"}) == {"edgeconnex"}
    assert m.operators_named({m.NAME: "Aligned 03 Crane"}) == {"aligned"}
    assert m.operators_named({m.LOCATION: "1.1 miles from the airport according to Google Earth"}) == set()
    assert m.campus_codes({m.NAME: "IAD 457 Crawler Gens"}) == {"IAD457"}


def _archive_cases(path: pathlib.Path) -> list[dict[str, Any]]:
    fh = io.TextIOWrapper(io.BytesIO(path.read_bytes()), encoding="utf-8", newline="")
    _, rows, _ = m.read_archive(fh)
    return [dict(zip(m.SNAPSHOT_COLUMNS, m.cut_row(r, "AEA"), strict=True)) for r in rows]


def test_loudoun_and_prince_william_campuses_stay_apart() -> None:
    """The 77 cases the first rule (1 km nearest-first, operator guard only) put into four records:
    2025-AEA-556-OE (31 cases over 2.5 km: Yondr NOV2A/B, Project IAD 170, IAD 12-13 and
    IAD295-297), 2025-AEA-1979-OE (17: Iron Mountain VA8/VA9, Wellington Glen, IAD 193, BCG Rose),
    2025-AEA-2945-OE (19: QTS MAN1 published as "NVA05E") and 2025-AEA-4390-OE (10, a CloudHQ crane
    founding an Amazon-named site)."""
    cases = _archive_cases(FIXTURES / "loudoun_pwc_campuses.csv")
    assert len(cases) == 77
    basis, _ = m.select_cases(cases)
    sel = [c for c in cases if c[m.ASN] in basis]
    keys = m.assign_sites(sel, {})
    sites: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for c in sel:
        sites[keys[c[m.ASN]]].append(c)
    for members in sites.values():
        pts = [(float(c[m.LAT]), float(c[m.LON])) for c in members]
        assert max(m.km(*p, *q) for p in pts for q in pts) <= m.MAX_SITE_KM
        named = set().union(*(m.operators_named(c) for c in members))
        coded = any(m.campus_codes(c) for c in members)
        assert len(named) <= 1 and not (coded and named - m.CODE_NUMBERING_COMPANIES)

    def site_of(text: str) -> list[dict[str, Any]]:
        (key,) = {keys[c[m.ASN]] for c in sel if text in c[m.NAME]}
        return sites[key]

    yondr = site_of("Yondr NOV2A")
    assert not any(m.campus_codes(c) for c in yondr) and m.site_name(yondr) == "Yondr NOV2A r1"
    assert not any("IAD29" in c[m.NAME] for c in site_of("IAD 12-13"))
    iron = site_of("Iron Mountain VA8")
    assert m.site_name(iron) == "Iron Mountain VA8 and VA9" and not any("IAD 193" in c[m.NAME] for c in iron)
    qts = site_of("QTS MAN1")
    assert not any("NVA05E" in c[m.NAME] or m.campus_codes(c) for c in qts)
    assert {"cloudhq"} == set().union(*(m.operators_named(c) for c in site_of("Cloud LC11")))
    assert site_of("IAD 193 - A&A") != site_of("BCG Rose West")  # two addresses 1.2 km apart


def test_remembered_sites_never_merge_and_never_rekey() -> None:
    base = {m.STATE: "VA", m.DESCRIPTION: "", m.LOCATION: "", m.SPONSOR_ORG: "", m.NAME: "Building"}
    a = {**base, m.ASN: "2025-AEA-5-OE", m.ENTERED: "2025-01-01", m.LAT: "39.0000", m.LON: "-77.4500"}
    b = {**base, m.ASN: "2025-AEA-9-OE", m.ENTERED: "2025-02-01", m.LAT: "39.0100", m.LON: "-77.4500"}
    memory = m.assign_sites([a, b], {})
    assert memory == {"2025-AEA-5-OE": "2025-AEA-5-OE", "2025-AEA-9-OE": "2025-AEA-9-OE"}  # 1.1 km apart
    bridge = {**base, m.ASN: "2026-AEA-1-OE", m.ENTERED: "2026-01-01", m.LAT: "39.0055", m.LON: "-77.4500"}
    assert len(set(m.assign_sites([a, b, bridge], {}).values())) == 1  # fresh: one chain, 1.1 km wide
    keys = m.assign_sites([a, b, bridge], memory)
    assert keys["2025-AEA-5-OE"] == "2025-AEA-5-OE" and keys["2025-AEA-9-OE"] == "2025-AEA-9-OE"
    assert keys["2026-AEA-1-OE"] == "2025-AEA-9-OE"  # the nearer remembered case (0.50 km vs 0.61 km)
    # the founding case gone, its site keeps its key
    assert (
        m.assign_sites([b, bridge], {**memory, "2026-AEA-1-OE": "2025-AEA-5-OE"})["2026-AEA-1-OE"]
        == "2025-AEA-5-OE"
    )


def test_a_site_is_placed_at_its_permanent_structures_inside_its_state() -> None:
    def case(lat: str, duration: str = "Permanent", acc: str = "4D") -> dict[str, str]:
        return {m.LAT: lat, m.LON: "-77.45", m.DURATION: duration, m.ACCURACY: acc}

    where, lat, lon = m.placement([case("39.000"), case("39.002"), case("39.100", "Temporary")], "VA")
    assert (where, lat, lon) == ("exact", 39.001, -77.45)
    assert m.placement([case("39.0", acc="6D")], "VA")[0] == "accuracy_not_stated_or_coarse"
    assert m.placement([case("39.0")], "TX")[0] == "point_outside_state"
    assert m.placement([{m.LAT: "", m.LON: ""}], "VA")[0] == "no_coordinate"


def test_names() -> None:
    assert m.display_name("QTS DC9", "Phoenix", "AZ") == "QTS DC9"
    assert m.display_name("Building B", "MARYSVILLE", "OH") == "Building B (Marysville data centre)"
    assert m.display_name("Crane 1", "el paso,", "TX") == "Crane 1 (El Paso data centre)"
    assert m.display_name("", "", "TX") == "Data centre site, TX"
    named = [
        {m.ASN: "2026-AEA-2-OE", m.ENTERED: "2026-01-01", m.NAME: "Crane 1", m.TYPE: "CRANE"},
        {m.ASN: "2026-AEA-3-OE", m.ENTERED: "2026-02-01", m.NAME: "Building C", m.TYPE: "BUILDING"},
        {m.ASN: "2026-AEA-4-OE", m.ENTERED: "2026-03-01", m.NAME: PERSON, m.TYPE: "BUILDING"},
    ]
    assert m.site_name(named) == "Building C"
    assert m.site_name([*named, {**named[0], m.NAME: "Vantage VA15 crane"}]) == "Vantage VA15 crane"
    assert m.site_name([named[2]]) == ""


# ------------------------------------------------------------------ lifecycle and expiry
def test_states_follow_what_the_filings_prove(parsed: Any) -> None:
    _, _, df = parsed
    state = dict(zip(df["source_record_id"], df["lifecycle_state"], strict=True))
    raw = dict(zip(df["source_record_id"], df["status_raw"], strict=True))
    assert state["2026-AEA-2624-OE"] == "filed" and raw["2026-AEA-2624-OE"] == m.STATUS_DETERMINED
    assert state["2026-AEA-8625-OE"] == "filed" and raw["2026-AEA-8625-OE"] == m.STATUS_UNDER_STUDY
    assert state["2025-AEA-2266-OE"] == "under_construction"  # DATE BUILT on a permanent building
    assert raw["2025-AEA-2266-OE"] == m.STATUS_BUILT_REPORTED
    assert state["2026-AEA-3584-OE"] == "built"  # fan repairs at an existing Amazon data centre
    assert state["2025-AEA-6175-OE"] == "built"  # a dunnage platform on an existing data centre
    assert state["2025-ANM-878-OE"] == "built"  # a re-roof at an existing data centre, determination expired
    assert "2025-ANM-67-OE" not in state  # every determination expired: not a record (module docstring)
    assert set(df["status_rule"]) == {"faa_oeaaa_part77.map"}
    assert not (set(df["lifecycle_state"]) & {"unknown", "permitted", "contracted", "withdrawn", "cancelled"})


def test_expiry_follows_the_faa_date_then_eighteen_months() -> None:
    on = dt.date(2026, 10, 23)
    dnh = {m.STATUS: "Determined - No Hazard", m.COMPLETION: "2025-04-01", m.EXPIRATION: "2026-10-23"}
    assert m.case_active(dnh, on) and not m.case_active(dnh, on + dt.timedelta(days=1))
    no_date = {**dnh, m.EXPIRATION: ""}
    assert m.expires_on(no_date) == dt.date(2026, 10, 1)  # 14 CFR 77.33(b): 18 months
    assert m.expires_on({**no_date, m.COMPLETION: "2024-08-31"}) == dt.date(2026, 2, 28)
    assert m.expires_on({**no_date, m.COMPLETION: ""}) is None and m.case_active(
        {**no_date, m.COMPLETION: ""}, on
    )
    assert m.case_active({m.STATUS: "Studying"}, on) and m.case_active({m.STATUS: "Evaluating"}, on)
    assert not m.case_active({m.STATUS: "Determined - Hazard", m.EXPIRATION: ""}, on)  # 77.33(c)
    assert m.case_active({**dnh, m.STATUS: "Reminder"}, on)
    assert not m.case_active({m.STATUS: "Final", m.DETERMINATION: "M&L Denied"}, on)


def test_existing_work_and_new_work() -> None:
    def ew(desc: str, loc: str = "") -> bool:
        return m.existing_work({m.DESCRIPTION: desc, m.LOCATION: loc})

    assert ew("Use of temporary crane to remove and replace existing MUA units")
    assert ew("Re-roof. The construction materials are roofing materials.", "Jobsite is Sabey Data Center")
    assert not ew("Proposal for new building on existing campus")
    assert not ew(
        "Proposing to build a data center", "next to an existing structure that is also a data center"
    )
    assert not ew("Temporary construction crane. Existing building did not require filing")
    assert not ew("Mobile crane to erect a 2-story Data Center")


def test_site_status_precedence() -> None:
    on = dt.date(2026, 10, 23)
    active = {
        m.STATUS: "Determined - No Hazard",
        m.EXPIRATION: "2027-01-01",
        m.DESCRIPTION: "new data center",
    }
    expired = {**active, m.EXPIRATION: "2026-01-01"}
    built = {**expired, m.DATE_BUILT: "2026-02-01", m.DURATION: "Permanent"}
    upkeep = {**expired, m.DESCRIPTION: "replace existing chillers"}
    assert m.site_status([active], on) == m.STATUS_DETERMINED
    assert m.site_status([{**active, m.STATUS: "Studying"}], on) == m.STATUS_UNDER_STUDY
    assert m.site_status([built, active], on) == m.STATUS_BUILT_REPORTED
    assert m.site_status([{**built, m.DURATION: "Temporary"}], on) == m.STATUS_EXPIRED  # a crane's date built
    assert m.site_status([upkeep, active], on) == m.STATUS_DETERMINED  # new work still active wins
    assert m.site_status([upkeep, expired], on) == m.STATUS_EXISTING
    assert m.site_status([{**expired, m.STATUS: "Determined - Hazard"}], on) == m.STATUS_HAZARD
    assert m.site_status([expired], on) == m.STATUS_EXPIRED


def test_a_date_built_on_a_notice_of_an_existing_structure_proves_built() -> None:
    """On a notice of an existing structure the date built is the original build date, not a Form
    7460-2: the building stands (`built`), it did not just reach its height (`under_construction`)."""
    on = dt.date(2026, 10, 23)
    existing = {
        m.STATUS: "Determined - No Hazard",
        m.EXPIRATION: "2027-01-01",
        m.DESCRIPTION: "Data center building",
        m.DURATION: "Permanent",
        m.NOTICE: "Existing",
        m.DATE_BUILT: "2019-06-01",
    }
    assert not m.built_reported(existing) and m.existing_work(existing)
    assert m.site_status([existing], on) == m.STATUS_EXISTING
    assert m.built_reported({**existing, m.NOTICE: "New Construction"})
    # "Existing" ticked on a new building, with no date built, proves nothing more than a filing
    assert not m.existing_work(
        {**existing, m.DATE_BUILT: "", m.DESCRIPTION: "four new single-story buildings"}
    )


def test_a_crane_whose_work_window_passed_is_reported_and_stays_active_until_it_expires(parsed: Any) -> None:
    """The FAA's expiration date is when a determination stops being in force; the crane's window
    passing says the lift is done, not that the project stopped. Reported per case, not used."""
    on = dt.date(2026, 10, 23)
    crane = {
        m.STATUS: "Determined - No Hazard",
        m.EXPIRATION: "2027-04-21",
        m.DURATION: "Temporary",
        m.WORK_END: "2026-04-30",
    }
    assert m.work_ended(crane, on) and m.case_active(crane, on)
    assert not m.work_ended({**crane, m.DURATION: "Permanent"}, on)
    _, rows, _ = parsed
    cases = [c for r in rows for c in r["cases"]]
    assert all(isinstance(c["work_ended"], bool) for c in cases) and any(c["work_ended"] for c in cases)


def test_a_campus_generator_building_stays_a_power_plant_does_not() -> None:
    """A generator yard or a data centre's own energy centre is campus support and stays; a power
    project that names no data centre is left out."""
    case = {m.ASN: "2026-AWP-1-OE", m.TYPE: "BUILDING$COMMERCIAL", m.LOCATION: ""}
    peru = {**case, m.NAME: "Peru Ridge Generator 3", m.DESCRIPTION: "New data center with 7 buildings"}
    assert m.exclusion(peru) is None and m.direct_basis(peru) == "description"
    energy = {
        **case,
        m.NAME: "South Site Data Center Energy Center",
        m.DESCRIPTION: "crane for energy center",
    }
    assert m.exclusion(energy) is None
    plant = {
        **case,
        m.NAME: "Peaker Power Plant",
        m.DESCRIPTION: "gas power plant serving a data center campus",
    }
    assert m.exclusion(plant) == "power_infrastructure"


def test_the_selection_rule_on_the_hand_labelled_samples() -> None:
    """Precision: 45 selected cases drawn at random from the 2026-10-24 read and judged by hand.
    Recall: 60 cases drawn within 0.5 km of a held data-centre point (2026-10-23). The labels and
    the cases are fixtures (`selection_sample_*`), so the figures in docs/08 can be re-checked."""
    with open(FIXTURES / "selection_sample_labels.csv", encoding="utf-8", newline="") as fh:
        labels = list(csv.DictReader(fh))
    doc = json.loads((FIXTURES / "selection_sample_cases.json").read_text(encoding="utf-8"))
    cases = [
        {**dict.fromkeys(m.SNAPSHOT_COLUMNS, ""), **dict(zip(doc["columns"], r, strict=True))}
        for r in doc["cases"]
    ]
    basis, _ = m.select_cases(cases)
    by_sample: dict[tuple[str, str], list[str]] = collections.defaultdict(list)
    for row in labels:
        by_sample[(row["sample"], row["label"])].append(row["asn"])
    precision = by_sample[("precision", "dc")]
    assert len(precision) == 45 and not by_sample[("precision", "not_dc")]
    assert all(a in basis for a in precision)  # 45 of 45 selected cases are data-centre structures
    dc, not_dc = by_sample[("recall", "dc")], by_sample[("recall", "not_dc")]
    assert (len(dc), len(not_dc), len(by_sample[("recall", "unsure")])) == (34, 24, 2)
    assert sum(a in basis for a in dc) == 21  # case recall 21 of 34 (sites: 32 of 34, docs/08)
    assert not any(a in basis for a in not_dc)  # 0 of 24 non-data-centre cases selected


def test_every_emitted_label_is_mapped_and_restated_and_no_label_maps_to_unknown() -> None:
    c = _connector(FakeFAA())
    labels = [m.STATUS_BUILT_REPORTED, m.STATUS_DETERMINED, m.STATUS_UNDER_STUDY, m.STATUS_EXISTING]
    mapping = c.status_map["sources"]["faa_oeaaa_part77"]["map"]
    assert set(mapping) == set(labels) and "unknown" not in set(mapping.values())
    assert not set(mapping) & m.INACTIVE_STATUSES  # never emitted, so deliberately unlisted
    frame = pd.DataFrame({"status_raw": [*labels, m.STATUS_EXPIRED]})
    restated = c.restate_status(frame)
    assert restated is not None and list(restated["lifecycle_state"]) == [
        "under_construction",
        "filed",
        "filed",
        "built",
        "unknown",  # should one ever be stored: unmapped, so the DQ vocabulary check warns
    ]
    assert restated["status_rule"].iloc[-1] == "faa_oeaaa_part77.unmapped"


# ------------------------------------------------------------------ records
def test_records_are_load_proposals_with_provenance(parsed: Any) -> None:
    c = _connector(FakeFAA())
    _, _, df = parsed
    assert list(df.columns) == c.columns
    assert set(df["kind"]) == {"load"} and set(df["technology"]) == {"load"}
    assert df["capacity_mw"].isna().all() and df["sponsor_name"].isna().all()
    for col in ("source_id", "source_url", "retrieved_at", "licence_id", "raw"):
        assert df[col].notna().all(), col
    assert set(df["licence_id"]) == {c.source.licence_id}
    assert df["source_url"].str.startswith(m.ARCHIVE_URL.split("Part77")[0]).all()
    assert _site(df, "2026-AEA-2624-OE")["source_url"] == m.archive_url("AEA", 2026)
    assert _site(df, "2025-AEA-2266-OE")["queue_date"] == pd.Timestamp("2025-02-20")  # earliest received
    payload = json.loads(_site(df, "2026-AEA-2624-OE")["raw"])
    assert payload["placement"] == "exact" and payload["Latitude"] and payload["Longitude"]
    assert payload["select_basis"] == "description" and payload["evaluated_on"] == "2026-10-23"
    assert all("PROPOSAL DESCRIPTION" not in case for case in payload["cases"])
    assert set(df["county"]) >= {"Loudoun", "Prince William", "Fairfax", "Taylor", "Denton"}


def test_a_run_loads_cleanly_through_the_runner(tmp_path: pathlib.Path) -> None:
    http = PoliteSession(session=FakeFAA(), sleep=lambda _s: None)  # type: ignore[arg-type]
    result = run(SOURCE_ID, registry=Registry(), store=Store(tmp_path), http=http, now=NOW)
    assert result.run["status"] == "ok" and result.run["dq_status"] == "pass"
    assert result.run["rows_seen"] == 15 and result.run["publishable"] is True
    assert result.run["snapshot"]["meta"]["source_header"][0] == "STUDY (ASN)"


# ------------------------------------------------------------------ repeat runs
def test_the_same_archives_next_day_are_unchanged_and_an_expiry_is_not(tmp_path: pathlib.Path) -> None:
    store, registry = Store(tmp_path), Registry()

    def go(now: dt.datetime) -> Any:
        http = PoliteSession(session=FakeFAA(), sleep=lambda _s: None)  # type: ignore[arg-type]
        return run(SOURCE_ID, registry=registry, store=store, http=http, now=now)

    assert go(NOW).run["status"] == "ok"
    second = go(NOW + dt.timedelta(days=1))  # site memory is written: the bytes differ once
    assert second.run["status"] == "ok" and second.run["events_emitted"] == 0
    assert go(NOW + dt.timedelta(days=2)).run["status"] == "unchanged"
    # 2025-AEA-6175/6176's determinations expire on 2026-12-30: the next day the sites are read again
    assert go(dt.datetime(2026, 12, 30, 21, 0, tzinfo=dt.UTC)).run["status"] == "unchanged"
    later = go(dt.datetime(2026, 12, 31, 21, 0, tzinfo=dt.UTC))
    assert later.run["status"] == "ok" and later.run["events_emitted"] == 0  # still an existing data centre


def test_a_site_whose_last_determination_expires_is_removed_not_withdrawn(tmp_path: pathlib.Path) -> None:
    store, registry = Store(tmp_path), Registry()

    def go(now: dt.datetime) -> Any:
        http = PoliteSession(session=FakeFAA(), sleep=lambda _s: None)  # type: ignore[arg-type]
        return run(SOURCE_ID, registry=registry, store=store, http=http, now=now)

    key = "2025-AEA-6892-OE"  # one building, Determination of No Hazard, expires 2027-03-10
    first = go(NOW)
    assert key in set(first.records["source_record_id"])
    later = go(dt.datetime(2027, 3, 11, 21, 0, tzinfo=dt.UTC))  # its case is carried, now expired
    assert later.run["status"] == "ok" and key not in set(later.records["source_record_id"])
    assert later.run["snapshot"]["meta"]["sites_inactive"] >= 1
    gone = later.events[later.events["record_id"] == f"{SOURCE_ID}:{key}"]
    assert list(gone["event_type"].astype(str)) == ["removed"] and gone["before"].iloc[0] == "filed"


def test_a_new_year_keeps_site_keys_and_carries_cases_still_active(tmp_path: pathlib.Path) -> None:
    store, registry = Store(tmp_path), Registry()
    january = dt.datetime(2027, 1, 11, 21, 0, tzinfo=dt.UTC)

    def go(now: dt.datetime) -> Any:
        http = PoliteSession(session=FakeFAA(), sleep=lambda _s: None)  # type: ignore[arg-type]
        return run(SOURCE_ID, registry=registry, store=store, http=http, now=now)

    first = go(NOW)
    before = set(first.records["source_record_id"])
    after_run = go(january)  # window 2026-2027: the 2025 archives are no longer fetched
    assert after_run.run["status"] == "ok"
    meta = after_run.run["snapshot"]["meta"]
    assert meta["years"] == [2026, 2027] and meta["cases_carried"] > 0
    after = set(after_run.records["source_record_id"])
    # every site whose cases can still matter keeps its key; nothing is re-keyed
    assert after <= before
    assert {"2025-AEA-14725-OE", "2025-AEA-2266-OE", "2025-AEA-6175-OE"} <= after
    # an expired site is never a record, and its cases leave after the grace period
    assert "2025-ANM-67-OE" not in before | after
    assert "2025-ANM-67-OE" not in {k for c in after_run.records["raw"] for k in json.loads(c)["asns"]}
    equinix = json.loads(after_run.records.set_index("source_record_id").loc["2025-AEA-14725-OE", "raw"])
    assert "2026-AEA-6396-OE" in equinix["asns"]


def test_an_archive_that_comes_back_empty_keeps_its_cases(tmp_path: pathlib.Path) -> None:
    store, registry = Store(tmp_path), Registry()
    full = PoliteSession(session=FakeFAA(), sleep=lambda _s: None)  # type: ignore[arg-type]
    first = run(SOURCE_ID, registry=registry, store=store, http=full, now=NOW)
    partial = {k: v for k, v in ARCHIVES.items() if k != "ASW2026"}
    http = PoliteSession(session=FakeFAA(partial), sleep=lambda _s: None)  # type: ignore[arg-type]
    second = run(SOURCE_ID, registry=registry, store=store, http=http, now=NOW + dt.timedelta(days=7))
    assert second.run["status"] in ("ok", "unchanged")
    assert "ASW2026" in second.run["snapshot"]["meta"]["archives_unpublished"]
    assert set(second.records["source_record_id"]) == set(first.records["source_record_id"])


def test_the_canonical_form_ignores_the_date_but_not_an_expiry() -> None:
    c = _connector(FakeFAA())
    doc = json.loads(c.fetch().content)
    same = c.canonical_content(json.dumps({**doc, "evaluated_on": "2026-10-24"}).encode())
    assert same == c.canonical_content(json.dumps(doc).encode())
    moved = c.canonical_content(json.dumps({**doc, "evaluated_on": "2027-06-01"}).encode())
    assert moved != same
    assert c.canonical_content(b"not json") == b"not json"


# ------------------------------------------------------------------ registry
def test_the_manifest_row_registers_the_connector() -> None:
    registry = Registry()
    src = registry.get(SOURCE_ID)
    assert src.implemented and not src.gated and src.reuse == "open" and src.publication == "raw_ok"
    assert src.max_rps == 0.1 and src.host == "oeaaa.faa.gov"
    assert registry.connector_class(SOURCE_ID) is m.Connector


def test_the_source_stays_out_of_the_schedule_until_the_resolver_compares_places(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`pipeline/resolve.py` has no location veto yet, and a replay of this source's records against
    the held loads accepted fuzzy merges of sites 8 to 32 km apart (docs/08 §4.1 "Built"). The
    scheduler fetches every manifest row with a `cadence` (`infra/scheduler/app.py`
    `_load_sources`), so the row carries none until the veto lands: no tick and no deploy bootstrap
    fetches it. An operator can still run it by hand to measure."""
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:pw@localhost:5432/dummy")
    import infra.scheduler.app as scheduler_app
    from infra.scheduler.bootstrap import fetchable_sources
    from infra.scheduler.cadence import CRON_BY_BUCKET

    row = next(s for s in Registry().sources.values() if s.id == SOURCE_ID).raw
    assert "cadence" not in row and "poll" not in row
    assert "location veto" in row["notes"]
    monday = int(dt.datetime(2026, 10, 26, 4, 13, tzinfo=dt.UTC).timestamp())
    for bucket in CRON_BY_BUCKET:
        assert SOURCE_ID not in {s["id"] for s in scheduler_app.due_sources(bucket, monday)}, bucket
    scheduled = fetchable_sources(scheduler_app._load_sources(), Registry().status())
    assert SOURCE_ID not in {str(s["id"]) for s in scheduled}
