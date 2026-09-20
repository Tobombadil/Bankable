"""PHMSA gas transmission and gathering annual reports + incidents -> per-operator pipeline
features (owner decision 2026-09-18 "objective feature set only, valuation excluded"; ADR 0008
§4; docs/21 §3.22 `attributes`).

Tabular, not geometric: this source decorates the EIA Atlas pipeline layer rather than drawing it
(`data/sources.yaml`, `us.phmsa.pipeline_operator_reports`). One row per PHMSA `OPERATOR_ID`:

    fetch(dataset)       -> FetchResult    network; snapshot + run record under data/snapshots|runs
    parse_annual(bytes)  -> AnnualTables   no network
    parse_incidents(b)   -> DataFrame      no network
    build_rows(...)      -> DataFrame      one row per operator, written to parquet

**Where the bytes come from (measured 2026-09-19).** Every `www.phmsa.dot.gov` URL -- the data
page, the data files and `robots.txt` itself -- answers `HTTP 403` from an AkamaiGHost edge to
this sandbox's egress (an "Access Denied" page, not a challenge interstitial), so the origin
cannot be read here at all. The same two files are public in the Internet Archive, so `fetch`
tries the origin first, records its exact answer in the run record, and falls back to the Wayback
`id_` (unrewritten-bytes) capture that the availability API names, carrying the origin's own
`Last-Modified` through as `upstream_last_modified`. The recorded `source_url` stays the PHMSA
origin URL -- that is where the data is published and where a reader should go -- while
`fetched_url` in the run record says exactly what was read. When the origin answers again, the
same code path uses it and nothing else changes.

**Three members of PHMSA's published annual zip are damaged** (measured 2026-09-19 on two
byte-identical downloads, sha256 5ed2ef12…): `annual_gas_transmission_gathering_2010.xlsx`,
`…_2019.xlsx` and `GT AR 2025 Part J.csv` raise zlib/CRC errors. The per-year workbooks carry
every part as a sheet, so `parse_annual` reads the newest *intact* `annual_…_<year>.xlsx` and
reports which one it used; the loose per-part CSVs (one of which is the damaged Part J) are not
read at all.

**Objective features only.** Every value is a number the operator filed with PHMSA, summed over
that operator's reports and states for the report year: total onshore transmission miles (Part L
class-location table), miles by decade of installation (Part J), miles by diameter class (Part H),
and incident counts over the five report years ending with the annual report year (the incident
file). Nothing is modelled, rated or valued.

**`incidents_5y_significant` is null here, by design.** "Significant incident" is PHMSA's own
flag, published in `PHMSA_Pipeline_Safety_Flagged_Incidents.zip`; the public incident file carries
neither that flag nor the 1984-dollar cost the definition turns on, and the flagged file could not
be retrieved (origin 403; the Archive's copy failed repeatedly mid-transfer, 2026-09-19). Deriving
it from the fields that *are* published would silently understate it, so the key is `None` with a
`feature_flags` note, and the stated sub-counts (fatality, injury, ignition, explosion) ride
alongside as objective facts the file does state.

CLI: `python -m pipeline.context.phmsa` (fetch both files, write the parquet, print one JSON
summary line), `--annual-snapshot/--incident-snapshot PATH` to parse recorded bytes instead.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import io
import json
import pathlib
import re
import time
import zipfile
from dataclasses import dataclass, field
from typing import Any

import openpyxl
import pandas as pd

from pipeline.connectors.base import ParseError, json_default
from pipeline.connectors.http import HttpBlocked, HttpFailed, PoliteSession
from pipeline.connectors.store import Store, ts_token
from pipeline.context import fuels

SOURCE_ID = "us.phmsa.pipeline_operator_reports"

#: PHMSA's own published files (the data page links both). These stay the recorded `source_url`
#: even when the bytes are read from an archived copy.
ANNUAL_URL = (
    "https://www.phmsa.dot.gov/sites/phmsa.dot.gov/files/data_statistics/pipeline/"
    "annual_gas_transmission_gathering_2010_present.zip"
)
INCIDENT_URL = (
    "https://www.phmsa.dot.gov/sites/phmsa.dot.gov/files/data_statistics/pipeline/"
    "incident_gas_transmission_gathering_jan2010_present.zip"
)
DATA_PAGE = (
    "https://www.phmsa.dot.gov/data-and-statistics/pipeline/"
    "gas-distribution-gas-gathering-gas-transmission-hazardous-liquids"
)
#: The CDX index is the authoritative capture list (the availability API answers `{}` for a URL
#: passed with its scheme, measured 2026-09-19); `limit=-5` returns the five newest captures.
WAYBACK_CDX = (
    "https://web.archive.org/cdx/search/cdx?url={url}&filter=statuscode:200"
    "&filter=mimetype:application/zip&fl=timestamp,length&limit=-5"
)
WAYBACK_AVAILABLE = "https://archive.org/wayback/available?url="

#: Politeness: PHMSA at the FERC ceiling (0.5 rps); the Archive gets the same, and its hosts are
#: slow on 25-110 MB objects, so the session timeout is generous.
RATE_LIMITS: dict[str, float] = {
    "www.phmsa.dot.gov": 0.5,
    "web.archive.org": 0.5,
    "archive.org": 0.5,
}

DATASETS: dict[str, str] = {"annual": ANNUAL_URL, "incident": INCIDENT_URL}

#: Part J (year of installation) column stem -> the key written to `miles_by_decade`.
DECADE_COLUMNS: dict[str, str] = {
    "PARTJTONUNKWN": "unknown",
    "PARTJTONPRE1940": "pre_1940",
    "PARTJTON194049": "1940s",
    "PARTJTON195059": "1950s",
    "PARTJTON196069": "1960s",
    "PARTJTON197079": "1970s",
    "PARTJTON198089": "1980s",
    "PARTJTON199099": "1990s",
    "PARTJTON200009": "2000s",
    "PARTJTON201019": "2010s",
    "PARTJTON202029": "2020s",
}

#: Part H (onshore transmission miles by nominal diameter) column -> key in `miles_by_diameter`.
#: PHMSA's own bucket labels: `PARTHON4LESS` is "4 inches or less", `PARTHON58OVER` is "58 and
#: over", everything between is the nominal inch size.
DIAMETER_COLUMNS: dict[str, str] = {
    "PARTHON4LESS": "4_or_less",
    "PARTHON58OVER": "58_or_more",
    "PARTHON_OTHER_PIPE_MILE_TOTAL": "other",
    **{f"PARTHON{n}": str(n) for n in (6, 8, 10, 12, 14, 16, 18, 20, 22, 24, 26, 28, 30, 32, 34)},
    **{f"PARTHON{n}": str(n) for n in (36, 38, 40, 42, 44, 46, 48, 52, 56)},
}

#: Sheet names in a per-year annual workbook, and the identity columns every part repeats.
SHEET_IDENTITY = "GT AR Part A to D"
SHEET_CLASS_MILES = "GT AR Part L"
SHEET_DECADE = "GT AR Part J"
SHEET_DIAMETER = "GT AR Part H"
#: Two free-text note rows precede the header in every sheet of the per-year workbook.
HEADER_ROW = 3

#: Part L: onshore transmission miles, summed over class locations 1-4.
CLASS_MILES_ONSHORE = "PARTLTONTOT"
CLASS_MILES_TOTAL = "PARTLTTOTAL"
DECADE_TOTAL = "PARTJTONTOTAL"
DIAMETER_TOTAL = "PARTHONTOTAL"

INCIDENT_MEMBER_RE = re.compile(r"incident_gas_transmission_gathering.*\.txt$", re.IGNORECASE)
ANNUAL_MEMBER_RE = re.compile(r"annual_gas_transmission_gathering_(\d{4})\.xlsx$", re.IGNORECASE)

#: Years counted by `incidents_5y_*`, ending with the annual report year.
INCIDENT_WINDOW_YEARS = 5

#: `YYYYMMDDTHHMMSSZ`, the store's snapshot token (this source suffixes it with the dataset).
_TOKEN_RE = re.compile(r"^\d{8}T\d{6}Z$")

FEATURE_KEY = "phmsa"


# --------------------------------------------------------------------------------------- fetch
@dataclass
class FetchResult:
    dataset: str
    source_url: str  # PHMSA's own URL: where the data is published
    fetched_url: str  # what was actually read (origin, or an archived copy)
    via: str  # "origin" | "wayback"
    retrieved_at: str
    content: bytes
    sha256: str
    origin_result: str
    upstream_last_modified: str | None = None
    snapshot_path: pathlib.Path | None = None
    run_path: pathlib.Path | None = None

    @property
    def bytes_(self) -> int:
        return len(self.content)


def polite_session() -> PoliteSession:
    return PoliteSession(rate_limits=RATE_LIMITS, default_rps=0.5, timeout=180.0)


def wayback_url(session: PoliteSession, url: str) -> tuple[str, str]:
    """`(capture_url, timestamp)` for the Archive's newest successful capture of `url`: the CDX
    index first, the availability API second. The `id_` suffix asks for the bytes as captured,
    unrewritten, which is what makes the archived copy byte-comparable with the origin's."""
    timestamp = ""
    resp = session.get(WAYBACK_CDX.format(url=url), honour_robots=False)
    if resp.status_code < 400 and resp.text.strip():
        lines = [line.split() for line in resp.text.strip().splitlines() if line.strip()]
        if lines and lines[-1]:
            timestamp = lines[-1][0]
    if not timestamp:
        # Availability wants the URL without its scheme.
        bare = url.split("://", 1)[-1]
        alt = session.get(f"{WAYBACK_AVAILABLE}{bare}", honour_robots=False)
        if alt.status_code < 400:
            try:
                closest = ((alt.json().get("archived_snapshots") or {}).get("closest")) or {}
            except ValueError:
                closest = {}
            if closest.get("available"):
                timestamp = str(closest.get("timestamp") or "")
    if not timestamp:
        raise HttpFailed(f"no Wayback capture available for {url}")
    return f"https://web.archive.org/web/{timestamp}id_/{url}", timestamp


