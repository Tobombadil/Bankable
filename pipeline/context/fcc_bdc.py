"""FCC Broadband Data Collection (BDC) -> fiber availability by US county (owner decision
2026-09-28: "fiber availability by area, not fiber routes").

What this layer is, and what it is not
--------------------------------------
One row per US county: the share of units in broadband-serviceable locations (BSLs) where at least
one provider reports **fiber-to-the-premises** service available (BDC technology code 50, labelled
``Fiber`` in the FCC's summary files), the FCC's own count of those units, and the BDC filing the
numbers come from ("December 2025"). It is a proxy for *fiber presence in an area* -- homes and
businesses a provider says it can connect within 10 days at a standard installation
charge. It says nothing about backbone or middle-mile routes, route capacity, dark fibre, the
distance from a site to a long-haul line or carrier hotel, or what a data centre could buy. No
route geometry is fetched, stored or derived here, by design: no open, current route-level
dataset exists, and precise routes are security-sensitive.

Where the numbers come from
---------------------------
The FCC publishes pre-aggregated **"Summary by Geography Type"** files beside the location-level
availability data on the National Broadband Map. The national file
``bdc_us_fixed_broadband_summary_by_geography_<J|D><yy>_<ddmonyyyy>.csv`` carries, per geography
(National, State, County, CBSA, Congressional District, Tribal area), per area type (Total,
Urban, Rural, Tribal, Nontribal), per ``biz_res`` (R residential / B business) and per technology
group (``Any Technology``, ``Fiber``, ``Cable``, ``Copper``, ``Licensed Fixed Wireless``, ...), the
``total_units`` in BSLs and the share of those units with service at each speed tier
(``speed_02_02`` ... ``speed_1000_100``). The fiber share is ``speed_02_02`` on the ``Fiber`` row:
the lowest tier, i.e. any fiber offering at all. Reading this file is one ~600k-row CSV per filing
instead of aggregating tens of millions of location x provider rows (and it never touches the
CostQuest-licensed Fabric, which the FCC does not publish at location level).

Access (measured 2026-09-28)
----------------------------
Every HTML page on ``broadbandmap.fcc.gov`` and ``www.fcc.gov`` -- including ``robots.txt`` --
answers ``HTTP 403 Access Denied`` (Akamai edge) to this egress, whatever the User-Agent. The only
FCC route that answers is the **Public Data API** (``/api/public/map/...``), and it answers
``401 {"status":"fail","status_code":401,"message":"Unauthorized"}`` without credentials: every
call needs two request headers, ``username`` (the email the FCC account was registered with) and
``hash_value`` (an API token generated under "Manage API Access" after agreeing to the FCC's terms).
So ``fetch`` reads both from the environment (``FCC_BDC_USERNAME``, ``FCC_BDC_API_TOKEN``) and
refuses with :class:`CredentialsMissing` -- naming exactly what the owner must obtain -- when
either is absent. Nothing is hard-coded; the credentials never enter a run record. Use a role
address for the registration, not a person's: it travels in a header on every request.

API sequence (third-party client code; FCC spec PDF unread -- 403 from this egress):
``GET /listAsOfDates`` -> newest ``as_of_date`` with ``data_type == "availability"``;
``GET /downloads/listAvailabilityData/<as_of_date>`` -> the entry whose ``file_name`` is the
national fixed-broadband geography summary; ``GET /downloads/downloadFile/availability/<file_id>``
-> a zip holding the one CSV. Three requests per run, at one request per five seconds.

Vintage
-------
The filing is named by the file itself: ``J25`` = data as of 30 June 2025, ``D25`` = 31 December
2025; the trailing ``15sep2026`` is the FCC's processing/revision date of that file. The vintage is
``2025-12`` / "December 2025" from the file name (cross-checked against the API's ``as_of_date``
when fetched live) and **never** the fetch date (`services/ingest/vintage.py`).

CLI (``python -m pipeline.context.fcc_bdc``): fetch with credentials, or ``--snapshot`` a recorded
zip/CSV; writes ``data/normalized/context/us.fcc.bdc.fixed_summary.parquet`` and prints one JSON
summary line including the join coverage against the vendored county polygons that
`pipeline/context/regions.py` produces (``data/vendored/regions/us_counties.geojson``).
"""

