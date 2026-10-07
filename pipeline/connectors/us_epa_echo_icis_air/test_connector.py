"""Parser tests for us.epa.echo.icis_air (EPA ICIS-Air data-centre selection, placed through FRS).

Fixture `tests/fixtures/epa_icis_air_data_centers.json` is the connector's own snapshot of 2026-09-29
(ICIS-AIR_FACILITIES.csv pre-filtered to 632 candidates, ECHO Exporter rows for their FRS ids)
trimmed to 35 facilities and their 34 FRS rows: every selection basis, every lifecycle value, every
placement outcome, the rows each exclusion exists for, and seven Virginia facilities that are also
in the Virginia DEQ fixture. No value is edited. No network: the fetch tests at the end run the
connector against `FakeEcho`, two zips built from the same rows.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import json
import re
import zipfile
import zlib
from email.utils import formatdate
from typing import Any

import pandas as pd
import pytest

from conftest import connector_for, fixture_path, snapshot
from pipeline import resolve
from pipeline.connectors.base import ParseError
from pipeline.connectors.http import PoliteSession
from pipeline.connectors.runner import run
from pipeline.connectors.store import Store
from pipeline.connectors.us_epa_echo_icis_air import connector as connector_module
from pipeline.connectors.us_epa_echo_icis_air.connector import (
    DFR_URL,
    EXPORTER_MEMBER,
    EXPORTER_URL,
    FETCH_VERSION,
    ICIS_MEMBER,
    ICIS_URL,
    central_directory,
    conditional_headers,
    inflate_member,
    is_candidate,
    placement_for,
    select_basis,
)
from pipeline.connectors.us_va_deq_data_center_air_sites.connector import LAYER_URL as VA_LAYER_URL

SOURCE_ID = "us.epa.echo.icis_air"
FIXTURE = "epa_icis_air_data_centers.json"


def _run() -> tuple[object, list[dict[str, object]], pd.DataFrame]:
    c = connector_for(SOURCE_ID)
    raw = snapshot(FIXTURE, ICIS_URL, "application/json")
    rows = c.parse(raw)
    return c, rows, c.normalize(rows, raw)


def test_selector_keeps_data_centres_and_rejects_each_exclusion():
    _, rows, df = _run()
    assert len(rows) == 25
    bases = pd.Series([r["select_basis"] for r in rows]).value_counts().to_dict()
    assert bases == {"naics_518210": 15, "name": 8, "naics_541513_operator": 2}
    names = set(df["name_canonical"])
    for rejected in (
        "SUNGARD AVAILABILITY SERVICES, LP",  # Permanently Closed
        "NYDIG DFM - OXBOW PAD",  # 518210 co-coded 211130 (well-site crypto mining)
        "GRMR OIL AND GAS - DEAL GULCH PRODUCTION",  # 518210, name says oil and gas
        "FDR HEADQUARTERS",  # 518210, name says headquarters
        "PHILCADE LLC / IBM TULSA OK BCS BPO",  # 518210, BPO office
        "LUFKIN PAPER MILL",  # 518210 on a paper mill
        "UBS AMERICAS INC.",  # 541513 without a data-centre operator
    ):
        assert rejected not in names


def test_select_basis_rule():
    row = {"AIR_OPERATING_STATUS_DESC": "Operating", "NAICS_CODES": "999999"}
    assert select_basis({**row, "FACILITY_NAME": "Travelers Data Center"}) == "name"
    assert select_basis({**row, "FACILITY_NAME": "Compass Datacenters PHX II"}) == "name"
    assert select_basis({**row, "FACILITY_NAME": "Fidelity Investments Data Ctr"}) == "name"
    assert select_basis({**row, "FACILITY_NAME": "CENTRA HEALTH ADMINISTRATION - DATA CENT"}) == "name"
    assert select_basis({**row, "FACILITY_NAME": "Data Central Office"}) is None
    assert select_basis({**row, "FACILITY_NAME": "Office Park"}) is None
    naics = {**row, "NAICS_CODES": "518210"}
    assert select_basis({**naics, "FACILITY_NAME": "Sharka"}) == "naics_518210"
    assert select_basis({**naics, "NAICS_CODES": "211130 518210", "FACILITY_NAME": "X"}) is None
    assert select_basis({**naics, "FACILITY_NAME": "NYDIG DFM - Veneta Pad"}) is None
    op = {**row, "NAICS_CODES": "541513"}
    assert select_basis({**op, "FACILITY_NAME": "Equinix LLC - DC 13"}) == "naics_541513_operator"
    assert select_basis({**op, "FACILITY_NAME": "L. L.Bean, Inc."}) is None
    closed = {**row, "AIR_OPERATING_STATUS_DESC": "Permanently Closed", "FACILITY_NAME": "Old Data Center"}
    assert select_basis(closed) is None
    # the fetch-side pre-filter is looser: it keeps closed and excluded rows for parse to judge
    assert is_candidate(closed) and is_candidate({**naics, "FACILITY_NAME": "LUFKIN PAPER MILL"})
    assert not is_candidate({**row, "FACILITY_NAME": "Funeral Home"})


def test_records_are_load_proposals_with_provenance():
    c, _, df = _run()
    assert list(df.columns) == c.columns
    assert set(df["kind"]) == {"load"} and set(df["technology"]) == {"load"}
    assert df["capacity_mw"].isna().all() and df["sponsor_name"].isna().all()
    for col in ("source_id", "source_url", "retrieved_at", "licence_id", "raw"):
        assert df[col].notna().all(), col
    assert df["record_id"].is_unique
    assert set(df["licence_id"]) == {c.source.licence_id}


def test_identity_url_and_cross_refs():
    _, _, df = _run()
    row = df[df["source_record_id"] == "VA0000005115374349"].iloc[0]
    assert row["record_id"] == f"{SOURCE_ID}:VA0000005115374349"
    assert row["name_canonical"] == "AMAZON DATA SERVICES INC IAD-264"
    raw = json.loads(row["raw"])
    assert row["source_url"] == DFR_URL.format(registry_id=raw["REGISTRY_ID"])
    assert row["cross_refs"] == f"icis_air:VA0000005115374349|frs:{raw['REGISTRY_ID']}"
    no_frs = df[df["name_canonical"] == "DOTIER, LLC"].iloc[0]
    assert no_frs["source_url"] == ICIS_URL and no_frs["cross_refs"] == "icis_air:AL0000000110100108"


def test_status_map_covers_every_value_in_the_fixture():
    _, _, df = _run()
    by_raw = dict(zip(df["status_raw"].fillna(""), df["lifecycle_state"], strict=False))
    assert by_raw["Planned Facility"] == "filed"
    assert by_raw["Under Construction"] == "under_construction"
    assert by_raw["Operating"] == "built"
    assert by_raw["Temporarily Closed"] == "built"
    assert by_raw[""] == "unknown"
    assert not df["status_rule"].str.endswith(".unmapped").any()
    assert df["lifecycle_state"].value_counts().to_dict() == {
        "built": 17,
        "filed": 3,
        "under_construction": 3,
        "unknown": 2,
    }


def test_only_frs_site_points_become_coordinates():
    _, rows, df = _run()
    by_name = {r["FACILITY_NAME"]: r for r in rows}
    placements = pd.Series([r["placement"] for r in rows]).value_counts().to_dict()
    assert placements == {
        "exact": 14,
        "method_not_site_specific": 8,
        "point_outside_state": 1,
        "accuracy_not_stated_or_coarse": 1,
        "no_frs_record": 1,
    }
    for r in rows:
        has_point = "Latitude" in r and "Longitude" in r
        assert has_point == (r["placement"] == "exact"), r["FACILITY_NAME"]
        assert "FAC_LAT" not in r  # a non-site coordinate never reaches `raw`
    walmart = by_name["WAL-MART NORTH DATA CENTER, FACILITY #8678"]
    assert walmart["placement"] == "point_outside_state"
    aligned = by_name["ALIGNED DATA CENTER (EGV) PROPCO LLC"]
    assert aligned["placement"] == "accuracy_not_stated_or_coarse" and aligned["frs_accuracy_m"] > 200
    counties = dict(zip(df["name_canonical"], df["county"], strict=False))
    # exact point: county from the point (ICIS says "Richland"; Mount Pleasant is in Racine County)
    assert counties["MICROSOFT CORPORATION - MKE 3B DATA CENTER"] == "Racine"
    assert by_name["MICROSOFT CORPORATION - MKE 3B DATA CENTER"]["COUNTY_NAME"] == "Richland"
    # not exact and ICIS county "Undetermined": no county, the loader places it at the state
    assert counties["CMH050"] is None
    assert placement_for(None, "VA") == ("no_frs_record", None, None)
    zip_centroid = {"FAC_LAT": "38.9", "FAC_LONG": "-77.4", "FAC_COLLECTION_METHOD": "Zip Code Centroid"}
    assert placement_for({**zip_centroid, "FAC_ACCURACY_METERS": "10"}, "VA")[0] == "method_not_site_specific"


def test_virginia_overlap_resolves_to_one_record_per_facility():
    """DEQ's `icis_air:<PLA_ICIS_ID>` and ICIS-Air's own `icis_air:<PGM_SYS_ID>` pair deterministically
    (D3); neighbouring campuses of one operator with different ids never pair."""
    _, _, icis = _run()
    va_c = connector_for("us.va.deq.data_center_air_sites")
    va_raw = snapshot("va_deq_air_sites_data_centers.json", VA_LAYER_URL, "application/json")
    va = va_c.normalize(va_c.parse(va_raw), va_raw)
    df = pd.concat([va, icis], ignore_index=True)
    det = resolve.deterministic(df)
    pairs = {
        (df.at[int(a), "source_record_id"], df.at[int(b), "source_record_id"])
        for a, b, p in zip(det["li"], det["ri"], det["pass"], strict=True)
        if p == "D3_xref"
    }
    assert pairs == {
        ("11790", "VA0000005119511790"),
        ("21527", "VA0000005111700071"),
        ("30142", "VA0000005111700009"),
        ("73158", "VA0000005110700814"),
        ("73160", "VA0000005110701042"),
        ("74129", "VA0000005168374129"),
        ("74349", "VA0000005115374349"),
    }
    assert resolve.shared_id_conflict("icis_air:A", "icis_air:B|frs:1")
    assert not resolve.shared_id_conflict("icis_air:A", "icis_air:A|frs:1")
    assert not resolve.shared_id_conflict("icis_air:A", "")
    # a source that repeats the id is ambiguous and is left to review, not paired
    dup = pd.concat([df, df[df["source_record_id"] == "74349"]], ignore_index=True)
    dup_pairs = resolve.deterministic(dup)
    assert not dup_pairs["rationale"].str.contains("VA0000005115374349").any()


def _zip_bytes(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for name, data in members.items():
            z.writestr(name, data)
    return buf.getvalue()


def test_ranged_zip_member_reader():
    first = b'"PGM_SYS_ID","REGISTRY_ID"\n' + b'"A","1"\n' * 500
    blob = _zip_bytes({"ICIS-AIR_FACILITIES.csv": first, "ICIS-AIR_PROGRAMS.csv": b"x" * 1000})
    tail = blob[-200:] if len(blob) > 200 else blob
    members = {m.name: m for m in central_directory(blob, len(blob))}
    assert set(members) == {"ICIS-AIR_FACILITIES.csv", "ICIS-AIR_PROGRAMS.csv"}
    m = members["ICIS-AIR_FACILITIES.csv"]
    assert m.size == len(first) and m.crc == zlib.crc32(first)
    assert inflate_member(blob[m.header_offset :], m) == first
    # the central directory can be read from the tail alone
    assert [x.name for x in central_directory(tail, len(blob))] == list(members)
    with pytest.raises(ParseError):
        inflate_member(blob[m.header_offset : m.header_offset + 40], m)  # truncated range
    with pytest.raises(ParseError):
        central_directory(b"not a zip", 9)


def test_redact_strips_contact_columns_and_leaves_clean_bytes_alone():
    c = connector_for(SOURCE_ID)
    doc = {
        "icis": {"columns": ["PGM_SYS_ID", "CONTACT_NAME"], "rows": [["A", "someone"]]},
        "frs": {"columns": ["REGISTRY_ID"], "rows": [["1"]]},
    }
    cleaned = json.loads(c.redact(json.dumps(doc).encode()))
    assert cleaned["icis"] == {"columns": ["PGM_SYS_ID"], "rows": [["A"]]}
    raw_bytes = snapshot(FIXTURE, ICIS_URL).content
    assert c.redact(raw_bytes) == raw_bytes


def test_layout_change_and_bad_payload_are_parse_errors():
    c = connector_for(SOURCE_ID)
    raw = snapshot(FIXTURE, ICIS_URL, "application/json")
    raw.content = b"<html>not json</html>"
    with pytest.raises(ParseError):
        c.parse(raw)
    raw.content = json.dumps({"icis": {"columns": ["NAME"], "rows": []}, "frs": {"columns": []}}).encode()
    with pytest.raises(ParseError):
        c.parse(raw)
    raw.content = json.dumps({"pages": []}).encode()
    with pytest.raises(ParseError):
        c.parse(raw)


# ------------------------------------------------------------------ fetch: conditional GETs (docs/25 §3.9)
# `FakeEcho` answers the way echo.epa.gov (Apache) was measured to on 2026-09-29: an ETag and
# Last-Modified on every 200/206, `Range` honoured, and a 304 carrying only the ETag when
# `If-None-Match` (or, without it, `If-Modified-Since`) matches, whether or not `Range` is also sent.
# The two zips are built from the recorded fixture's rows plus rows the pre-filter must drop.
ROBOTS = b"User-agent: *\nCrawl-delay: 10\nAllow: /files/\n"


class FakeResponse:
    def __init__(self, status: int, body: bytes, headers: dict[str, str]) -> None:
        self.status_code = status
        self.content = body
        self.text = body.decode("utf-8", "replace")
        self.headers = headers
        self.closed = False

    def iter_content(self, chunk_size: int) -> Any:
        for i in range(0, len(self.content), chunk_size):
            yield self.content[i : i + chunk_size]

    def close(self) -> None:
        self.closed = True


class FakeEcho:
    """The two bulk zips on echo.epa.gov/files/, with validators that change on every `put`."""

    def __init__(self, files: dict[str, bytes]) -> None:
        self.files: dict[str, tuple[bytes, str, str]] = {}
        self.version = 0
        for url, body in files.items():
            self.put(url, body)
        self.headers: dict[str, str] = {}
        self.calls: list[tuple[str, str, dict[str, str], bool]] = []
        self.body_bytes = 0

    def put(self, url: str, body: bytes) -> None:
        self.version += 1
        etag = f'"{len(body):x}-{self.version:x}"'
        self.files[url] = (body, etag, formatdate(1_790_000_000 + self.version * 86_400, usegmt=True))

    def reset(self) -> None:
        self.calls.clear()
        self.body_bytes = 0

    def request(
        self, method: str, url: str, headers: dict[str, str] | None = None, **kw: Any
    ) -> FakeResponse:
        sent = dict(headers or {})
        self.calls.append((method, url, sent, bool(kw.get("stream"))))
        if url.endswith("/robots.txt"):
            return FakeResponse(200, ROBOTS, {"Content-Type": "text/plain"})
        body, etag, modified = self.files[url]
        inm, ims = sent.get("If-None-Match"), sent.get("If-Modified-Since")
        if (inm is not None and inm == etag) or (inm is None and ims is not None and ims == modified):
            return FakeResponse(304, b"", {"ETag": etag})
        base = {
            "ETag": etag,
            "Last-Modified": modified,
            "Accept-Ranges": "bytes",
            "Content-Type": "application/zip",
        }
        m = re.fullmatch(r"bytes=(\d*)-(\d*)", sent.get("Range", ""))
        if m:
            first, last = m.groups()
            if first == "":
                start, end = max(0, len(body) - int(last)), len(body) - 1
            else:
                start, end = int(first), min(len(body) - 1, int(last)) if last else len(body) - 1
            part = body[start : end + 1]
            self.body_bytes += len(part)
            headers_out = {**base, "Content-Range": f"bytes {start}-{end}/{len(body)}"}
            return FakeResponse(206, part, {**headers_out, "Content-Length": str(len(part))})
        self.body_bytes += len(body)
        return FakeResponse(200, body, {**base, "Content-Length": str(len(body))})

    def get(self, url: str, **kw: Any) -> FakeResponse:
        return self.request("GET", url, **kw)

    def gets(self, url: str) -> list[dict[str, str]]:
        return [h for m, u, h, _ in self.calls if m == "GET" and u == url]


# ------------------------------------------------------------------ upstream files from the fixture
UPSTREAM = json.loads(fixture_path(FIXTURE).read_text(encoding="utf-8"))
ICIS_COLS: list[str] = UPSTREAM["icis"]["columns"]
FRS_COLS: list[str] = UPSTREAM["frs"]["columns"]


def _csv(columns: list[str], rows: list[list[str]]) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(columns)
    w.writerows(rows)
    return buf.getvalue().encode("utf-8")


def _zip(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for name, data in members.items():
            z.writestr(name, data)
    return buf.getvalue()


def icis_zip(rows: list[list[str]]) -> bytes:
    """The fixture's candidates plus a facility the pre-filter drops, and a second member."""
    other = ["XX0000000000000001", "110000000001", "FUNERAL HOME", *[""] * (len(ICIS_COLS) - 3)]
    return _zip({ICIS_MEMBER: _csv(ICIS_COLS, [*rows, other]), "ICIS-AIR_PROGRAMS.csv": b"x" * 5000})