def fetch(
    dataset: str,
    *,
    session: PoliteSession | None = None,
    store: Store | None = None,
    write: bool = True,
    allow_wayback: bool = True,
) -> FetchResult:
    """One dataset (`annual` | `incident`): PHMSA origin first, the Archive's capture second.
    Both answers are recorded in the run record, so which copy a run read is auditable."""
    if dataset not in DATASETS:
        raise KeyError(f"{dataset!r} is not one of {sorted(DATASETS)}")
    url = DATASETS[dataset]
    session = session or polite_session()
    store = store or Store()

    content: bytes | None = None
    fetched_url = url
    via = "origin"
    origin_result = "ok"
    last_modified: str | None = None
    try:
        resp = session.get(url, honour_robots=True)
        if resp.status_code != 200 or not resp.content[:2] == b"PK":
            raise HttpFailed(
                f"{url} answered HTTP {resp.status_code} ({resp.headers.get('Content-Type')}), not a zip"
            )
        content = resp.content
        last_modified = resp.headers.get("Last-Modified")
    except (HttpBlocked, HttpFailed) as e:
        origin_result = f"{type(e).__name__}: {e}"
        if not allow_wayback:
            raise
    if content is None:
        fetched_url, _ = wayback_url(session, url)
        via = "wayback"
        resp = session.get(fetched_url, honour_robots=False)
        if resp.status_code != 200 or resp.content[:2] != b"PK":
            raise HttpFailed(f"{fetched_url} answered HTTP {resp.status_code}, not a zip")
        content = resp.content
        last_modified = resp.headers.get("x-archive-orig-last-modified")

    retrieved_at = fuels.iso(fuels.utc_now())
    result = FetchResult(
        dataset=dataset,
        source_url=url,
        fetched_url=fetched_url,
        via=via,
        retrieved_at=retrieved_at,
        content=content,
        sha256=hashlib.sha256(content).hexdigest(),
        origin_result=origin_result,
        upstream_last_modified=last_modified,
    )
    if write:
        token = ts_token(fuels.utc_now())
        result.snapshot_path = store.write_snapshot(SOURCE_ID, f"{token}-{dataset}", "zip", content)
        result.run_path = store.write_run(
            SOURCE_ID,
            f"{token}-{dataset}",
            {
                "id": f"{SOURCE_ID}:{token}-{dataset}",
                "source_id": SOURCE_ID,
                "dataset": dataset,
                "kind": "feature",
                "status": "ok",
                "retrieved_at": retrieved_at,
                "bytes": len(content),
                "snapshot": {
                    "fetched_url": fetched_url,
                    "source_url": url,
                    "via": via,
                    "origin_result": origin_result,
                    "upstream_last_modified": last_modified,
                    "byte_size": len(content),
                    "sha256": result.sha256,
                    "path": str(result.snapshot_path.relative_to(store.root)),
                },
                "requests_made": session.requests_made,
            },
        )
    return result