from __future__ import annotations

import argparse
import datetime as dt
import io
import json
import os
import pathlib
import re
import time
import zipfile
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from pipeline.connectors.base import ParseError, json_default
from pipeline.connectors.http import HttpFailed, PoliteSession
from pipeline.connectors.store import Store
from pipeline.context import fuels

SOURCE_ID = "us.fcc.bdc.fixed_summary"
LICENCE_CLASS = "public-domain"

HOST = "broadbandmap.fcc.gov"
API_BASE = f"https://{HOST}/api/public/map"
LIST_AS_OF_DATES_URL = f"{API_BASE}/listAsOfDates"
#: The public page a reader follows to the same file (the API URL needs a token, so it is kept in
#: the run record, not on published rows).
DATA_PAGE_URL = f"https://{HOST}/data-download/nationwide-data"
AREA_SUMMARY_URL = f"https://{HOST}/area-summary/fixed"

#: One request per five seconds: three calls per run, so politeness costs nothing. The FCC's own
#: API rate limit is stated in the spec PDF, which this egress cannot read (open item, docs/25 §4).
RATE_LIMITS: dict[str, float] = {HOST: 0.2}

ENV_USERNAME = "FCC_BDC_USERNAME"
ENV_TOKEN = "FCC_BDC_API_TOKEN"  # noqa: S105 -- the variable's name, not a secret

#: ``bdc_us_fixed_broadband_summary_by_geography_D25_15sep2026.csv`` -> filing D25, revision date.
SUMMARY_FILE_RE = re.compile(
    r"bdc_us_fixed_broadband_summary_by_geography_(?P<half>[JD])(?P<yy>\d{2})_(?P<rev>\d{1,2}[a-z]{3}\d{4})",
    re.IGNORECASE,
)

#: The CSV header of the geography summary (identical in the J24, J25 and D25 files quoted in
#: docs/25 §4). Checked on parse so a renamed column fails loudly instead of yielding nulls.
EXPECTED_COLUMNS: tuple[str, ...] = (
    "area_data_type",
    "geography_type",
    "geography_id",
    "geography_desc",
    "geography_desc_full",
    "total_units",
    "biz_res",
    "technology",
    "speed_02_02",
    "speed_10_1",
    "speed_25_3",
    "speed_100_20",
    "speed_250_25",
    "speed_1000_100",
)
SPEED_COLUMNS = EXPECTED_COLUMNS[8:]

#: The technology group the FCC labels ``Fiber`` in its summaries is BDC technology code 50
#: (fiber to the premises). ``Cable/Fiber`` is a union group and must never be read as fiber.
FIBER = "Fiber"
ANY_TECHNOLOGY = "Any Technology"
TECHNOLOGIES_USED = (FIBER, ANY_TECHNOLOGY)
AREA_TYPES_USED = ("Total", "Rural")
COUNTY = "County"

#: Output columns: key and names, metrics, vintage, provenance. `raw` keeps the source rows the
#: metrics were read from (JSON), so every number can be traced to the FCC file's own cells.
COLUMNS: list[str] = [
    "county_fips",
    "county_name",
    "state_code",
    "bsl_units",
    "fiber_share",
    "fiber_share_business",
    "fiber_gigabit_share",
    "served_100_20_share",
    "rural_bsl_units",
    "rural_fiber_share",
    "vintage",
    "vintage_label",
    "as_of_date",
    "file_name",
    "file_revision",
    "fcc_area_url",
    "source_id",
    "source_url",
    "retrieved_at",
    "licence",
    "licence_id",
    "raw",
]


class CredentialsMissing(RuntimeError):
    """The FCC Public Data API needs an account username and an API token; neither is optional."""


# ----------------------------------------------------------------------------------- vintage
@dataclass(frozen=True)
class Filing:
    """The BDC filing a summary file belongs to, read from its own name."""

    file_name: str
    as_of_date: str  # 2025-12-31
    vintage: str  # 2025-12
    vintage_label: str  # December 2025
    revision: str | None  # 2026-09-15: the FCC's processing date of this file, not a vintage
    version_param: str  # dec2025: the map's own `?version=` token

    @property
    def data_page_url(self) -> str:
        return f"{DATA_PAGE_URL}?version={self.version_param}"