def exporter_zip(rows: list[list[str]]) -> bytes:
    """The fixture's FRS rows (with one extra upstream column) plus one no candidate cites."""
    cols = [*FRS_COLS, "FAC_NAME"]
    extra = ["999999999999", "TX", "HARRIS", "29.7", "-95.3", "", "", "", "", "", "SOMEWHERE"]
    return _zip({EXPORTER_MEMBER: _csv(cols, [[*r, "N"] for r in rows] + [extra])})


@pytest.fixture()
def clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """One day per fetch, so each run's snapshot has its own timestamped key."""
    days = iter(range(1, 100))
    start = dt.datetime(2026, 9, 29, 6, 0, tzinfo=dt.UTC)
    monkeypatch.setattr(connector_module, "utcnow", lambda: start + dt.timedelta(days=next(days)))


@pytest.fixture()
def echo(clock: None) -> FakeEcho:
    return FakeEcho(
        {
            ICIS_URL: icis_zip([list(r) for r in UPSTREAM["icis"]["rows"]]),
            EXPORTER_URL: exporter_zip([list(r) for r in UPSTREAM["frs"]["rows"]]),
        }
    )


def _fetch_run(registry: Any, store: Store, echo: FakeEcho) -> Any:
    echo.reset()
    http = PoliteSession(session=echo, sleep=lambda _s: None)  # type: ignore[arg-type]
    return run(SOURCE_ID, registry=registry, store=store, http=http)