# --------------------------------------------------------------------------------------- parse
@dataclass
class AnnualTables:
    """The four parts `build_rows` reads, plus which workbook member they came from."""

    report_year: int
    member: str
    datafile_as_of: str | None
    identity: pd.DataFrame
    class_miles: pd.DataFrame
    decade: pd.DataFrame
    diameter: pd.DataFrame
    damaged_members: list[str] = field(default_factory=list)


def _sheet_frame(book: openpyxl.Workbook, name: str) -> pd.DataFrame:
    if name not in book.sheetnames:
        raise ParseError(f"annual workbook has no sheet {name!r} (sheets: {book.sheetnames})")
    rows = book[name].iter_rows(min_row=HEADER_ROW, values_only=True)
    try:
        header = [str(h).strip() if h is not None else "" for h in next(rows)]
    except StopIteration as e:
        raise ParseError(f"sheet {name!r} is empty") from e
    if "OPERATOR_ID" not in header:
        raise ParseError(f"sheet {name!r} header has no OPERATOR_ID (got {header[:6]})")
    body = [r for r in rows if any(v is not None for v in r)]
    frame = pd.DataFrame(body, columns=header)
    frame["OPERATOR_ID"] = frame["OPERATOR_ID"].map(_operator_id)
    return frame


def _operator_id(value: Any) -> str | None:
    """PHMSA operator ids are integers stored as numbers in the workbook and as text in the
    incident file; both normalise to the same digit string."""
    text = fuels.clean_str(value)
    if text is None:
        return None
    if re.fullmatch(r"\d+\.0", text):
        text = text[:-2]
    return text


