"""us.epa.ghgrp — EPA Greenhouse Gas Reporting Program facility register (Envirofacts `pub_dim_facility`).

Route (measured 2026-09-25, and the brief's preference for the bulk zip is corrected here). EPA's
"Data Summary Spreadsheets" zip (`2023_data_summary_spreadsheets.zip`, 28,389,973 bytes, sha256
`895349c8…7bd5`, one `ghgp_data_<year>.xlsx` per reporting year 2010–2023) carries emissions
totals by gas and by process, but **none of the fields this connector exists for**: no
`parent_company`, no `co2_captured`, no `rr_mrv_plan_url`, no `year` column (one file per year).
Its "Direct Point Emitters" sheet holds 6,470 facilities for 2023 against the register's 11,281,
because suppliers, onshore production basins and facilities that have since stopped reporting sit
on other sheets or nowhere. The ownership strings live in a separate `ghgp_data_parent_company.xlsb`
and in the Envirofacts `pub_dim_facility` table. So the route is the Envirofacts REST table, which
is also cheaper: one reporting year is 11,281 rows in **two** row windows of 10,000
(`.../pub_dim_facility/year/<y>/rows/0:9999/CSV`, 3,094,060 bytes, and `rows/10000:19999/CSV`,
~400 KB), against 28 MB for the zip. The CSV output is used rather than JSON (12.3 MB for the
same rows); CSV fidelity against JSON was checked column by column — the only differences are
numeric formatting (`48.828707000000000` vs `48.828707`), which `parse` normalises.

Fetch: (1) find the newest reporting year by probing `pub_dim_facility/year/<y>/count/JSON` from
this year downwards (RY2024 answered 0 rows on 2026-09-25 and the data-sets page links only the
2023 zip, so 2023 is the newest); (2) page that year in 10,000-row windows until a short window.
`honour_robots` is False because this is an API endpoint (`pipeline/connectors/http.py`), and
because `https://data.epa.gov/robots.txt` answers HTTP 200 with the string `"Welcome to
data.epa.gov!"` — not a robots file. Politeness is the manifest's 0.5 rps.

Parse: the CSV windows are concatenated (header kept once) and read with every column as text.
`year` is a full-register annual snapshot, so `snapshot_mode` stays `full`: a facility that leaves
the newest year's register is a `removed` event on the next annual run.

Personal data (docs/13 §5; CLAUDE.md "store the minimum personal data"). All 37 columns of
`pub_dim_facility` were enumerated (`PUB_DIM_FACILITY_COLUMNS`). None is a designated-representative
or contact field. Three are free text where a person could be named — `address1` (18 of 10,269
non-empty 2023 values carry an `Attn:` or `c/o` line; on 2026-09-25 every one named a department or
a company, none a person), `address2` and `comments` (both empty in 2023). They are not needed
(latitude/longitude, city, county FIPS and state place the facility) and are stripped by `redact()`
before the snapshot is stored and excluded from `parse()` — `PERSONAL_DATA_COLUMNS`, tested. The
`parent_company` string is kept: it is the ownership fact this connector exists to record, and it
occasionally names a natural person as an owner (one 2023 row lists an individual at 7.89 %), the
same way EIA-860 Schedule 4 lists individual owners. The bulk zip's sheets were enumerated too
(`Facility Id … Does the facility employ continuous emissions monitoring?`); none names a person.

`parent_company` grammar, measured on the 11,281 RY2023 rows: 11,151 rows are `NAME (pct%); NAME
(pct%)…` with every share stated (11,118 sum to 100 ± 0.5, **33 do not** — e.g. `75.7`, `98.88`),
93 rows carry a parent with an empty share (`US GOVERNMENT (%)`), 36 are empty, 1 mixes the two
(`Garland Power & Light; City of Garland (100%);`), and the longest string lists 119 parents.
`parse_parent_company` keeps shares exactly as stated and `share_flag` says which case a row is;
nothing is ever rescaled (docs/24 §7 / `services/ingest/ownership.py` rule).

kind: `"document"`, the least-wrong existing `Kind`, for the same reason `us.eia.860` uses it: a
facility-year row is an annual register snapshot, not a proposal — no lifecycle, `lifecycle_state`
held at `filed`. The consumer-facing frame is `build_facilities()` (FACILITY_COLUMNS), written by
`pipeline/context/ghgrp.py` to `data/normalized/context/us.epa.ghgrp.parquet`; the framework's
document frame exists for the snapshot-diff and DQ machinery. `pipeline.normalize.classify_tech` is
never called: emitters are context rows and owner edges, not proposals of any `kind`.

source_record_id: `<facility_id>-<year>` (EPA's own facility id, unique per year: 11,281 distinct
ids in 11,281 RY2023 rows). `frs_id` is not the key — 1,738 rows have none.
Reuse: US federal government work, public domain (17 U.S.C. §105, EPA hedge at docs/13 §2.12).
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import re
import time
from typing import Any, ClassVar

import pandas as pd

from pipeline.connectors.base import Connector as BaseConnector
from pipeline.connectors.base import ConnectorError, Kind, ParseError, RawSnapshot

EF_BASE = "https://data.epa.gov/efservice"
TABLE = "pub_dim_facility"
#: Envirofacts answers at most 10,000 rows per `rows/<a>:<b>` window.
WINDOW = 10_000
#: 136,005 facility-years across all years on 2026-09-25; one year is ~11k rows. A guard, not a target.
MAX_WINDOWS = 20
#: Reporting years start at 2010; the newest year is found by probing downwards from this year.
FIRST_REPORTING_YEAR = 2010
#: FLIGHT is the map front end; the row's own REST address is the citable one.
ROW_URL = EF_BASE + "/" + TABLE + "/facility_id/{facility_id}/year/{year}/JSON"

#: Every column `pub_dim_facility` returned on 2026-09-25, in the API's own order.
PUB_DIM_FACILITY_COLUMNS: tuple[str, ...] = (
    "facility_id",
    "latitude",
    "longitude",
    "city",
    "state",
    "zip",
    "county_fips",
    "county",
    "address1",
    "address2",
    "facility_name",
    "state_name",
    "naics_code",
    "year",
    "bamm_used_desc",
    "emission_classification_code",
    "program_name",
    "program_sys_id",
    "frs_id",
    "cems_used",
    "co2_captured",
    "reported_subparts",
    "bamm_approved",
    "emitted_co2_supplied",
    "tribal_land_id",
    "eggrt_facility_id",
    "parent_company",
    "reported_industry_types",
    "facility_types",
    "submission_id",
    "uu_rd_exempt",
    "reporting_status",
    "process_stationary_cml",
    "comments",
    "rr_mrv_plan_url",
    "rr_monitoring_plan_filename",
    "rr_monitoring_plan",
)
#: Free-text columns where a person could be named; never stored, never parsed (module docstring).
PERSONAL_DATA_COLUMNS: tuple[str, ...] = ("address1", "address2", "comments")
REQUIRED_COLUMNS: tuple[str, ...] = (
    "facility_id",
    "facility_name",
    "state",
    "latitude",
    "longitude",
    "year",
    "parent_company",
)

#: Consumer frame (`pipeline/context/ghgrp.py` writes it; `services/ingest/ghgrp.py` reads it).
FACILITY_COLUMNS: list[str] = [
    "source_id",
    "source_url",
    "retrieved_at",
    "licence",
    "licence_id",
    "ghgrp_facility_id",
    "frs_id",
    "name",
    "reporting_year",
    "lon",
    "lat",
    "city",
    "state_code",
    "county_name",
    "county_fips",
    "naics_code",
    "facility_types",
    "reported_subparts",
    "subparts",
    "subpart_rr",
    "subpart_uu",
    "subpart_pp",
    "reporting_status",
    "co2_captured",
    "rr_mrv_plan_url",
    "parent_company_raw",
    "parents",
    "parent_count",
    "share_sum_pct",
    "share_flag",
]
SHARE_FLAGS = ("ok", "not_100", "partial", "unstated", "none")
#: A stated set of shares is "ok" within this many percentage points of 100.
SHARE_TOLERANCE_PCT = 0.5

#: `reported_subparts` spells a subpart with an optional qualifier: `C,PP,RR (RPT),W` — the
#: qualifier (`(RPT)` reported, `(Abbr)` abbreviated) is dropped, the letters are the membership.
_SUBPART_RE = re.compile(r"^([A-Z]{1,2})\b")
_PARENT_RE = re.compile(r"^(?P<name>.*?)\s*\(\s*(?P<pct>\d+(?:\.\d+)?)?\s*%\s*\)\s*$")
_WORLD_LAT = (-90.0, 90.0)
_WORLD_LON = (-180.0, 180.0)


def count_url(year: int) -> str:
    return f"{EF_BASE}/{TABLE}/year/{year}/count/JSON"


def window_url(year: int, start: int, size: int | None = None) -> str:
    size = WINDOW if size is None else size
    return f"{EF_BASE}/{TABLE}/year/{year}/rows/{start}:{start + size - 1}/CSV"


def parse_parent_company(text: Any) -> list[dict[str, Any]]:
    """`NAME (pct%); NAME (pct%)` -> `[{"name", "share_pct"}, …]`, shares exactly as stated.

    A parent with an empty or missing share (`US GOVERNMENT (%)`, or a bare `NAME`) gets
    `share_pct: None`. Empty parts (a trailing `;`) are dropped. Never rescaled.
    """
    if text is None or (isinstance(text, float) and pd.isna(text)):
        return []
    out: list[dict[str, Any]] = []
    for part in str(text).split(";"):
        part = part.strip()
        if not part:
            continue
        m = _PARENT_RE.match(part)
        if m:
            name = m.group("name").strip()
            pct = m.group("pct")
            out.append({"name": name, "share_pct": float(pct) if pct else None})
        else:
            out.append({"name": part, "share_pct": None})
    return [p for p in out if p["name"]]


def subparts_list(text: Any) -> list[str]:
    """`C,PP,RR (RPT),W` -> `["C", "PP", "RR", "W"]` (qualifiers dropped, order kept, no repeats)."""
    if text is None or (isinstance(text, float) and pd.isna(text)):
        return []
    out: list[str] = []
    for part in str(text).split(","):
        m = _SUBPART_RE.match(part.strip().upper())
        if m and m.group(1) not in out:
            out.append(m.group(1))
    return out


def share_summary(parents: list[dict[str, Any]]) -> tuple[float | None, str]:
    """`(share_sum_pct, share_flag)` for a parsed parent list (`SHARE_FLAGS`)."""
    if not parents:
        return None, "none"
    stated = [p["share_pct"] for p in parents if p["share_pct"] is not None]
    if not stated:
        return None, "unstated"
    total = round(float(sum(stated)), 3)
    if len(stated) < len(parents):
        return total, "partial"
    return total, "ok" if abs(total - 100.0) <= SHARE_TOLERANCE_PCT else "not_100"


def _text(v: Any) -> str | None:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    s = str(v).strip()
    return s or None


def _number_text(v: Any) -> str | None:
    """`521939.0000000000` -> `521939`; a non-numeric value is returned as trimmed text."""
    s = _text(v)
    if s is None:
        return None
    try:
        f = float(s)
    except ValueError:
        return s
    return str(int(f)) if f.is_integer() else repr(f)


def _coord(lat_raw: Any, lon_raw: Any) -> tuple[float | None, float | None]:
    """`(lon, lat)` for a numeric, in-bounds, non-`(0, 0)` pair — the `fuels.valid_point` rule."""
    try:
        lat, lon = float(str(lat_raw)), float(str(lon_raw))
    except (TypeError, ValueError):
        return None, None
    if (lat == 0.0 and lon == 0.0) or not (_WORLD_LAT[0] <= lat <= _WORLD_LAT[1]):
        return None, None
    if not (_WORLD_LON[0] <= lon <= _WORLD_LON[1]):
        return None, None
    return lon, lat


def _state_code(v: Any) -> str | None:
    s = _text(v)
    return f"US-{s.upper()}" if s and re.fullmatch(r"[A-Za-z]{2}", s) else None


def _year(v: Any) -> int | None:
    s = _number_text(v)
    return int(s) if s and s.isdigit() else None


def strip_columns(content: bytes, columns: tuple[str, ...]) -> bytes:
    """The CSV payload without `columns`, byte-for-byte otherwise (rows and order kept)."""
    text = content.decode("utf-8")
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        return content
    drop = {i for i, c in enumerate(header) if c.strip() in columns}
    if not drop:
        return content
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow([c for i, c in enumerate(header) if i not in drop])
    for row in reader:
        writer.writerow([c for i, c in enumerate(row) if i not in drop])
    return out.getvalue().encode("utf-8")


def build_facilities(
    rows: list[dict[str, Any]],
    *,
    retrieved_at: str,
    licence_id: str,
    source_id: str = "us.epa.ghgrp",
    licence: str = "public-domain",
) -> pd.DataFrame:
    """One row per facility-year (`FACILITY_COLUMNS`) from `parse()`'s rows."""
    out: list[dict[str, Any]] = []
    for r in rows:
        parents = parse_parent_company(r.get("parent_company"))
        share_sum, flag = share_summary(parents)
        lon, lat = _coord(r.get("latitude"), r.get("longitude"))
        facility_id = _number_text(r.get("facility_id"))
        year = _year(r.get("year"))
        captured = _text(r.get("co2_captured"))
        subparts = subparts_list(r.get("reported_subparts"))
        out.append(
            {
                "source_id": source_id,
                "source_url": ROW_URL.format(facility_id=facility_id, year=year),
                "retrieved_at": retrieved_at,
                "licence": licence,
                "licence_id": licence_id,
                "ghgrp_facility_id": facility_id,
                "frs_id": _number_text(r.get("frs_id")),
                "name": _text(r.get("facility_name")),
                "reporting_year": year,
                "lon": lon,
                "lat": lat,
                "city": _text(r.get("city")),
                "state_code": _state_code(r.get("state")),
                "county_name": _text(r.get("county")),
                "county_fips": _number_text(r.get("county_fips")),
                "naics_code": _number_text(r.get("naics_code")),
                "facility_types": _text(r.get("facility_types")),
                "reported_subparts": _text(r.get("reported_subparts")),
                "subparts": subparts,
                # Membership only (coordinator, 2026-09-25): RR reporters are 8 dedicated
                # sequestration sites and 12 EOR fields under MRV plans, so RR never decides an
                # asset type on its own; the quantities live on the summary zip's sheets.
                "subpart_rr": "RR" in subparts,
                "subpart_uu": "UU" in subparts,
                "subpart_pp": "PP" in subparts,
                "reporting_status": _text(r.get("reporting_status")),
                # EPA states `Y` or nothing; nothing is "not stated", not False.
                "co2_captured": True if captured and captured.upper() == "Y" else None,
                "rr_mrv_plan_url": _text(r.get("rr_mrv_plan_url")),
                "parent_company_raw": _text(r.get("parent_company")),
                "parents": parents,
                "parent_count": len(parents),
                "share_sum_pct": share_sum,
                "share_flag": flag,
            }
        )
    df = pd.DataFrame(out, columns=FACILITY_COLUMNS)
    df["reporting_year"] = df["reporting_year"].astype("Int64")
    df["parent_count"] = df["parent_count"].astype("Int64")
    df["share_sum_pct"] = df["share_sum_pct"].astype("Float64")
    return df


