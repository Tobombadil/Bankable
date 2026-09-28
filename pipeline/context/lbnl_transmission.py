"""US transmission lines -> `transmission_line` asset rows (owner decision 2026-09-28: substations and
transmission lines as built-infrastructure context layers; docs/25 §2; docs/13 §2.19).

**Why this file and not HIFLD or the EIA Atlas (terms read 2026-09-28, docs/13 §2.19).** The EIA
Energy Atlas publishes neither a transmission-line nor a substation layer (its Hub catalogue, its
ArcGIS org's 79 public services and the `www.eia.gov/maps/map_data/` zips were all checked). DHS's
HIFLD Open, the national source, was discontinued on 2025-08-26; DHS's own data.gov record for the
lines layer says ``"license": "https://www.usa.gov/government-works"``, ``"accessLevel": "public"``,
but no copy is reachable by a clean route (the GeoPlatform service is withdrawn, the Data Rescue
Project archive sits on DataLumos behind a Cloudflare challenge that is not bypassed, and the
third-party ArcGIS copies carry either Esri's Master License Agreement or no licence at all). The
one openly licensed, reachable, attributable copy of HIFLD line geometry is Lawrence Berkeley
National Laboratory's harmonized FERC Form 1 x HIFLD dataset on the Open Energy Data Initiative
(https://data.openei.org/submissions/8742, CC BY 4.0 on the dataset page and in LBNL's own DCAT
record on catalog.data.gov): one row per HIFLD line (`GlobalID`) that LBNL linked to a FERC Form 1
line record, 13,084 lines, 146,890 route miles, 49 states, 70-765 kV. It is a *subset* of the
national network -- the lines owned by FERC Form 1 respondents that LBNL could link -- and the
coverage statement must say so.

**Only the HIFLD side is kept.** Each row carries two descriptions of the line: the HIFLD one
(`substation_from_eia`, `substation_to_eia`, `operating_voltage`, `total_length`, `state`,
`respondent_legal_name_eia` = HIFLD's OWNER, the geometry) and the FERC Form 1 record LBNL linked to
it (`substation_*_ferc`, costs, conductor, structure). The link is a scored record linkage
(`confidence_tier` high/medium/low) and it is not always right: on the file fetched 2026-09-28 the
FERC respondent and the HIFLD owner differ on 2,151 of the 10,439 rows where HIFLD states an owner,
and some links join plainly different lines (GlobalID 162595, tier `low`: FERC side
"gateway"-"massac", HIFLD side "pana"-"faraday"). The geometry,
voltage, endpoints and owner below are therefore HIFLD's, which describe the line that is drawn;
nothing from the FERC side is published, so no cost or engineering figure can be attached to the
wrong line. `confidence_tier` is kept in `attributes` only as provenance of how the row entered
LBNL's set.

**One row per line, no dissolve.** Unlike the EIA pipeline layer (32,961 unnamed segments
dissolved to 259 operator rows, `eia_atlas.py`), every transmission line here has its own endpoints,
voltage and owner, which is what a reader asks of a line and what the interconnection-point matcher
(`pipeline/context/poi_match.py`) needs; dissolving by owner would destroy all three. The size is
comparable to the `power_plant` layer (14,659 rows). LBNL's geometry already carries at most 200
vertices per line (540,930 in all); coordinates are written at 6 decimal places (~0.1 m, below the
source's positional accuracy), which is the only reduction. `/v1/assets/geo` simplifies by zoom and
caps a response at the 1,500 longest lines in view (`services/api/assets.py::LINE_FEATURE_CAP`).

**Names.** LBNL lower-cased every string. Endpoint names are title-cased for display
(``"scriba"`` -> ``"Scriba"``) and HIFLD's placeholders (``tap``, ``unknown``, ``riser``,
``deadend``) become `None` -- they name no place. The asset `name` is ``"<sub_1> - <sub_2> <kV> kV"``
from those two fields, the only name the source gives a line.

**Owners are shown, not linked (yet).** HIFLD's OWNER reaches us through LBNL's normalisation, which
lower-cased it and dropped "&", "and" and legal forms: "florida power light", "virginia electric
power", plus source typos ("sothern california edison"). `pipeline.normalize.org_key` cannot join
those to the organisations already on file ("Florida Power & Light Co" keys to FLORIDA POWER AND
LIGHT, LBNL's string to FLORIDA POWER LIGHT): measured 2026-09-28 against the 2026-09-27 dev store,
only 73 of the 259 distinct owner strings (3,292 of 10,439 rows) resolve, so handing them to
`services/ingest/midstream.py` would mint about 186 new organisations, most of them duplicates of
utilities the registry already holds. `owner_name` is therefore left empty -- the generic edge loader
writes no edge -- and the owner is carried as `attributes.owner` (title-cased with a short acronym
list, ``aep`` -> ``AEP``) and `attributes.owner_raw` (LBNL's spelling). Linking needs a reviewed
alias table from these strings to existing organisations first (docs/25 §2, open item).

**Status** is `unknown`: HIFLD's STATUS field is not in LBNL's file, and a FERC Form 1 match is
not a status statement (the matched report year runs from 1994 to 2024).

Substations are **not** produced here or anywhere: DHS's own data.gov record classifies the HIFLD
Electric Substations layer ``"accessLevel": "restricted public"`` (docs/13 §2.19), and a
substation point layer derived from these line endpoints would publish what the publisher chose
to restrict. The endpoint *names* are kept on each line for the matcher.

CLI (``python -m pipeline.context.lbnl_transmission``): fetches (or reads ``--snapshot``),
normalises, writes ``data/normalized/context/us.lbnl.ferc_hifld_transmission_lines.parquet`` and
prints one JSON summary line; `web/dev_up.py` loads it with
``services.ingest.assets.load_assets_parquet`` and ``services.ingest.midstream.load_operator_edges_parquet``.
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
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from pipeline.connectors.base import ParseError
from pipeline.connectors.http import HttpFailed, PoliteSession
from pipeline.connectors.registry import Registry
from pipeline.connectors.store import Store, ts_token
from pipeline.context.eia_atlas import ASSET_COLUMNS, Provenance
from pipeline.context.geo import StateIndex, geodesic_length_miles, representative_point, wkt_multilinestring

ROOT = pathlib.Path(__file__).resolve().parents[2]
NORMALIZED_DIR = ROOT / "data" / "normalized" / "context"
STATES_GEOJSON = ROOT / "data" / "vendored" / "regions" / "us_states.geojson"

SOURCE_ID = "us.lbnl.ferc_hifld_transmission_lines"
ASSET_TYPE = "transmission_line"
LANDING_URL = "https://data.openei.org/submissions/8742"
DOWNLOAD_URL = "https://data.openei.org/files/8742/ferc_eia_transmission_lines_public.csv"
#: data.openei.org answered HTTP 429 (no Retry-After) to three of five requests on 2026-09-28,
#: minutes apart; one request a minute is what got through.
OPENEI_RPS = 1.0 / 60.0

#: Columns the normaliser reads; `parse` refuses a file missing any of them.
REQUIRED_COLUMNS = (
    "GlobalID",
    "confidence_tier",
    "respondent_legal_name_eia",
    "substation_from_eia",
    "substation_to_eia",
    "operating_voltage",
    "total_length",
    "state",
    "linestring_coord",
)
#: HIFLD's endpoint placeholders: they name no place.
PLACEHOLDER_NAMES = frozenset(
    {"tap", "unknown", "riser", "deadend", "dead end", "not available", "na", "none"}
)
#: Owner-name tokens printed in capitals after title-casing (acronyms in the file's owner strings).
OWNER_ACRONYMS = frozenset(
    {
        "aep",
        "itc",
        "ppl",
        "peco",
        "jea",
        "iid",
        "tid",
        "reu",
        "mwd",
        "ccsf",
        "ladwp",
        "wapa",
        "pud",
        "nh",
        "nv",
        "dte",
    }
)
COORD_PRECISION = 6


# --------------------------------------------------------------------------------------- fetch
@dataclass
class FetchResult:
    fetched_url: str
    retrieved_at: str
    content: bytes
    sha256: str
    last_modified: str | None
    snapshot_path: pathlib.Path | None = None
    run_path: pathlib.Path | None = None
    meta: dict[str, Any] = field(default_factory=dict)


def _now_iso() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def fetch(
    *, session: PoliteSession | None = None, store: Store | None = None, write: bool = True
) -> FetchResult:
    """One GET of the CSV (robots honoured; data.openei.org serves no robots.txt, 404), snapshot and
    run record under data/snapshots|runs unless ``write=False``."""
    session = session or PoliteSession(rate_limits={"data.openei.org": OPENEI_RPS})
    store = store or Store()
    resp = session.get(DOWNLOAD_URL, honour_robots=True)
    if resp.status_code != 200 or not resp.content.startswith(b"GlobalID,"):
        raise HttpFailed(
            f"{DOWNLOAD_URL} answered HTTP {resp.status_code} ({resp.headers.get('Content-Type')})"
        )
    result = FetchResult(
        fetched_url=DOWNLOAD_URL,
        retrieved_at=_now_iso(),
        content=resp.content,
        sha256=hashlib.sha256(resp.content).hexdigest(),
        last_modified=resp.headers.get("Last-Modified"),
    )
    if write:
        token = ts_token(dt.datetime.now(dt.UTC))
        result.snapshot_path = store.write_snapshot(SOURCE_ID, token, "csv", resp.content)
        result.run_path = store.write_run(
            SOURCE_ID,
            token,
            {
                "source_id": SOURCE_ID,
                "retrieved_at": result.retrieved_at,
                "snapshot": {
                    "fetched_url": DOWNLOAD_URL,
                    "landing_page": LANDING_URL,
                    "bytes": len(resp.content),
                    "sha256": result.sha256,
                    "last_modified": result.last_modified,
                    "etag": resp.headers.get("ETag"),
                    "path": str(result.snapshot_path.relative_to(store.root)),
                },
                "requests_made": session.requests_made,
            },
        )
    return result


# --------------------------------------------------------------------------------------- parse
def parse(content: bytes) -> pd.DataFrame:
    """The CSV as a frame of strings (no dtype guessing: IDs stay IDs). No network."""
    try:
        df = pd.read_csv(io.BytesIO(content), dtype=str, keep_default_na=False, na_values=[""])
    except (pd.errors.ParserError, UnicodeDecodeError, pd.errors.EmptyDataError) as e:
        raise ParseError(f"not a CSV: {e}") from e
    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise ParseError(f"missing columns {missing}")
    return df


# ----------------------------------------------------------------------------------- normalise
def _text(value: Any) -> str | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    s = str(value).strip()
    return s or None


def _num(value: Any) -> float | None:
    s = _text(value)
    if s is None:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def endpoint_name(value: Any) -> str | None:
    """HIFLD SUB_1/SUB_2 as LBNL wrote it -> display name, or `None` for a placeholder."""
    s = _text(value)
    if s is None:
        return None
    s = re.sub(r"\s+kv$", "", s.lower()).strip()
    if not s or s in PLACEHOLDER_NAMES or re.fullmatch(r"(?:tap|unknown)\s*\d*", s):
        return None
    return " ".join(w[:1].upper() + w[1:] for w in s.split())


def owner_display(value: Any) -> str | None:
    s = _text(value)
    if s is None:
        return None
    words = []
    for w in s.lower().split():
        words.append(w.upper() if w in OWNER_ACRONYMS else ("dba" if w == "dba" else w[:1].upper() + w[1:]))
    return " ".join(words)


def voltage_class(kv: float | None) -> str | None:
    """HIFLD's VOLT_CLASS bands, recomputed from the stated voltage (the band field itself is not
    in LBNL's file)."""
    if kv is None:
        return None
    if kv < 100:
        return "under 100 kV"
    if kv <= 161:
        return "100-161 kV"
    if kv <= 287:
        return "220-287 kV"
    if kv <= 345:
        return "345 kV"
    if kv <= 500:
        return "500 kV"
    return "735 kV and above"