def annual_years(content: bytes) -> list[int]:
    """Report years present as per-year workbooks, newest first."""
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        years = {int(m.group(1)) for n in zf.namelist() if (m := ANNUAL_MEMBER_RE.search(n))}
    return sorted(years, reverse=True)


def parse_annual(content: bytes, year: int | None = None) -> AnnualTables:
    """The newest intact per-year workbook (or `year`). PHMSA's published zip carries damaged
    members (module docstring), so an unreadable workbook is recorded and the next year tried
    rather than aborting the run."""
    damaged: list[str] = []
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        members = {int(m.group(1)): n for n in zf.namelist() if (m := ANNUAL_MEMBER_RE.search(n))}
        if not members:
            raise ParseError("no annual_gas_transmission_gathering_<year>.xlsx member in the zip")
        wanted = [year] if year is not None else sorted(members, reverse=True)
        for candidate in wanted:
            name = members.get(candidate)
            if name is None:
                raise ParseError(f"no annual workbook for {candidate} (have {sorted(members)})")
            try:
                raw = zf.read(name)
                book = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
            except (zipfile.BadZipFile, OSError, ValueError, KeyError) as e:
                damaged.append(f"{name}: {type(e).__name__}")
                continue
            identity = _sheet_frame(book, SHEET_IDENTITY)
            tables = AnnualTables(
                report_year=candidate,
                member=name,
                datafile_as_of=_as_of(identity),
                identity=identity,
                class_miles=_sheet_frame(book, SHEET_CLASS_MILES),
                decade=_sheet_frame(book, SHEET_DECADE),
                diameter=_sheet_frame(book, SHEET_DIAMETER),
                damaged_members=damaged,
            )
            book.close()
            return tables
    raise ParseError(f"no readable annual workbook; damaged members: {damaged}")