class Connector(BaseConnector):
    source_id: ClassVar[str] = "us.epa.ghgrp"
    kind: ClassVar[Kind] = "document"  # no "context" kind exists yet -- see module docstring
    ext: ClassVar[str] = "csv"
    honour_robots: ClassVar[bool] = False  # API endpoint; data.epa.gov has no robots file (docstring)
    personal_data_columns: ClassVar[tuple[str, ...]] = PERSONAL_DATA_COLUMNS
    dq_required_fields: ClassVar[tuple[str, ...]] = ("title", "project_name_hint", "state_hint")
    key_source_columns: ClassVar[tuple[str, ...]] = (
        "facility_id",
        "facility_name",
        "state",
        "latitude",
        "longitude",
        "naics_code",
        "year",
        "parent_company",
        "frs_id",
        "co2_captured",
        "rr_mrv_plan_url",
    )
    #: Pin a reporting year (backfills, tests); None discovers the newest one.
    reporting_year: ClassVar[int | None] = None

    # ------------------------------------------------------------------ fetch
    def _count(self, year: int) -> int:
        r = self.http.get(count_url(year), honour_robots=self.honour_robots)
        if r.status_code != 200:
            raise ConnectorError(f"GET {count_url(year)} -> HTTP {r.status_code}")
        try:
            payload = r.json()
            return int(payload[0]["TOTALQUERYRESULTS"])
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ConnectorError(f"{count_url(year)} answered no count: {r.text[:120]!r}") from exc

    def discover_year(self, now: dt.datetime | None = None) -> tuple[int, int, list[dict[str, int]]]:
        """`(year, row_count, probes)`: the newest reporting year with rows, probing downwards."""
        this_year = (now or dt.datetime.now(dt.UTC)).year
        probes: list[dict[str, int]] = []
        if self.reporting_year is not None:
            n = self._count(self.reporting_year)
            probes.append({"year": self.reporting_year, "count": n})
            if n <= 0:
                raise ConnectorError(f"{TABLE} has no rows for reporting year {self.reporting_year}")
            return self.reporting_year, n, probes
        for year in range(this_year, FIRST_REPORTING_YEAR - 1, -1):
            n = self._count(year)
            probes.append({"year": year, "count": n})
            if n > 0:
                return year, n, probes
        raise ConnectorError(f"{TABLE} answered 0 rows for every year {this_year}..{FIRST_REPORTING_YEAR}")

    def fetch(self) -> RawSnapshot:
        t0 = time.monotonic()
        year, expected, probes = self.discover_year()
        parts: list[bytes] = []
        windows: list[dict[str, Any]] = []
        header: bytes | None = None
        total = 0
        for i in range(MAX_WINDOWS):
            url = window_url(year, i * WINDOW)
            r = self.http.get(url, honour_robots=self.honour_robots, timeout=300)
            if r.status_code != 200:
                raise ConnectorError(f"GET {url} -> HTTP {r.status_code}")
            body = r.content
            if not body.strip() or (header is not None and body.strip() == header.strip()):
                # A year with an exact multiple of 10,000 rows: the window past the end is empty.
                windows.append({"url": url, "bytes": len(body), "rows": 0})
                break
            first_nl = body.find(b"\n")
            this_header = body[: first_nl + 1] if first_nl >= 0 else body
            if header is None:
                header = this_header
                if not this_header.strip().startswith(b"facility_id"):
                    raise ConnectorError(
                        f"{url} answered something other than the {TABLE} CSV: {body[:80]!r}"
                    )
                parts.append(body)
            elif this_header != header:
                raise ConnectorError(f"{url} changed the CSV header mid-run")
            else:
                parts.append(body[first_nl + 1 :])
            n_rows = max(len(body.splitlines()) - 1, 0)
            total += n_rows
            windows.append({"url": url, "bytes": len(body), "rows": n_rows})
            if n_rows < WINDOW:
                break
        else:
            raise ConnectorError(f"{TABLE} year {year} did not end within {MAX_WINDOWS} windows")
        content = b"".join(parts)
        return RawSnapshot(
            content=content,
            content_type="text/csv",
            url=window_url(year, 0),
            retrieved_at=dt.datetime.now(dt.UTC),
            http_status=200,
            ext="csv",
            elapsed_s=round(time.monotonic() - t0, 2),
            requests_made=len(probes) + len(windows),
            meta={
                "table": TABLE,
                "reporting_year": year,
                "count_probes": probes,
                "expected_rows": expected,
                "rows_fetched_estimate": total,
                "windows": windows,
            },
        )

    def redact(self, content: bytes) -> bytes:
        """The three free-text columns never reach the snapshot store (module docstring)."""
        return strip_columns(content, self.personal_data_columns)

    # ------------------------------------------------------------------ parse
    def parse(self, raw: RawSnapshot) -> list[dict[str, Any]]:
        try:
            df = pd.read_csv(io.BytesIO(raw.content), dtype=str, keep_default_na=False)
        except (UnicodeDecodeError, pd.errors.ParserError, pd.errors.EmptyDataError) as exc:
            raise ParseError(f"{raw.url} is not the {TABLE} CSV: {exc}") from exc
        df.columns = [str(c).strip() for c in df.columns]
        missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
        if missing:
            raise ParseError(f"{TABLE} layout changed: missing {missing}; have {list(df.columns)[:8]}")
        df = df.drop(columns=[c for c in self.personal_data_columns if c in df.columns])
        df = df[df["facility_id"].str.strip() != ""]
        rows: list[dict[str, Any]] = []
        for rec in df.to_dict("records"):
            rows.append({str(k): _text(v) for k, v in rec.items()})
        if not rows:
            raise ParseError(f"{raw.url} carried a header but no facility rows")
        return rows

    def normalize(self, rows: list[dict[str, Any]], raw: RawSnapshot) -> pd.DataFrame:
        facility_ids = [_number_text(r.get("facility_id")) for r in rows]
        years = [_year(r.get("year")) for r in rows]
        df = pd.DataFrame(
            {
                "source_record_id": [f"{f}-{y}" for f, y in zip(facility_ids, years, strict=True)],
                "source_url": [
                    ROW_URL.format(facility_id=f, year=y) for f, y in zip(facility_ids, years, strict=True)
                ],
                "doc_type": "ghgrp_facility_year",
                "title": [
                    f"GHGRP facility {f}, reporting year {y}: {_text(r.get('facility_name')) or ''}".strip()
                    for f, y, r in zip(facility_ids, years, rows, strict=True)
                ],
                "published_date": None,
                "accession_number": None,
                "docket_refs": None,
                "filer": [_text(r.get("parent_company")) for r in rows],
                "affiliations": None,
                "document_class": "register",
                "document_type": "facility_year",
                "project_name_hint": [_text(r.get("facility_name")) for r in rows],
                "state_hint": [_state_code(r.get("state")) for r in rows],
                "identifiers": [
                    {
                        "ghgrp_facility_id": f,
                        "frs_id": _number_text(r.get("frs_id")),
                        "reporting_year": y,
                        "naics_code": _number_text(r.get("naics_code")),
                    }
                    for f, y, r in zip(facility_ids, years, rows, strict=True)
                ],
                "lifecycle_state": "filed",
                "status_raw": [_text(r.get("reporting_status")) for r in rows],
                "status_rule": None,
                "capacity_mw": pd.array([None] * len(rows), dtype="Float64"),
                "proposed_cod": pd.NaT,
            }
        )
        return self.finalize(df, rows, raw)