def filing_from_file_name(name: str) -> Filing:
    """``..._D25_15sep2026.csv`` -> as of 2025-12-31, vintage "December 2025". Anything that does
    not name a filing is a `ParseError`: a vintage is never guessed or taken from the fetch date."""
    m = SUMMARY_FILE_RE.search(name)
    if m is None:
        raise ParseError(f"{name!r} is not a BDC fixed-broadband geography summary file name")
    year = 2000 + int(m.group("yy"))
    june = m.group("half").upper() == "J"
    as_of = f"{year}-06-30" if june else f"{year}-12-31"
    vintage = f"{year}-06" if june else f"{year}-12"
    label = f"June {year}" if june else f"December {year}"
    revision: str | None = None
    try:
        parsed = dt.datetime.strptime(m.group("rev").lower(), "%d%b%Y").replace(tzinfo=dt.UTC)
        revision = parsed.date().isoformat()
    except ValueError:
        revision = None
    return Filing(
        file_name=name,
        as_of_date=as_of,
        vintage=vintage,
        vintage_label=label,
        revision=revision,
        version_param=f"{'jun' if june else 'dec'}{year}",
    )


# ------------------------------------------------------------------------------------- fetch
@dataclass(frozen=True)
class Credentials:
    username: str
    token: str

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Credentials:
        source = os.environ if env is None else env
        username = (source.get(ENV_USERNAME) or "").strip()
        token = (source.get(ENV_TOKEN) or "").strip()
        missing = [n for n, v in ((ENV_USERNAME, username), (ENV_TOKEN, token)) if not v]
        if missing:
            raise CredentialsMissing(
                f"{', '.join(missing)} not set. The FCC Public Data API refuses every call without "
                "both: register an FCC user account (a role address, not a person's -- it is sent "
                "as the `username` header), sign in to broadbandmap.fcc.gov, open 'Manage API "
                "Access', read and record the terms shown before 'I Agree' (docs/13 §6 row "
                "us.fcc.bdc.fixed_summary), generate a token, and set both variables in the "
                "environment's secret store. Until then run with --snapshot."
            )
        return cls(username=username, token=token)

    def headers(self) -> dict[str, str]:
        return {"username": self.username, "hash_value": self.token}


@dataclass
class FetchResult:
    content: bytes
    filing: Filing
    retrieved_at: str
    fetched_url: str
    file_id: str
    snapshot_path: pathlib.Path | None = None
    meta: dict[str, Any] = field(default_factory=dict)


def polite_session() -> PoliteSession:
    return PoliteSession(rate_limits=RATE_LIMITS, default_rps=0.2)


def list_files_url(as_of_date: str) -> str:
    return f"{API_BASE}/downloads/listAvailabilityData/{as_of_date}"


def download_url(file_id: str) -> str:
    return f"{API_BASE}/downloads/downloadFile/availability/{file_id}"


def _api_json(session: PoliteSession, url: str, creds: Credentials) -> Any:
    # API endpoint, not a site page: robots.txt governs crawlers of pages (and answers 403 here).
    resp = session.get(url, honour_robots=False, headers=creds.headers())
    if resp.status_code != 200:
        raise HttpFailed(f"GET {url} -> HTTP {resp.status_code}: {resp.text[:160]!r}")
    try:
        return resp.json()
    except ValueError as e:
        raise ParseError(f"non-JSON answer from {url}: {resp.text[:160]!r}") from e


def _data_list(payload: Any, url: str) -> list[dict[str, Any]]:
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        raise ParseError(f"{url}: no `data` list in {str(payload)[:160]!r}")
    return [d for d in data if isinstance(d, dict)]


def latest_availability_as_of(dates: list[dict[str, Any]]) -> str:
    """Newest ``as_of_date`` among the availability filings (``2025-12-31``)."""
    values = sorted(
        str(d.get("as_of_date"))[:10]
        for d in dates
        if str(d.get("data_type", "")).lower() == "availability" and d.get("as_of_date")
    )
    if not values:
        raise ParseError("listAsOfDates returned no availability filing")
    return values[-1]