def _as_of(identity: pd.DataFrame) -> str | None:
    if "DATAFILE_AS_OF" not in identity.columns or not len(identity):
        return None
    values = identity["DATAFILE_AS_OF"].dropna()
    if not len(values):
        return None
    stamp = pd.Timestamp(values.iloc[0])
    return None if pd.isna(stamp) else str(stamp.date())


def parse_incidents(content: bytes) -> pd.DataFrame:
    """The tab-separated incident table inside PHMSA's incident zip. The file is cp1252 (it
    carries non-breaking spaces in free-text fields), which is why it is decoded explicitly."""
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        names = [n for n in zf.namelist() if INCIDENT_MEMBER_RE.search(n)]
        if not names:
            raise ParseError(f"no incident .txt member in the zip (members: {zf.namelist()[:5]})")
        raw = zf.read(names[0])
    try:
        text = raw.decode("cp1252")
    except UnicodeDecodeError as e:  # pragma: no cover - defensive
        raise ParseError(f"incident file is not cp1252: {e}") from e
    frame = pd.read_csv(io.StringIO(text), sep="\t", dtype=str, low_memory=False)
    missing = {"OPERATOR_ID", "IYEAR", "REPORT_NUMBER"} - set(frame.columns)
    if missing:
        raise ParseError(f"incident file lacks {sorted(missing)}")
    frame["OPERATOR_ID"] = frame["OPERATOR_ID"].map(_operator_id)
    frame["IYEAR"] = pd.to_numeric(frame["IYEAR"], errors="coerce")
    return frame


# ------------------------------------------------------------------------------- feature build
def _sum_by_operator(frame: pd.DataFrame, columns: dict[str, str]) -> dict[str, dict[str, float]]:
    """`{operator_id: {key: miles}}`, summing each PHMSA column over the operator's reports and
    states. A column the vintage does not carry is simply absent, never zero-filled."""
    present = {col: key for col, key in columns.items() if col in frame.columns}
    if not present or not len(frame):
        return {}
    work = frame[["OPERATOR_ID", *present]].copy()
    for col in present:
        work[col] = pd.to_numeric(work[col], errors="coerce")
    grouped = work.groupby("OPERATOR_ID", dropna=True).sum(numeric_only=True)
    out: dict[str, dict[str, float]] = {}
    for operator_id, row in grouped.iterrows():
        values = {present[col]: round(float(row[col]), 2) for col in present if float(row[col]) != 0.0}
        out[str(operator_id)] = values
    return out


def _total_by_operator(frame: pd.DataFrame, column: str) -> dict[str, float]:
    if column not in frame.columns or not len(frame):
        return {}
    work = frame[["OPERATOR_ID", column]].copy()
    work[column] = pd.to_numeric(work[column], errors="coerce")
    grouped = work.groupby("OPERATOR_ID", dropna=True)[column].sum()
    return {str(k): round(float(v), 2) for k, v in grouped.items()}