def _snapshot_doc(store: Store, result: Any) -> dict[str, Any]:
    return dict(json.loads(result.paths["snapshot"].read_bytes()))


# ------------------------------------------------------------------ tests
def test_first_run_fetches_both_files_and_records_their_validators(registry, tmp_path, echo):
    first = _fetch_run(registry, Store(tmp_path), echo)
    assert first.status == "ok"
    meta = first.run["snapshot"]["meta"]
    assert meta["fetch_version"] == FETCH_VERSION and meta["upstream"] == "changed"
    for key, url in (("icis", ICIS_URL), ("exporter", EXPORTER_URL)):
        _, etag, modified = echo.files[url]
        assert meta[key]["etag"] == etag and meta[key]["last_modified"] == modified
        assert meta[key]["not_modified"] is False
    # nothing to compare against: no conditional header goes out
    assert not any("If-None-Match" in h or "If-Modified-Since" in h for _, _, h, _ in echo.calls)
    # the Exporter is streamed, never read whole into memory
    assert [s for m, u, _, s in echo.calls if u == EXPORTER_URL] == [True]
    assert meta["icis"]["candidates"] == 35 and meta["icis"]["rows_total"] == 36
    assert meta["exporter"]["rows_kept"] == 34 and meta["exporter"]["rows_total"] == 35
    assert first.run["rows_fetched"] == 25