def pick_summary_file(files: list[dict[str, Any]]) -> dict[str, Any]:
    """The national fixed-broadband geography summary among a filing's downloads, selected by its
    file name (stable across J24..D25) rather than by the `subcategory` label. Several revisions
    of one filing -> the newest revision date."""
    candidates: list[tuple[str, dict[str, Any]]] = []
    for f in files:
        name = str(f.get("file_name") or "")
        if not SUMMARY_FILE_RE.search(name):
            continue
        if str(f.get("file_type") or "csv").lower() not in ("csv", "zip"):
            continue
        candidates.append((filing_from_file_name(name).revision or "", f))
    if not candidates:
        raise ParseError("no bdc_us_fixed_broadband_summary_by_geography_* file in this filing's list")
    candidates.sort(key=lambda c: c[0])
    return candidates[-1][1]


def fetch(
    *,
    credentials: Credentials | None = None,
    session: PoliteSession | None = None,
    store: Store | None = None,
    write: bool = True,
) -> FetchResult:
    """Three API calls (module docstring). Raises `CredentialsMissing` before any request when the
    environment has no username/token, so a scheduled run without secrets fails with the reason."""
    creds = credentials or Credentials.from_env()
    session = session or polite_session()
    dates = _data_list(_api_json(session, LIST_AS_OF_DATES_URL, creds), LIST_AS_OF_DATES_URL)
    as_of = latest_availability_as_of(dates)
    files_url = list_files_url(as_of)
    files = _data_list(_api_json(session, files_url, creds), files_url)
    chosen = pick_summary_file(files)
    file_id = str(chosen.get("file_id"))
    url = download_url(file_id)
    resp = session.get(url, honour_robots=False, headers=creds.headers())
    if resp.status_code != 200:
        raise HttpFailed(f"GET {url} -> HTTP {resp.status_code}")
    content: bytes = resp.content
    filing = filing_from_file_name(str(chosen.get("file_name")))
    if filing.as_of_date != as_of:
        raise ParseError(
            f"file {filing.file_name} names filing {filing.as_of_date} but was listed under {as_of}"
        )
    now = fuels.utc_now()
    subcategories = sorted({str(f.get("subcategory")) for f in files if f.get("subcategory")})
    meta = {
        "as_of_date": as_of,
        "file_id": file_id,
        "file_name": filing.file_name,
        "files_listed": len(files),
        # Kept so the run record answers "does the FCC publish an H3 or other summary?" per filing.
        "subcategories": subcategories,
    }
    result = FetchResult(
        content=content,
        filing=filing,
        retrieved_at=fuels.iso(now),
        fetched_url=url,
        file_id=file_id,
        meta=meta,
    )
    if write:
        ext = "zip" if zipfile.is_zipfile(io.BytesIO(content)) else "csv"
        path, _ = fuels.record_snapshot(
            SOURCE_ID,
            content,
            ext=ext,
            fetched_url=url,
            retrieved_at=now,
            requests_made=session.requests_made,
            meta=meta,
            store=store,
        )
        result.snapshot_path = path
    return result


# ------------------------------------------------------------------------------------- parse
def _csv_member(content: bytes) -> tuple[str | None, bytes]:
    if not zipfile.is_zipfile(io.BytesIO(content)):
        return None, content
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        members = [n for n in zf.namelist() if n.lower().endswith(".csv")]
        if len(members) != 1:
            raise ParseError(f"expected one CSV in the summary zip, found {members}")
        return members[0], zf.read(members[0])