def incident_counts(
    incidents: pd.DataFrame, report_year: int, window: int = INCIDENT_WINDOW_YEARS
) -> tuple[dict[str, dict[str, int]], list[int]]:
    """Per-operator incident counts over the `window` report years ending with `report_year`.

    The window is anchored on the annual report year rather than on the incident file's own
    maximum, because the newest year in the incident file is a partial year (the file is refreshed
    monthly) and counting it would make a five-year count that quietly spans 4.2 years.
    """
    years = list(range(report_year - window + 1, report_year + 1))
    subset = incidents[incidents["IYEAR"].isin(years)]
    out: dict[str, dict[str, int]] = {}
    if not len(subset):
        return out, years

    def yes(group: pd.DataFrame, column: str) -> int:
        """How many of the group's rows carry PHMSA's `YES` in a flag column it may not have."""
        series = group[column] if column in group.columns else pd.Series(dtype=str)
        return int((series.astype(str).str.upper() == "YES").sum())

    for operator_id, group in subset.groupby("OPERATOR_ID", dropna=True):
        out[str(operator_id)] = {
            "incidents_5y_total": int(group["REPORT_NUMBER"].nunique()),
            "incidents_5y_with_fatality": yes(group, "FATALITY_IND"),
            "incidents_5y_with_injury": yes(group, "INJURY_IND"),
            "incidents_5y_with_ignition": yes(group, "IGNITE_IND"),
            "incidents_5y_with_explosion": yes(group, "EXPLODE_IND"),
        }
    return out, years


#: Recorded on every row: PHMSA publishes the significant-incident flag in a separate file this
#: environment cannot reach (module docstring), so the key stays null rather than being guessed.
SIGNIFICANT_UNAVAILABLE = (
    "incidents_5y_significant unavailable: PHMSA's significant flag is published in "
    "PHMSA_Pipeline_Safety_Flagged_Incidents.zip, which could not be retrieved; the public "
    "incident file carries neither the flag nor the 1984-dollar cost its definition uses"
)

COLUMNS: list[str] = [
    "source_id",
    "source_url",
    "incident_source_url",
    "retrieved_at",
    "licence",
    "licence_id",
    "operator_id",
    "operator_name",
    "report_year",
    "datafile_as_of",
    "onshore_transmission_miles",
    "total_transmission_miles",
    "miles_by_decade",
    "miles_by_diameter",
    "incidents_5y_total",
    "incidents_5y_significant",
    "incidents_5y_with_fatality",
    "incidents_5y_with_injury",
    "incidents_5y_with_ignition",
    "incidents_5y_with_explosion",
    "incident_years",
    "feature_flags",
]


def build_rows(
    annual: AnnualTables,
    incidents: pd.DataFrame | None,
    *,
    retrieved_at: str,
    source_url: str = ANNUAL_URL,
    incident_source_url: str = INCIDENT_URL,
    licence_id: str = "",
) -> pd.DataFrame:
    """One row per PHMSA operator in the annual file. Operators that filed a report but no
    transmission mileage (gathering-only filers) are kept with zero miles -- the registry states
    them, and dropping them would hide a real "no transmission miles" answer."""
    names: dict[str, str] = {}
    identity = annual.identity
    if "REPORT_NUMBER" in identity:
        identity = identity.sort_values("REPORT_NUMBER")
    for operator_id, group in identity.groupby("OPERATOR_ID", dropna=True):
        name = fuels.clean_str(group["PARTA2NAMEOFCOMP"].iloc[0])
        if name:
            names[str(operator_id)] = name

    onshore = _total_by_operator(annual.class_miles, CLASS_MILES_ONSHORE)
    total = _total_by_operator(annual.class_miles, CLASS_MILES_TOTAL)
    decades = _sum_by_operator(annual.decade, DECADE_COLUMNS)
    diameters = _sum_by_operator(annual.diameter, DIAMETER_COLUMNS)
    counts, years = incident_counts(incidents, annual.report_year) if incidents is not None else ({}, [])

    rows: list[dict[str, Any]] = []
    for operator_id, name in sorted(names.items()):
        flags = [SIGNIFICANT_UNAVAILABLE]
        if incidents is None:
            flags.append("incident file not read on this run; incident counts are null")
        counted = counts.get(operator_id)
        rows.append(
            {
                "source_id": SOURCE_ID,
                "source_url": source_url,
                "incident_source_url": incident_source_url,
                "retrieved_at": retrieved_at,
                "licence": fuels.LICENCE_CLASS,
                "licence_id": licence_id,
                "operator_id": operator_id,
                "operator_name": name,
                "report_year": annual.report_year,
                "datafile_as_of": annual.datafile_as_of,
                "onshore_transmission_miles": onshore.get(operator_id, 0.0),
                "total_transmission_miles": total.get(operator_id, 0.0),
                "miles_by_decade": decades.get(operator_id, {}),
                "miles_by_diameter": diameters.get(operator_id, {}),
                "incidents_5y_total": (counted or {}).get("incidents_5y_total", 0)
                if incidents is not None
                else None,
                "incidents_5y_significant": None,
                "incidents_5y_with_fatality": (counted or {}).get("incidents_5y_with_fatality", 0)
                if incidents is not None
                else None,
                "incidents_5y_with_injury": (counted or {}).get("incidents_5y_with_injury", 0)
                if incidents is not None
                else None,
                "incidents_5y_with_ignition": (counted or {}).get("incidents_5y_with_ignition", 0)
                if incidents is not None
                else None,
                "incidents_5y_with_explosion": (counted or {}).get("incidents_5y_with_explosion", 0)
                if incidents is not None
                else None,
                "incident_years": years,
                "feature_flags": flags,
            }
        )
    frame = pd.DataFrame(rows, columns=COLUMNS)
    if len(frame) and frame["operator_id"].duplicated().any():
        dups = sorted(frame.loc[frame["operator_id"].duplicated(), "operator_id"].unique())
        raise ValueError(f"duplicate operator_id: {dups[:5]}")
    return frame