def _coords(value: Any) -> list[list[float]]:
    s = _text(value)
    if s is None:
        return []
    try:
        raw = json.loads(s)
    except ValueError:
        return []
    out: list[list[float]] = []
    for pair in raw if isinstance(raw, list) else []:
        try:
            lon, lat = float(pair[0]), float(pair[1])
        except (TypeError, ValueError, IndexError):
            continue
        if -180 <= lon <= 180 and -90 <= lat <= 90:
            out.append([round(lon, COORD_PRECISION), round(lat, COORD_PRECISION)])
    return out


def _fmt_kv(kv: float | None) -> str | None:
    if kv is None:
        return None
    return f"{int(kv)}" if float(kv).is_integer() else f"{kv:g}"


def line_name(sub_1: str | None, sub_2: str | None, kv: float | None, line_id: str) -> str:
    kv_str = _fmt_kv(kv)
    kv_text = f" {kv_str} kV" if kv_str else ""
    if sub_1 and sub_2:
        return f"{sub_1} - {sub_2}{kv_text}"
    if sub_1 or sub_2:
        return f"{sub_1 or sub_2}{kv_text} line"
    return f"{kv_str + ' kV ' if kv_str else ''}line {line_id}"


def normalise(df: pd.DataFrame, prov: Provenance, *, states: StateIndex | None = None) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    dropped_no_geometry = 0
    for rec in df.to_dict("records"):
        line_id = _text(rec.get("GlobalID"))
        line = _coords(rec.get("linestring_coord"))
        if line_id is None or len(line) < 2:
            dropped_no_geometry += 1
            continue
        kv = _num(rec.get("operating_voltage"))
        sub_1 = endpoint_name(rec.get("substation_from_eia"))
        sub_2 = endpoint_name(rec.get("substation_to_eia"))
        owner_raw = _text(rec.get("respondent_legal_name_eia"))
        state = _text(rec.get("state"))
        rep = representative_point([line])
        lon, lat = (rep[0], rep[1]) if rep else (None, None)
        attributes: dict[str, Any] = {
            "voltage_kv": kv,
            "voltage_class": voltage_class(kv),
            "sub_1": sub_1,
            "sub_2": sub_2,
            "sub_1_raw": _text(rec.get("substation_from_eia")),
            "sub_2_raw": _text(rec.get("substation_to_eia")),
            "miles": round(geodesic_length_miles([line]), 2),
            "length_miles_source": _num(rec.get("total_length")),
            "vertex_count": len(line),
            "states_crossed": states.states_for([line]) if states else ([f"US-{state}"] if state else []),
            "owner": owner_display(owner_raw),
            "owner_raw": owner_raw,
            "hifld_line_id": line_id,
            "lbnl_link_confidence": _text(rec.get("confidence_tier")),
            "source_vintage": prov.vintage,
        }
        rows.append(
            {
                "source_asset_id": line_id,
                "name": line_name(sub_1, sub_2, kv, line_id),
                "operator_name": None,
                "owner_name": None,  # module docstring: shown, not linked
                "status": "unknown",
                "technology": None,
                "technology_raw": None,
                "capacity_value": None,
                "capacity_unit": None,
                "unit_count": None,
                "lon": lon,
                "lat": lat,
                "geom_line_wkt": wkt_multilinestring([line], precision=COORD_PRECISION),
                "state_code": f"US-{state.upper()}" if state and len(state) == 2 else None,
                "county_name": None,
                "country": "US",
                "attributes": attributes,
                **prov.columns(),
            }
        )
    out = pd.DataFrame(rows, columns=ASSET_COLUMNS)
    out.attrs["dropped_no_geometry"] = dropped_no_geometry
    return out