def test_unchanged_upstream_is_an_unchanged_run_that_downloads_nothing(registry, tmp_path, echo):
    store = Store(tmp_path)
    first = _fetch_run(registry, store, echo)
    second = _fetch_run(registry, store, echo)
    assert second.status == "unchanged"
    assert set(second.paths) == {"run"}  # no snapshot, parquet or events written
    record = second.run
    assert record["snapshot"]["sha256"] == first.run["snapshot"]["sha256"]
    assert record["http_status"] == 304
    meta = record["snapshot"]["meta"]
    assert meta["upstream"] == "unchanged"
    assert meta["icis"]["not_modified"] is True and meta["exporter"]["not_modified"] is True
    assert meta["icis"]["bytes_fetched"] == 0 and meta["exporter"]["bytes_fetched"] == 0
    # validators and totals carried forward for the next run (Apache's 304 omits Last-Modified)
    assert meta["icis"]["last_modified"] == first.run["snapshot"]["meta"]["icis"]["last_modified"]
    assert meta["exporter"]["rows_total"] == 35 and meta["icis"]["candidates"] == 35
    # one conditional GET per file, each carrying the first run's validators; no body bytes
    assert echo.body_bytes == 0
    icis_gets, exp_gets = echo.gets(ICIS_URL), echo.gets(EXPORTER_URL)
    assert len(icis_gets) == 1 and len(exp_gets) == 1
    assert icis_gets[0]["If-None-Match"] == echo.files[ICIS_URL][1]
    assert icis_gets[0]["Range"].startswith("bytes=-")
    assert exp_gets[0] == conditional_headers(first.run["snapshot"]["meta"]["exporter"])
    assert record["snapshot"]["requests_made"] == 3  # robots.txt (once per session) + two conditional GETs
    # a third run still has validators to send (read from the unchanged run's own record)
    third = _fetch_run(registry, store, echo)
    assert third.status == "unchanged" and echo.body_bytes == 0