def default_output() -> pathlib.Path:
    return fuels.CONTEXT_DIR / f"{SOURCE_ID}.parquet"


# ----------------------------------------------------------------------------------------- run
def snapshot_provenance(
    path: pathlib.Path, *, fallback_url: str, store: Store | None = None
) -> tuple[str, str]:
    """`(retrieved_at, source_url)` for a recorded snapshot of this source.

    `pipeline.context.fuels.snapshot_metadata` cannot be used here: this connector stores two
    datasets per run and names their snapshots `<token>-<dataset>`, which its `<token>` pattern
    does not match, so it would stamp a re-parse with "now" and quietly claim the run had fetched
    the bytes today. The run record is the authority -- it carries both the retrieval time and the
    PHMSA URL the bytes belong to -- and the token is the fallback.
    """
    token = path.stem
    st = store or Store()
    run_path = st.run_path(SOURCE_ID, token)
    if run_path.exists():
        try:
            record = json.loads(run_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            record = {}
        snapshot = record.get("snapshot") or {}
        retrieved_at = str(record.get("retrieved_at") or "") or None
        source_url = str(snapshot.get("source_url") or "") or fallback_url
        if retrieved_at:
            return retrieved_at, source_url
    stamp = token.split("-", 1)[0]
    if _TOKEN_RE.match(stamp):
        parsed = dt.datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(tzinfo=dt.UTC)
        return fuels.iso(parsed), fallback_url
    # Not one of this store's snapshots (a hand-downloaded file, a test fixture): the bytes are
    # real but their retrieval time is unknown, so record the read time rather than inventing one.
    return fuels.iso(fuels.utc_now()), fallback_url


def run(
    *,
    annual_snapshot: pathlib.Path | None = None,
    incident_snapshot: pathlib.Path | None = None,
    out: pathlib.Path | None = None,
    store: Store | None = None,
    manifest: pathlib.Path | None = None,
) -> dict[str, Any]:
    t0 = time.monotonic()
    entry = fuels.entry_for(SOURCE_ID, manifest)
    store = store or Store()
    summary: dict[str, Any] = {"source_id": SOURCE_ID}
    session: PoliteSession | None = None
    annual_meta: dict[str, Any] = {}

    if annual_snapshot is not None:
        annual_bytes = annual_snapshot.read_bytes()
        annual_meta = {"annual_snapshot": str(annual_snapshot)}
        retrieved_at, source_url = snapshot_provenance(annual_snapshot, fallback_url=ANNUAL_URL, store=store)
    else:
        session = polite_session()
        fetched = fetch("annual", session=session, store=store)
        annual_bytes = fetched.content
        retrieved_at, source_url = fetched.retrieved_at, fetched.source_url
        annual_meta = {
            "annual_fetched_url": fetched.fetched_url,
            "annual_via": fetched.via,
            "annual_bytes": fetched.bytes_,
            "annual_sha256": fetched.sha256,
            "annual_origin_result": fetched.origin_result,
            "annual_upstream_last_modified": fetched.upstream_last_modified,
        }

    incident_frame: pd.DataFrame | None = None
    incident_meta: dict[str, Any] = {}
    incident_url = INCIDENT_URL
    if incident_snapshot is not None:
        incident_frame = parse_incidents(incident_snapshot.read_bytes())
        _, incident_url = snapshot_provenance(incident_snapshot, fallback_url=INCIDENT_URL, store=store)
        incident_meta = {"incident_snapshot": str(incident_snapshot)}
    else:
        session = session or polite_session()
        try:
            fetched_inc = fetch("incident", session=session, store=store)
            incident_frame = parse_incidents(fetched_inc.content)
            incident_url = fetched_inc.source_url
            incident_meta = {
                "incident_fetched_url": fetched_inc.fetched_url,
                "incident_via": fetched_inc.via,
                "incident_bytes": fetched_inc.bytes_,
                "incident_sha256": fetched_inc.sha256,
                "incident_origin_result": fetched_inc.origin_result,
            }
        except (HttpBlocked, HttpFailed, ParseError) as e:
            # The mileage features do not depend on the incident file; a run that cannot read it
            # writes null incident counts and says so, rather than writing nothing at all.
            incident_meta = {"incident_error": f"{type(e).__name__}: {e}"}

    annual = parse_annual(annual_bytes)
    frame = build_rows(
        annual,
        incident_frame,
        retrieved_at=retrieved_at,
        source_url=source_url,
        incident_source_url=incident_url,
        licence_id=entry.licence_id,
    )
    out = out or default_output()
    fuels.write_context_parquet(frame, out)

    summary.update(
        {
            "report_year": annual.report_year,
            "annual_member": annual.member,
            "damaged_members": annual.damaged_members,
            "datafile_as_of": annual.datafile_as_of,
            "operators": len(frame),
            "operators_with_transmission_miles": int((frame["onshore_transmission_miles"] > 0).sum()),
            "onshore_transmission_miles_total": round(float(frame["onshore_transmission_miles"].sum()), 1),
            "incident_rows": len(incident_frame) if incident_frame is not None else None,
            "operators_with_incidents": (frame["incidents_5y_total"].fillna(0) > 0).sum()
            if incident_frame is not None
            else 0,
            "incident_years": frame["incident_years"].iloc[0] if len(frame) else [],
            "retrieved_at": retrieved_at,
            "out": str(out),
            "parquet_bytes": out.stat().st_size,
            "elapsed_s": round(time.monotonic() - t0, 2),
            **annual_meta,
            **incident_meta,
        }
    )
    return summary


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--annual-snapshot", type=pathlib.Path, help="Parse this recorded zip")
    parser.add_argument("--incident-snapshot", type=pathlib.Path, help="Parse this recorded zip")
    parser.add_argument("--out", type=pathlib.Path)
    args = parser.parse_args(argv)
    summary = run(
        annual_snapshot=args.annual_snapshot,
        incident_snapshot=args.incident_snapshot,
        out=args.out,
    )
    # numpy scalars reach the summary from pandas aggregates; `json_default` is the same
    # coercion the run records use (`pipeline.connectors.store`).
    print(json.dumps(summary, default=json_default))  # noqa: T201 — CLI summary line


if __name__ == "__main__":
    main()