def parse(content: bytes, *, file_name: str | None = None) -> pd.DataFrame:
    """Zip or CSV -> the County rows this layer uses (Total and Rural areas; Fiber and Any
    Technology groups), every other geography dropped while reading. `geography_id` stays text
    and is zero-padded to five digits (one third-party copy of the J24 file had lost the leading
    zero). The member name (or `file_name`) names the filing; `frame.attrs["filing"]` carries it."""
    member, body = _csv_member(content)
    name = member or file_name
    if not name:
        raise ParseError("cannot tell the filing: no zip member name and no file_name given")
    filing = filing_from_file_name(pathlib.PurePath(name).name)
    try:
        header = pd.read_csv(io.BytesIO(body), nrows=0).columns.tolist()
    except (ValueError, pd.errors.ParserError) as e:
        raise ParseError(f"unreadable CSV: {e}") from e
    missing = [c for c in EXPECTED_COLUMNS if c not in header]
    if missing:
        raise ParseError(f"summary CSV lacks columns {missing}; header was {header}")
    dtypes: dict[str, Any] = dict.fromkeys((*EXPECTED_COLUMNS[:5], "biz_res", "technology"), "string")
    frame = pd.read_csv(io.BytesIO(body), usecols=list(EXPECTED_COLUMNS), dtype=dtypes)
    rows_in = len(frame)
    keep = (
        (frame["geography_type"] == COUNTY)
        & frame["area_data_type"].isin(AREA_TYPES_USED)
        & frame["technology"].isin(TECHNOLOGIES_USED)
    )
    out = frame.loc[keep].copy()
    out["geography_id"] = out["geography_id"].str.strip().str.zfill(5)
    bad_ids = out.loc[~out["geography_id"].str.fullmatch(r"\d{5}"), "geography_id"]
    if len(bad_ids):
        raise ParseError(f"non-FIPS county ids: {sorted(set(bad_ids))[:5]}")
    for col in ("total_units", *SPEED_COLUMNS):
        out[col] = pd.to_numeric(out[col], errors="raise")
    shares = out[list(SPEED_COLUMNS)]
    if ((shares < 0) | (shares > 1)).any().any():
        raise ParseError("a speed-tier share outside [0, 1]: the file is not in the expected units")
    out = out.reset_index(drop=True)
    out.attrs["filing"] = filing
    out.attrs["rows_in"] = rows_in
    out.attrs["technologies_seen"] = sorted(frame["technology"].dropna().unique().tolist())
    out.attrs["geography_types_seen"] = sorted(frame["geography_type"].dropna().unique().tolist())
    return out


# ---------------------------------------------------------------------------------- metrics
def _state_code(desc_full: Any) -> str | None:
    """``"Autauga County, AL"`` -> ``US-AL`` (the FCC's own postal suffix, not a lookup table)."""
    text = "" if desc_full is None or pd.isna(desc_full) else str(desc_full)
    m = re.search(r",\s*([A-Z]{2})\s*$", text)
    return f"US-{m.group(1)}" if m else None


def _share(value: Any) -> float | None:
    if value is None or pd.isna(value):
        return None
    return round(float(value), 6)