def endpoint_nodes(assets: pd.DataFrame) -> pd.DataFrame:
    """Named line endpoints as the node table `pipeline/context/poi_match.py` reads: one row per
    (endpoint name, state), located only where the lines carrying that name share an endpoint.

    LBNL's file does not say which end of the drawn line is SUB_1 and which SUB_2, so a name on a
    single line is *not* placed (``lon``/``lat`` null, ``location_method = "unresolved"``); a
    name on two or more lines is placed at the endpoint vertex every one of those lines has within
    `SAME_NODE_KM` (``"shared_endpoint"``), and if no such vertex exists the name is on lines that
    do not meet -- two places with one name -- and is emitted once per line, unplaced. These rows
    are never written to `asset` (module docstring); they exist for the crosswalk measurement."""
    from pipeline.context.geo import haversine_m

    by_key: dict[tuple[str, str], list[tuple[str, float | None, list[list[float]]]]] = {}
    for rec in assets.to_dict("records"):
        attrs = rec["attributes"] if isinstance(rec["attributes"], dict) else json.loads(rec["attributes"])
        state = (rec.get("state_code") or "")[-2:]
        wkt = str(rec.get("geom_line_wkt") or "")
        pts = re.findall(r"(-?\d+(?:\.\d+)?) (-?\d+(?:\.\d+)?)", wkt)
        ends = [[float(pts[0][0]), float(pts[0][1])], [float(pts[-1][0]), float(pts[-1][1])]] if pts else []
        for key in ("sub_1", "sub_2"):
            name = attrs.get(key)
            if name and state:
                by_key.setdefault((name, state), []).append(
                    (str(rec["source_asset_id"]), attrs.get("voltage_kv"), ends)
                )
    rows = []
    for (name, state), members in sorted(by_key.items()):
        volts = sorted({float(v) for _, v, _ in members if v is not None})
        line_ids = sorted({lid for lid, _, _ in members})
        place: list[float] | None = None
        if len(members) >= 2 and members[0][2]:
            for cand in members[0][2]:
                if all(any(haversine_m(cand, e) <= SAME_NODE_KM * 1000 for e in m[2]) for m in members[1:]):
                    place = cand
                    break
        rows.append(
            {
                "node_id": f"{state}:{name.lower()}",
                "name": name,
                "state": state,
                "lon": place[0] if place else None,
                "lat": place[1] if place else None,
                "voltages_kv": volts,
                "line_ids": line_ids,
                "location_method": "shared_endpoint" if place else "unresolved",
            }
        )
    return pd.DataFrame(
        rows, columns=["node_id", "name", "state", "lon", "lat", "voltages_kv", "line_ids", "location_method"]
    )


