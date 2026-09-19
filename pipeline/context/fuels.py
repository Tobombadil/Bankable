"""Shared seam for the fuels asset layers (owner decision 2026-09-19, option (a): ethanol and RNG
point layers join the midstream wave; ADR 0008 "new point layers are one parser each").

Four registry sources ride this module: `ethanol_capacity.py` (EIA annual plant table),
`ethanol_plants.py` (EIA Atlas ArcGIS layer, gated), `lmop.py` (EPA landfill-gas projects) and
`agstar.py` (EPA farm digesters). Each is fetch -> parse -> build rows -> parquet, and every one
emits the same frame shape so `services/ingest/assets.py::load_assets_parquet` loads it unchanged.

Column contract (`ASSET_COLUMNS`). The first block is exactly what `load_assets` reads (its
docstring lists them: `source_asset_id`, `name`, `operator_name`, `technology`, `technology_raw`,
`technologies`, `capacity_mw`, `capacity_value`, `capacity_unit`, `unit_count`,
`commissioned_year`, `lon`/`lat`, `state_code`, `county_name`, `county_fips`, `country`,
`attributes`, `status`, `source_url`, `retrieved_at`); the loader ignores every other column, so
the second block carries this lane's provenance and feature text without touching the loader:

- `attributes` holds only the *numeric* objective features. The loader's `_to_json_dict` casts
  every value with `float()`, so a string there would abort the load; the string-valued features
  (feedstock, project type, end use, developer strings) live in `attributes_text` (JSON) and in
  the dedicated columns `feedstock`, `owner_raw`, `developer_raw`, `status_raw`. Folding them
  into `asset.attributes` needs a one-line loader change the coordinator owns.
- `lon`/`lat` are set only from a coordinate the source itself states (EIA-860M rule, same
  `valid_point` check as `eia_plants.py`). A source that gives county or city only is left
  unplaced for the loader (ADR 0008: never a point at a centroid) and carries the county FIPS
  plus a `placement_precision`/`centroid_lon`/`centroid_lat` triple so the map can draw it at
  region grade.
- `status` is the `asset.status` vocabulary (`services/db/models.py::ASSET_STATUSES`); rows whose
  registry status is planned or under construction are proposals (ADR 0008 §1) and never enter
  an asset frame -- each parser returns them separately.
- `licence` is the reuse class label (`public-domain`, as `eia_plants.py` writes) and
  `licence_id` the registry key the loader will mint (`SourceEntry.licence_id`).

Manifest gate: `entry_for()` refuses a source whose `reuse` is gated or whose `publication` is
`none`, before any fetch and before any parquet write -- the same rule the loader re-checks.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import pathlib
import re
from typing import Any

import pandas as pd

from pipeline.connectors.base import GateViolation, to_parquet_safe
from pipeline.connectors.http import HttpBlocked, HttpFailed, PoliteSession
from pipeline.connectors.registry import Registry, SourceEntry
from pipeline.connectors.store import Store, ts_token
from services.ingest.geocode import CountyGazetteer, default_gazetteer

ROOT = pathlib.Path(__file__).resolve().parents[2]
CONTEXT_DIR = ROOT / "data" / "normalized" / "context"

#: Reuse-class label written to `licence`, the same value `pipeline.normalize.SOURCE_META` gives
#: the EIA-860M plants frame. All four fuels sources are US federal works (docs/13 §2.11, §2.12).
LICENCE_CLASS = "public-domain"

#: `asset.status` vocabulary (`services/db/models.py::ASSET_STATUSES`), spelled here so the
#: pipeline does not import the ORM just for four strings.
STATUS_VOCAB = ("operating", "standby", "retired", "unknown")

#: Per-host politeness: EPA and EIA are 0.5 rps (the FERC ceiling, well under anything either
#: site asks for); atlas.eia.gov/robots.txt sets `Crawl-delay: 60` for every agent (read
#: 2026-09-19), so one request a minute there; the ArcGIS feature host gets 0.5 rps.
RATE_LIMITS: dict[str, float] = {
    "www.epa.gov": 0.5,
    "www.eia.gov": 0.5,
    "atlas.eia.gov": 1.0 / 60.0,
    "services7.arcgis.com": 0.5,
}

#: Loader-mapped columns first (the order `load_assets` documents), lane extras after.
ASSET_COLUMNS: list[str] = [
    "source_id",
    "source_url",
    "retrieved_at",
    "licence",
    "licence_id",
    "source_asset_id",
    "name",
    "operator_name",
    "status",
    "technology",
    "technology_raw",
    "technologies",
    "capacity_mw",
    "capacity_value",
    "capacity_unit",
    "commissioned_year",
    "unit_count",
    "lon",
    "lat",
    "state_code",
    "county_name",
    "county_fips",
    "country",
    "attributes",
    # -- lane extras: ignored by the loader today, kept for the feature set and provenance
    "attributes_text",
    "feedstock",
    "owner_raw",
    "developer_raw",
    "status_raw",
    "placement_precision",
    "centroid_lon",
    "centroid_lat",
    "raw",
]

_WORLD_LAT_RANGE = (-90.0, 90.0)
_WORLD_LON_RANGE = (-180.0, 180.0)
_TOKEN_RE = re.compile(r"^\d{8}T\d{6}Z$")


# ------------------------------------------------------------------ registry / gate
def entry_for(source_id: str, manifest: pathlib.Path | None = None) -> SourceEntry:
    """The manifest row, refused (never silently skipped) when the source may not be published.
    `manifest` overrides `data/sources.yaml` (a run against the last committed manifest while a
    working-tree edit of it is mid-flight; tests pass a fixture)."""
    entry = (Registry(manifest) if manifest else Registry()).get(source_id)
    if entry.gated:
        raise GateViolation(f"{source_id}: reuse={entry.reuse!r} is gated; no fetch, no parquet")
    if entry.publication == "none":
        raise GateViolation(f"{source_id}: publication=none publishes nothing (docs/21 §8)")
    return entry


# ------------------------------------------------------------------ fetch + snapshot
def polite_session() -> PoliteSession:
    return PoliteSession(rate_limits=RATE_LIMITS, default_rps=0.5)


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def iso(t: dt.datetime) -> str:
    return t.astimezone(dt.UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def fetch(session: PoliteSession, url: str, *, honour_robots: bool = True) -> bytes:
    """One GET through the polite session. Challenge pages and robots refusals surface as
    `HttpBlocked`; anything else non-2xx as `HttpFailed` -- never swallowed."""
    resp = session.get(url, honour_robots=honour_robots)
    if resp.status_code >= 400:
        raise HttpFailed(f"GET {url} -> HTTP {resp.status_code}")
    content: bytes = resp.content
    return content


def record_snapshot(
    source_id: str,
    content: bytes,
    *,
    ext: str,
    fetched_url: str,
    retrieved_at: dt.datetime,
    requests_made: int,
    meta: dict[str, Any] | None = None,
    store: Store | None = None,
) -> tuple[pathlib.Path, str]:
    """Write the raw bytes under `data/snapshots/<source_id>/<token>.<ext>` and the run record
    under `data/runs/<source_id>/<token>.json`, the layout `pipeline.connectors.store` documents
    and `snapshot_metadata()` reads back. Returns `(snapshot_path, retrieved_at_iso)`."""
    st = store or Store()
    token = ts_token(retrieved_at)
    path = st.write_snapshot(source_id, token, ext, content)
    record = {
        "id": f"{source_id}:{token}",
        "source_id": source_id,
        "kind": "asset",
        "status": "ok",
        "retrieved_at": iso(retrieved_at),
        "bytes": len(content),
        "snapshot": {
            "object_key": str(path),
            "sha256": hashlib.sha256(content).hexdigest(),
            "byte_size": len(content),
            "fetched_url": fetched_url,
            "retrieved_at": iso(retrieved_at),
            "requests_made": requests_made,
            "meta": meta or {},
        },
    }
    st.write_run(source_id, token, record)
    return path, iso(retrieved_at)


def latest_snapshot(source_id: str, ext: str, store: Store | None = None) -> pathlib.Path:
    root = (store or Store()).root / "snapshots" / source_id
    candidates = sorted(root.glob(f"*.{ext}"))
    if not candidates:
        raise FileNotFoundError(f"no *.{ext} snapshot under {root}")
    return candidates[-1]


def snapshot_metadata(
    path: pathlib.Path, source_id: str, fallback_url: str, store: Store | None = None
) -> tuple[str, str]:
    """`(retrieved_at_iso, source_url)` for a snapshot named `<token>.<ext>` (the token is the
    retrieval time; the matching run record gives the exact URL fetched). A file that is not one
    of the store's snapshots (a test fixture, a hand-downloaded workbook) gets "now" and the
    fallback URL, the same rule as `eia_plants._snapshot_metadata`."""
    token = path.stem
    retrieved_at = iso(utc_now())
    if _TOKEN_RE.match(token):
        retrieved_at = iso(dt.datetime.strptime(token, "%Y%m%dT%H%M%SZ").replace(tzinfo=dt.UTC))
    source_url = fallback_url
    run_path = (store or Store()).run_path(source_id, token)
    if run_path.exists():
        try:
            record = json.loads(run_path.read_text(encoding="utf-8"))
            source_url = str(((record.get("snapshot") or {}).get("fetched_url")) or fallback_url)
        except (json.JSONDecodeError, OSError):
            source_url = fallback_url
    return retrieved_at, source_url


# ------------------------------------------------------------------ value helpers
def clean_str(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, float) and pd.isna(value):
        return None
    if value is pd.NA or value is pd.NaT:
        return None
    text = str(value).strip()
    if not text or text.lower() in {"nan", "none", "n/a"}:
        return None
    return text


def to_float(value: Any) -> float | None:
    text = clean_str(value)
    if text is None:
        return None
    try:
        f = float(text.replace(",", ""))
    except ValueError:
        return None
    return None if pd.isna(f) else f


def to_year(value: Any) -> int | None:
    """Year of a date, a datetime, a `YYYY` or a `YYYY-MM-DD` string; `None` otherwise."""
    if isinstance(value, dt.datetime | dt.date):
        return value.year
    text = clean_str(value)
    if text is None:
        return None
    m = re.match(r"^(\d{4})", text)
    if m:
        return int(m.group(1))
    try:
        ts = pd.Timestamp(text)
    except (ValueError, TypeError):
        return None
    return None if pd.isna(ts) else int(ts.year)


def valid_point(lat_raw: Any, lon_raw: Any) -> tuple[float, float] | None:
    """`(lon, lat)` only for a numeric, finite, in-bounds, non-`(0, 0)` pair -- the rule
    `eia_plants._valid_point` and `services/ingest/loader.py::_extract_exact_point` apply."""
    lat = to_float(lat_raw)
    lon = to_float(lon_raw)
    if lat is None or lon is None:
        return None
    if lat == 0.0 and lon == 0.0:
        return None
    if not (_WORLD_LAT_RANGE[0] <= lat <= _WORLD_LAT_RANGE[1]):
        return None
    if not (_WORLD_LON_RANGE[0] <= lon <= _WORLD_LON_RANGE[1]):
        return None
    return (lon, lat)


def state_code(value: Any) -> str | None:
    """`US-XX` from a two-letter postal code; `None` for anything else."""
    text = clean_str(value)
    if text is None:
        return None
    text = text.upper()
    return f"US-{text}" if re.fullmatch(r"[A-Z]{2}", text) else None


def content_key(*parts: Any) -> str:
    """Stable id for a registry row that carries no id of its own (docs/20 §3.1: a content hash
    of the identifying columns). Case- and whitespace-insensitive so a re-published row with
    trivially different spacing keeps its identity."""
    norm = "|".join(re.sub(r"\s+", " ", (clean_str(p) or "").lower()) for p in parts)
    return hashlib.sha1(norm.encode("utf-8"), usedforsecurity=False).hexdigest()[:16]


def numeric_attributes(values: dict[str, Any]) -> dict[str, float]:
    """Loader-safe attributes: floats only, missing values dropped, never a string."""
    out: dict[str, float] = {}
    for k, v in values.items():
        f = to_float(v) if not isinstance(v, bool) else float(v)
        if f is not None:
            out[k] = f
    return out


def text_attributes(values: dict[str, Any]) -> dict[str, str]:
    return {k: s for k, v in values.items() if (s := clean_str(v)) is not None}


# ------------------------------------------------------------------ placement
class Placement:
    """County-name placement for the sources that state no coordinate (AgSTAR, the EIA capacity
    table, LMOP rows without a Latitude). The centroid never becomes `lon`/`lat`; it rides the
    `centroid_*` columns with its precision so the map draws the county, not a false point."""

    def __init__(self, gaz: CountyGazetteer | None = None) -> None:
        self.gaz = gaz or default_gazetteer()

    def resolve(self, state: str | None, county: str | None) -> dict[str, Any]:
        st = clean_str(state)
        st = st.upper() if st else None
        fips = self.gaz.county_fips(st, county) if st else None
        point = self.gaz.county_point(st, county) if st else None
        precision = "county_centroid" if point is not None else "unknown"
        if point is None and st:
            point = self.gaz.state_point(st)
            precision = "state_centroid" if point is not None else "unknown"
        lat, lon = point if point is not None else (None, None)
        return {
            "county_fips": fips,
            "placement_precision": precision,
            "centroid_lon": lon,
            "centroid_lat": lat,
        }


# ------------------------------------------------------------------ frame assembly
def asset_row(**fields: Any) -> dict[str, Any]:
    """One row with every `ASSET_COLUMNS` key present (missing -> None), so frames from all four
    parsers align column-for-column."""
    unknown = set(fields) - set(ASSET_COLUMNS)
    if unknown:
        raise KeyError(f"not asset columns: {sorted(unknown)}")
    row: dict[str, Any] = dict.fromkeys(ASSET_COLUMNS)
    row.update(fields)
    row.setdefault("technologies", {})
    if row["technologies"] is None:
        row["technologies"] = {}
    if row["attributes"] is None:
        row["attributes"] = {}
    if row["attributes_text"] is None:
        row["attributes_text"] = {}
    row["country"] = row["country"] or "US"
    row["licence"] = row["licence"] or LICENCE_CLASS
    if row["status"] not in STATUS_VOCAB:
        raise ValueError(f"status {row['status']!r} is not in {STATUS_VOCAB}")
    return row


def assemble(rows: list[dict[str, Any]]) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=ASSET_COLUMNS)
    if len(df) and df["source_asset_id"].duplicated().any():
        dups = sorted(df.loc[df["source_asset_id"].duplicated(), "source_asset_id"].unique())
        raise ValueError(f"duplicate source_asset_id after keying: {dups[:5]}")
    return df


def write_context_parquet(df: pd.DataFrame, out: pathlib.Path) -> pathlib.Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    to_parquet_safe(df).to_parquet(out, index=False)
    return out


def summarise(df: pd.DataFrame, **extra: Any) -> dict[str, Any]:
    placed = int(df["lon"].notna().sum()) if len(df) else 0
    by_status = {str(k): int(v) for k, v in df["status"].value_counts().items()} if len(df) else {}
    by_tech = {str(k): int(v) for k, v in df["technology"].value_counts().items()} if len(df) else {}
    return {
        "assets": len(df),
        "with_coordinates": placed,
        "without_coordinates": len(df) - placed,
        "by_status": by_status,
        "by_technology": by_tech,
        **extra,
    }


__all__ = [
    "ASSET_COLUMNS",
    "CONTEXT_DIR",
    "LICENCE_CLASS",
    "RATE_LIMITS",
    "STATUS_VOCAB",
    "HttpBlocked",
    "HttpFailed",
    "Placement",
    "assemble",
    "asset_row",
    "clean_str",
    "content_key",
    "entry_for",
    "fetch",
    "iso",
    "latest_snapshot",
    "numeric_attributes",
    "polite_session",
    "record_snapshot",
    "snapshot_metadata",
    "state_code",
    "summarise",
    "text_attributes",
    "to_float",
    "to_year",
    "utc_now",
    "valid_point",
    "write_context_parquet",
]