def test_a_changed_exporter_reuses_the_stored_icis_table(registry, tmp_path, echo):
    store = Store(tmp_path)
    first = _fetch_run(registry, store, echo)
    frs = [list(r) for r in UPSTREAM["frs"]["rows"]]
    frs[0][3] = "36.60999"  # one facility's FRS latitude moves
    echo.put(EXPORTER_URL, exporter_zip(frs))
    second = _fetch_run(registry, store, echo)
    assert second.status == "ok"
    meta = second.run["snapshot"]["meta"]
    assert meta["upstream"] == "changed"
    assert meta["icis"]["not_modified"] is True and meta["exporter"]["not_modified"] is False
    assert len(echo.gets(ICIS_URL)) == 1  # the 304 tail only; no member range
    before, after = _snapshot_doc(store, first), _snapshot_doc(store, second)
    assert after["icis"] == before["icis"]
    assert after["frs"]["rows"][0][3] == "36.60999"


def test_a_changed_icis_file_whose_ids_are_covered_skips_the_exporter(registry, tmp_path, echo):
    store = Store(tmp_path)
    _fetch_run(registry, store, echo)
    rows = [list(r) for r in UPSTREAM["icis"]["rows"]]
    status = ICIS_COLS.index("AIR_OPERATING_STATUS_DESC")
    rows[0][status] = "Planned Facility"
    rows = rows[1:] + rows[:1]  # and the file's row order changes
    echo.put(ICIS_URL, icis_zip(rows))
    second = _fetch_run(registry, store, echo)
    assert second.status == "ok"
    meta = second.run["snapshot"]["meta"]
    assert meta["exporter"]["not_modified"] is True and meta["icis"]["not_modified"] is False
    assert echo.gets(EXPORTER_URL)[0].get("If-None-Match") == echo.files[EXPORTER_URL][1]
    assert second.run["rows_changed"] == 1
    # byte for byte what a fetch with no stored snapshot builds from the same two files
    fresh = _fetch_run(registry, Store(tmp_path / "fresh"), echo)
    assert fresh.run["snapshot"]["sha256"] == second.run["snapshot"]["sha256"]