#: Two line endpoints closer than this are one node (a substation's bays sit a few hundred metres
#: apart; HIFLD digitised lines to the fence, not to a common point).
SAME_NODE_KM = 1.0


# --------------------------------------------------------------------------------------- CLI
def default_output() -> pathlib.Path:
    return NORMALIZED_DIR / f"{SOURCE_ID}.parquet"


def _latest_snapshot(store: Store) -> pathlib.Path:
    d = store.root / "snapshots" / SOURCE_ID
    candidates = sorted(d.glob("*.csv")) if d.exists() else []
    if not candidates:
        raise FileNotFoundError(f"no snapshot under {d}")
    return candidates[-1]


def _snapshot_provenance(store: Store, path: pathlib.Path, licence: str) -> Provenance:
    retrieved_at: str | None = None
    run_path = store.run_path(SOURCE_ID, path.stem)
    if run_path.exists():
        try:
            retrieved_at = json.loads(run_path.read_text(encoding="utf-8")).get("retrieved_at")
        except (json.JSONDecodeError, OSError):
            retrieved_at = None
    if retrieved_at is None:
        try:
            retrieved_at = (
                dt.datetime.strptime(path.stem, "%Y%m%dT%H%M%SZ")
                .replace(tzinfo=dt.UTC)
                .isoformat(timespec="seconds")
                .replace("+00:00", "Z")
            )
        except ValueError:
            retrieved_at = _now_iso()
    return Provenance(SOURCE_ID, DOWNLOAD_URL, retrieved_at, licence)