def county_metrics(
    frame: pd.DataFrame, *, retrieved_at: str, source_url: str, licence_id: str
) -> pd.DataFrame:
    """One row per county FIPS (module docstring for each metric's meaning).

    A county with no ``Fiber`` row gets ``fiber_share = None`` (the file did not say), never 0,
    and is counted in ``attrs["counties_without_fiber_row"]``. Duplicate keys are an error."""
    filing: Filing = frame.attrs["filing"]
    key_cols = ["geography_id", "area_data_type", "biz_res", "technology"]
    dup = frame.duplicated(subset=key_cols, keep=False)
    if dup.any():
        sample = frame.loc[dup, key_cols].head(3).to_dict("records")
        raise ParseError(f"duplicate (county, area, biz_res, technology) rows: {sample}")
    cells: dict[tuple[str, str, str, str], dict[str, Any]] = {
        (r["geography_id"], r["area_data_type"], r["biz_res"], r["technology"]): r
        for r in frame.to_dict("records")
    }
    rows: list[dict[str, Any]] = []
    without_fiber = 0
    for fips in sorted(frame["geography_id"].unique()):
        any_r = cells.get((fips, "Total", "R", ANY_TECHNOLOGY))
        fib_r = cells.get((fips, "Total", "R", FIBER))
        fib_b = cells.get((fips, "Total", "B", FIBER))
        rural_any = cells.get((fips, "Rural", "R", ANY_TECHNOLOGY))
        rural_fib = cells.get((fips, "Rural", "R", FIBER))
        base = any_r or fib_r or fib_b
        rural_base = rural_any or rural_fib
        if base is None:
            continue  # Rural-only rows with no Total row: nothing to key a county row on
        if fib_r is None:
            without_fiber += 1
        used = [c for c in (any_r, fib_r, fib_b, rural_any, rural_fib) if c is not None]
        raw = [
            {k: (v if not isinstance(v, float) else round(v, 9)) for k, v in c.items() if not pd.isna(v)}
            for c in used
        ]
        rows.append(
            {
                "county_fips": fips,
                "county_name": base["geography_desc"],
                "state_code": _state_code(base["geography_desc_full"]),
                "bsl_units": int(base["total_units"]),
                "fiber_share": _share(fib_r["speed_02_02"]) if fib_r else None,
                "fiber_share_business": _share(fib_b["speed_02_02"]) if fib_b else None,
                "fiber_gigabit_share": _share(fib_r["speed_1000_100"]) if fib_r else None,
                "served_100_20_share": _share(any_r["speed_100_20"]) if any_r else None,
                "rural_bsl_units": int(rural_base["total_units"]) if rural_base else None,
                "rural_fiber_share": _share(rural_fib["speed_02_02"]) if rural_fib else None,
                "vintage": filing.vintage,
                "vintage_label": filing.vintage_label,
                "as_of_date": filing.as_of_date,
                "file_name": filing.file_name,
                "file_revision": filing.revision,
                "fcc_area_url": f"{AREA_SUMMARY_URL}?type=county&geoid={fips}",
                "source_id": SOURCE_ID,
                "source_url": source_url,
                "retrieved_at": retrieved_at,
                "licence": LICENCE_CLASS,
                "licence_id": licence_id,
                "raw": json.dumps(raw, sort_keys=True, default=json_default),
            }
        )
    out = pd.DataFrame(rows, columns=COLUMNS)
    for col in ("bsl_units", "rural_bsl_units"):
        out[col] = out[col].astype("Int64")
    out.attrs["counties_without_fiber_row"] = without_fiber
    return out


# ---------------------------------------------------------------------------------- summary
def rank_counties(metrics: pd.DataFrame, n: int = 10) -> dict[str, list[dict[str, Any]]]:
    """Top and bottom ``n`` counties by residential fiber share, for the run summary. Ties break
    on unit count (larger first), so a run of 1.0 or 0.0 shares lists the biggest places."""
    known = metrics.loc[metrics["fiber_share"].notna()]
    cols = ["county_fips", "county_name", "state_code", "fiber_share", "bsl_units"]

    def pick(ascending: bool) -> list[dict[str, Any]]:
        ordered = known.sort_values(["fiber_share", "bsl_units"], ascending=[ascending, False])
        return [
            {k: (int(v) if k == "bsl_units" else v) for k, v in r.items()}
            for r in ordered[cols].head(n).to_dict("records")
        ]

    return {"top": pick(False), "bottom": pick(True)}


def unit_weighted_fiber_share(metrics: pd.DataFrame) -> float | None:
    """Sum of county shares x units over sum of units, counties with a Fiber row only. A
    descriptive national figure, not the FCC's own national row (which the file also carries)."""
    known = metrics.loc[metrics["fiber_share"].notna()]
    units = known["bsl_units"].astype("float64")
    total = float(units.sum())
    return round(float((known["fiber_share"] * units).sum()) / total, 4) if total else None


# ------------------------------------------------------------------------------------- join
COUNTIES_GEOJSON = fuels.ROOT / "data" / "vendored" / "regions" / "us_counties.geojson"


def county_region_ids(path: pathlib.Path = COUNTIES_GEOJSON) -> dict[str, str]:
    """``{region_id: name}`` from the vendored Census county polygons (`regions.py` output)."""
    doc = json.loads(path.read_text(encoding="utf-8"))
    return {
        str(f["properties"]["region_id"]): str(f["properties"].get("name"))
        for f in doc.get("features") or []
        if (f.get("properties") or {}).get("level") == "county"
    }