def test_a_new_registry_id_downloads_the_exporter_unconditionally(registry, tmp_path, echo):
    store = Store(tmp_path)
    _fetch_run(registry, store, echo)
    rows = [list(r) for r in UPSTREAM["icis"]["rows"]]
    new = list(rows[0])
    new[0], new[1], new[2] = "TX0000000000000999", "999999999999", "NEW HYPERSCALE DATA CENTER"
    echo.put(ICIS_URL, icis_zip([*rows, new]))
    second = _fetch_run(registry, store, echo)
    assert second.status == "ok"
    exp_gets = echo.gets(EXPORTER_URL)
    assert len(exp_gets) == 1 and not conditional_headers({"etag": exp_gets[0].get("If-None-Match")})
    doc = _snapshot_doc(store, second)
    assert "999999999999" in {r[0] for r in doc["frs"]["rows"]}
    assert second.run["snapshot"]["meta"]["exporter"]["not_modified"] is False


def test_a_snapshot_from_other_fetch_code_is_not_reused(registry, tmp_path, echo):
    store = Store(tmp_path)
    first = _fetch_run(registry, store, echo)
    path = first.paths["run"]
    record = json.loads(path.read_text())
    record["snapshot"]["meta"].pop("fetch_version")  # as the lane H1 runs recorded it
    store.write_run(SOURCE_ID, path.stem, record)
    second = _fetch_run(registry, store, echo)
    assert not any("If-None-Match" in h or "If-Modified-Since" in h for _, _, h, _ in echo.calls)
    assert second.status == "unchanged"  # same bytes, found the old way: by downloading them