def run(
    *,
    snapshot: pathlib.Path | None = None,
    latest_snapshot: bool = False,
    out: pathlib.Path | None = None,
    with_states: bool = True,
    store: Store | None = None,
) -> dict[str, Any]:
    t0 = time.monotonic()
    store = store or Store()
    entry = Registry().get(SOURCE_ID)
    if snapshot is None and latest_snapshot:
        snapshot = _latest_snapshot(store)
    fetch_summary: dict[str, Any]
    if snapshot is not None:
        content = snapshot.read_bytes()
        prov = _snapshot_provenance(store, snapshot, entry.license)
        fetch_summary = {"snapshot": str(snapshot)}
    else:
        result = fetch(store=store)
        content = result.content
        prov = Provenance(SOURCE_ID, result.fetched_url, result.retrieved_at, entry.license)
        fetch_summary = {
            "fetched_url": result.fetched_url,
            "bytes": len(content),
            "sha256": result.sha256,
            "last_modified": result.last_modified,
            "snapshot": str(result.snapshot_path) if result.snapshot_path else None,
            "run": str(result.run_path) if result.run_path else None,
        }
    raw = parse(content)
    states = StateIndex.from_file(STATES_GEOJSON) if with_states and STATES_GEOJSON.exists() else None
    df = normalise(raw, prov, states=states)
    out = out or default_output()
    from pipeline.connectors.base import to_parquet_safe

    out.parent.mkdir(parents=True, exist_ok=True)
    to_parquet_safe(df).to_parquet(out, index=False)
    return {
        "source_id": SOURCE_ID,
        "rows_in": len(raw),
        "rows": len(df),
        "dropped_no_geometry": df.attrs.get("dropped_no_geometry", 0),
        "with_owner": int(df["attributes"].map(lambda a: a.get("owner") is not None).sum()) if len(df) else 0,
        "total_miles": round(float(df["attributes"].map(lambda a: a["miles"]).sum()), 1) if len(df) else 0.0,
        "vertices": int(df["attributes"].map(lambda a: a["vertex_count"]).sum()) if len(df) else 0,
        "retrieved_at": prov.retrieved_at,
        "out": str(out),
        "parquet_bytes": out.stat().st_size,
        "elapsed_s": round(time.monotonic() - t0, 2),
        **fetch_summary,
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--snapshot", type=pathlib.Path, help="Parse this recorded CSV instead of fetching")
    group.add_argument(
        "--latest-snapshot", action="store_true", help="Parse the newest data/snapshots/<source_id>/*.csv"
    )
    parser.add_argument("--out", type=pathlib.Path, help="Output parquet")
    parser.add_argument("--no-states", action="store_true", help="Skip states_crossed point-in-polygon")
    args = parser.parse_args(argv)
    summary = run(
        snapshot=args.snapshot,
        latest_snapshot=args.latest_snapshot,
        out=args.out,
        with_states=not args.no_states,
    )
    print(json.dumps(summary))  # noqa: T201 -- CLI summary line, same convention as eia_atlas.py


if __name__ == "__main__":
    main()