def join_coverage(metrics: pd.DataFrame, regions: dict[str, str]) -> dict[str, Any]:
    """How the FCC's county keys meet the map's county polygons, both directions. Unmatched ids are
    listed, not dropped silently: territories outside the Census 1:20m file and any county-vintage
    difference (Connecticut's 2022 planning regions) show up here on the first real run."""
    fcc = set(metrics["county_fips"])
    geo = set(regions)
    matched = fcc & geo
    return {
        "fcc_counties": len(fcc),
        "region_counties": len(geo),
        "matched": len(matched),
        "matched_share_of_regions": round(len(matched) / len(geo), 4) if geo else None,
        "fcc_without_polygon": sorted(fcc - geo),
        "polygons_without_fcc_row": len(geo - fcc),
        "polygons_without_fcc_row_sample": sorted(geo - fcc)[:20],
    }


# --------------------------------------------------------------------------------------- run
def default_output() -> pathlib.Path:
    return fuels.CONTEXT_DIR / f"{SOURCE_ID}.parquet"


def run(
    *,
    snapshot: pathlib.Path | None = None,
    out: pathlib.Path | None = None,
    store: Store | None = None,
    manifest: pathlib.Path | None = None,
    credentials: Credentials | None = None,
    regions_path: pathlib.Path = COUNTIES_GEOJSON,
) -> dict[str, Any]:
    t0 = time.monotonic()
    entry = fuels.entry_for(SOURCE_ID, manifest)
    store = store or Store()
    meta: dict[str, Any]
    if snapshot is not None:
        content = snapshot.read_bytes()
        retrieved_at, _ = fuels.snapshot_metadata(snapshot, SOURCE_ID, DATA_PAGE_URL, store)
        meta = {"snapshot": str(snapshot), "bytes": len(content)}
        file_name = snapshot.name if SUMMARY_FILE_RE.search(snapshot.name) else None
    else:
        fetched = fetch(credentials=credentials, store=store)
        content, retrieved_at, file_name = fetched.content, fetched.retrieved_at, fetched.filing.file_name
        meta = {"fetched_url": fetched.fetched_url, "bytes": len(content), **fetched.meta}
    t_parse = time.monotonic()
    frame = parse(content, file_name=file_name)
    filing: Filing = frame.attrs["filing"]
    metrics = county_metrics(
        frame, retrieved_at=retrieved_at, source_url=filing.data_page_url, licence_id=entry.licence_id
    )
    parse_s = round(time.monotonic() - t_parse, 2)
    out = out or default_output()
    fuels.write_context_parquet(metrics, out)
    coverage = join_coverage(metrics, county_region_ids(regions_path))
    return {
        "source_id": SOURCE_ID,
        "file_name": filing.file_name,
        "vintage": filing.vintage,
        "vintage_label": filing.vintage_label,
        "as_of_date": filing.as_of_date,
        "file_revision": filing.revision,
        "csv_rows": frame.attrs["rows_in"],
        "county_rows_used": len(frame),
        "counties": len(metrics),
        "counties_without_fiber_row": metrics.attrs["counties_without_fiber_row"],
        "technologies_seen": frame.attrs["technologies_seen"],
        "geography_types_seen": frame.attrs["geography_types_seen"],
        "coverage": coverage,
        "unit_weighted_fiber_share": unit_weighted_fiber_share(metrics),
        "bsl_units_total": int(metrics["bsl_units"].sum()),
        "ranked": rank_counties(metrics),
        "retrieved_at": retrieved_at,
        "out": str(out),
        "parquet_bytes": out.stat().st_size,
        "parse_s": parse_s,
        "elapsed_s": round(time.monotonic() - t0, 2),
        **meta,
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--snapshot", type=pathlib.Path, help="Parse this recorded zip/CSV (no network)")
    parser.add_argument("--out", type=pathlib.Path)
    parser.add_argument("--manifest", type=pathlib.Path)
    args = parser.parse_args(argv)
    summary = run(snapshot=args.snapshot, out=args.out, manifest=args.manifest)
    print(json.dumps(summary, default=json_default))  # noqa: T201 -- CLI summary line


if __name__ == "__main__":
    main()