def test_a_missing_stored_object_falls_back_to_a_full_fetch(registry, tmp_path, echo):
    store = Store(tmp_path)
    first = _fetch_run(registry, store, echo)
    first.paths["snapshot"].unlink()
    assert store.last_snapshot(SOURCE_ID) is None
    second = _fetch_run(registry, store, echo)
    assert not any("If-None-Match" in h for _, _, h, _ in echo.calls)
    assert second.status == "unchanged" and echo.body_bytes > 0


def test_conditional_headers():
    assert conditional_headers(None) == {}
    assert conditional_headers({"etag": None, "last_modified": ""}) == {}
    assert conditional_headers({"etag": '"a-1"', "last_modified": "Sun, 27 Sep 2026 10:11:26 GMT"}) == {
        "If-None-Match": '"a-1"',
        "If-Modified-Since": "Sun, 27 Sep 2026 10:11:26 GMT",
    }


# ------------------------------------------------------------------ re-registrations and county text
# Real ICIS-AIR_FACILITIES.csv rows (2026-10-07 store frame), one string per row with "|" between the
# fixture's columns, in its order.
_REREGISTERED = [
    "NE0000003105500430|110045442124|NEBRASKA COLOCATION CENTER|1623 FARNAM ST|OMAHA|Douglas|NE|681022107|07|7374|518210|POF|MIN|Minor Emissions|OPR|Operating|No Violation Identified||",  # noqa: E501
    "NECOO0003105500430|110045442124|NEBRASKA COLOCATION CENTER|1623 FARNAM ST|OMAHA|Douglas|NE|68102-2107|07|7374|518210|POF|MIN|Minor Emissions|OPR|Operating|No Violation Identified|COO|City Of Omaha",  # noqa: E501
    "IN00010900094|110072198023|WOODLAND CARIBOU LLC|1598 W SR 42|MOORESVILLE|Morgan|IN|46158|05|7374|518210||||CNS|Under Construction|No Violation Identified||",  # noqa: E501
    "IN0000001810900094|110072198023|WOODLAND CARIBOU LLC|1598 W SR 42|MOORESVILLE|Morgan|IN|46158|05|7374|518210||||OPR|Operating|No Violation Identified||",  # noqa: E501
    # Same FRS id, different names: one campus's buildings, or a renamed entity. Never folded.
    "KY0000002101500236|110070526398|CYRUSONE LLC - FLORENCE DATA CENTER|7190 INDUSTRIAL RD|FLORENCE|Boone|KY|41042|04|7374|518210|POF|SMI|Synthetic Minor Emissions|OPR|Operating|No Violation Identified||",  # noqa: E501
    "KY0002101500236|110070526398|CYRUS ONE LLC|7190 INDUSTRIAL RD|FLORENCE|Boone|KY|41042|04|7374|518210|POF|SMI|Synthetic Minor Emissions|OPR|Operating|No Violation Identified||",  # noqa: E501
    # No exact FRS point: county comes from ICIS-Air's own text, "Harrisonburg (city)".
    "VA0000005166000169|110040513209|ANTHEM CDC 3|1175 NORTH MAIN STREET|HARRISONBURG|Harrisonburg (city)|VA|22802|03||518210|NON|SMI|Synthetic Minor Emissions|OPR|Operating|No Violation Identified||",  # noqa: E501
]


def _run_rows(icis_rows: list[str]) -> tuple[list[dict[str, Any]], pd.DataFrame, object]:
    c = connector_for(SOURCE_ID)
    raw = snapshot(FIXTURE, ICIS_URL, "application/json")
    doc = json.loads(raw.content)
    doc["icis"]["rows"] = [r.split("|") for r in icis_rows]
    doc["frs"]["rows"] = []
    raw.content = json.dumps(doc).encode()
    rows = c.parse(raw)
    return rows, c.normalize(rows, raw), raw


def test_a_facility_registered_twice_under_one_frs_id_and_name_is_one_record():
    """Audit RES-14: re-padded / re-prefixed programme ids made two live proposals of one site."""
    rows, df, raw = _run_rows(_REREGISTERED)
    ids = list(df["source_record_id"])
    assert "NECOO0003105500430" not in ids and "IN00010900094" not in ids
    assert ids.count("NE0000003105500430") == 1 and ids.count("IN0000001810900094") == 1
    assert raw.meta["reregistrations_folded"] == 2
    caribou = df[df["source_record_id"] == "IN0000001810900094"].iloc[0]
    assert caribou["lifecycle_state"] == "built"  # the kept registration's own status
    assert json.loads(caribou["raw"])["duplicate_pgm_sys_ids"] == [
        {"PGM_SYS_ID": "IN00010900094", "AIR_OPERATING_STATUS_DESC": "Under Construction"}
    ]
    # Either id still pairs with a record that cites it (Virginia DEQ's PLA_ICIS_ID).
    assert caribou["cross_refs"] == "icis_air:IN0000001810900094|icis_air:IN00010900094|frs:110072198023"
    assert not resolve.shared_id_conflict(caribou["cross_refs"], "icis_air:IN00010900094")
    # Different names under one FRS id stay two records.
    assert {"KY0000002101500236", "KY0002101500236"} <= set(ids)
    assert len(rows) == len(_REREGISTERED) - 2


def test_county_text_uses_the_shared_spelling():
    _, df, _ = _run_rows(_REREGISTERED)
    county = dict(zip(df["source_record_id"], df["county"], strict=True))
    assert county["VA0000005166000169"] == "Harrisonburg city"
    assert connector_module.county_for_point(-119.7674, 39.1638) == "Carson City"  # not "Carson City city"
    assert connector_module.county_for_point(-77.3064, 38.8462) == "Fairfax city"
    assert connector_module.county_for_point(-77.4311, 38.8942) == "Fairfax"
