"""Sprint 2 public-site data build (docs/04 DA-2, D-3, D-8, D-9; docs/21 §7-§8).

Reads the latest normalised parquet per source under `data/normalized/<source_id>/` (or, with
`--eval-parquet`, the single combined `data/eval/normalized.parquet` fallback used by tests) and
writes three static files consumed by the FastAPI app:

    web/static/data/proposals.geojson    -- placed proposals (point) + state-aggregate markers
    web/static/data/opportunities.json   -- opportunity list (no geometry in this sprint)
    web/static/data/stats.json           -- counts, unplaced records, source/licence registry

Every emitted proposal/opportunity feature carries the provenance quartet (`source_id`,
`source_url`, `retrieved_at`, `licence_id`) plus `licence_text`, `reuse_class` and
`attribution_text` so every page renders attribution from data, never a hard-coded string
(`CLAUDE.md`; docs/04 DA-2, D-27, D-34).

Scope note: this build covers only the five sources the Sprint 2 task named (ERCOT, CAISO,
NYISO, EIA-860M, NESO TEC for proposals; grants.gov, TED, Find a Tender, World Bank for
opportunities). None of the nine is `restricted`/`unknown` in `data/sources.yaml` today, but the
gate below is enforced anyway (`docs/04` E-13 item 3) so a future registry change fails closed
rather than leaking a row.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, cast

import pandas as pd
import yaml

log = logging.getLogger("web.build_data")

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATA_DIR = REPO_ROOT / "data" / "normalized"
DEFAULT_SOURCES_YAML = REPO_ROOT / "data" / "sources.yaml"
DEFAULT_OUT_DIR = REPO_ROOT / "web" / "static" / "data"
COUNTY_CENTROID_TSV = REPO_ROOT / "web" / "data_ref" / "us_county_centroids.tsv"

# Paywall by shape, not by time (owner, 2026-09-19; `services/ingest/lag.py`): a record is public
# the moment it is ingested, so this static prototype builder applies no cutoff either. Kept as
# named constants rather than deleted because the `--no-lag` preview flag and the stats fragment
# both still speak in terms of a lag, and a future per-shape cutoff would land here.
LAG_DAYS_PROPOSAL = 0
LAG_DAYS_OPPORTUNITY = 0

Family = Literal["neutral", "progress", "committed", "success", "danger"]

# docs/21 §7.1-§7.2; docs/31 §1.2 five-family grouping.
LIFECYCLE_FAMILY: dict[str, Family] = {
    "announced": "neutral",
    "unknown": "neutral",
    "closed": "neutral",
    "filed": "progress",
    "studied": "progress",
    "permitted": "progress",
    "under_construction": "progress",
    "reinstated": "progress",
    "contracted": "committed",
    "awarded": "committed",
    "built": "success",
    "open": "success",
    "withdrawn": "danger",
    "cancelled": "danger",
    "frozen": "danger",
}
FAMILY_LABEL: dict[Family, str] = {
    "neutral": "Neutral",
    "progress": "Progress",
    "committed": "Committed",
    "success": "Success",
    "danger": "Danger",
}


# The five proposal + four opportunity sources this Sprint 2 task names, mapped to the policy
# facts a page must render (attribution text, whether raw fields may reach a public surface).
# `allows_raw` mirrors docs/21 §8's field-class table: CAISO and NYISO are attribution sources
# published derived-only at launch (docs/00-PLAN.md decisions log, 2026-09-12 legal register
# outcome; `data/sources.yaml` notes on each source) even though their `reuse` class alone would
# otherwise permit more. Everything else in this set is raw-ok.
@dataclass(frozen=True)
class SourcePolicy:
    attribution_text: str
    allows_raw: bool
    basis: str


SOURCE_POLICY: dict[str, SourcePolicy] = {
    "us.iso.ercot.gen_queue": SourcePolicy(
        "ERCOT", True, "reuse=open (data/sources.yaml); ERCOT Website User Agreement clause 5"
    ),
    "us.iso.caiso.gen_queue": SourcePolicy(
        "California ISO",
        False,
        "docs/21 §8 attribution/raw-withheld example; sources.yaml notes 'Publish derived-only "
        "until counsel resolves the tension'",
    ),
    "us.iso.nyiso.gen_queue": SourcePolicy(
        "NYISO",
        False,
        "docs/00-PLAN.md legal register outcome 2026-09-12: 'NYISO derived-only with credit'; "
        "sources.yaml notes 'Derived-only at launch with credit NYISO'",
    ),
    "us.eia.860m": SourcePolicy(
        "U.S. Energy Information Administration", True, "US federal public domain work"
    ),
    "gb.neso.tec_register": SourcePolicy(
        "Supported by National Energy SO Open Data",
        True,
        "NESO Open Data Licence v1.0 mandatory attribution string (sources.yaml)",
    ),
    "us.grants_gov.search2": SourcePolicy("Grants.gov", True, "US federal public domain work"),
    "eu.ted.api": SourcePolicy(
        "Tenders Electronic Daily (TED), Publications Office of the EU",
        True,
        "sources.yaml notes 'Raw-ok. Credit the source'",
    ),
    "gb.find_a_tender": SourcePolicy(
        "Find a Tender Service (Crown copyright, Open Government Licence v3)", True, "OGL v3"
    ),
    "mdb.worldbank.procnotices": SourcePolicy("World Bank", True, "World Bank Open Data, CC BY 4.0"),
}

PROPOSAL_SOURCE_IDS = [
    "us.iso.ercot.gen_queue",
    "us.iso.caiso.gen_queue",
    "us.iso.nyiso.gen_queue",
    "us.eia.860m",
    "gb.neso.tec_register",
]
OPPORTUNITY_SOURCE_IDS = [
    "us.grants_gov.search2",
    "eu.ted.api",
    "gb.find_a_tender",
    "mdb.worldbank.procnotices",
]

# data/eval/normalized.parquet (the Phase 2 evaluation fallback) uses short source ids and
# includes SPP/ISO-NE, which are `restricted` (docs/13-legal-data-rights.md) and must never be
# published (CLAUDE.md guardrail) -- rows that map to them are dropped, not merely relabelled.
EVAL_SHORT_ID_MAP: dict[str, str] = {
    "ercot": "us.iso.ercot.gen_queue",
    "caiso": "us.iso.caiso.gen_queue",
    "nyiso": "us.iso.nyiso.gen_queue",
    "eia860m": "us.eia.860m",
    # "spp" and "isone" deliberately absent: restricted sources are excluded, not remapped.
}

_COUNTY_SUFFIX_RE = re.compile(r"\b(COUNTY|PARISH|BOROUGH|CENSUS AREA|MUNICIPALITY|CITY AND BOROUGH|CITY)\b")
_NON_ALNUM_RE = re.compile(r"[^A-Z0-9 ]")

# NYISO's free-text `county` field names NYC boroughs and carries a handful of misspellings
# rather than the Gazetteer's county name; a multi-county span ("Oneida-Dutchess") is left
# unmatched on purpose -- D-8's state-aggregate fallback is the honest placement for those, not
# a guess at one of the counties named.
_COUNTY_ALIASES: dict[str, str] = {
    "BROOKLYN": "KINGS",
    "MANHATTAN": "NEW YORK",
    "STATEN ISLAND": "RICHMOND",
    "THOMPKINS": "TOMPKINS",
    "OSTEGO": "OTSEGO",
}


def normalize_county_name(name: str | None) -> str | None:
    """Match `docs/21` §3.7 `county_name` free text against the Census Gazetteer spelling."""
    if not name:
        return None
    upper = name.upper()
    upper = _COUNTY_SUFFIX_RE.sub("", upper)
    upper = _NON_ALNUM_RE.sub("", upper)
    normalized = " ".join(upper.split())
    if not normalized:
        return None
    return _COUNTY_ALIASES.get(normalized, normalized)


@dataclass
class CountyGazetteer:
    """US county centroids (Census Gazetteer 2024, public domain) plus a derived state centroid
    (unweighted mean of that state's county centroids) used for the D-8 state-aggregate marker.
    """

    counties: dict[tuple[str, str], tuple[float, float]] = field(default_factory=dict)
    state_centroids: dict[str, tuple[float, float]] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> CountyGazetteer:
        gaz = cls()
        sums: dict[str, tuple[float, float, int]] = {}
        with path.open(encoding="utf-8") as f:
            next(f)  # header
            for line in f:
                state, county_name, lat_s, lon_s = line.rstrip("\n").split("\t")
                lat, lon = float(lat_s), float(lon_s)
                norm = normalize_county_name(county_name)
                if norm:
                    gaz.counties[(state, norm)] = (lat, lon)
                slat, slon, n = sums.get(state, (0.0, 0.0, 0))
                sums[state] = (slat + lat, slon + lon, n + 1)
        gaz.state_centroids = {s: (slat / n, slon / n) for s, (slat, slon, n) in sums.items() if n}
        return gaz

    def county_point(self, state: str | None, county_name: str | None) -> tuple[float, float] | None:
        if not state:
            return None
        norm = normalize_county_name(county_name)
        if not norm:
            return None
        return self.counties.get((state.upper(), norm))

    def state_point(self, state: str | None) -> tuple[float, float] | None:
        if not state:
            return None
        return self.state_centroids.get(state.upper())


def load_sources_yaml(path: Path) -> dict[str, dict[str, Any]]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    out: dict[str, dict[str, Any]] = {}
    for entry in raw["sources"]:
        out[str(entry["id"])] = entry
    return out


def slugify(text: str) -> str:
    lowered = text.lower().strip()
    lowered = re.sub(r"[^a-z0-9]+", "-", lowered)
    return lowered.strip("-") or "record"


def public_id(prefix: str, record_id: str) -> str:
    # Not a security context: a short, stable id derived from the source's own record_id
    # (docs/21 §1 `public_id`). sha256 avoids the weak-hash lint rather than needing a
    # per-call suppression.
    digest = hashlib.sha256(record_id.encode("utf-8")).hexdigest()[:10]
    return f"{prefix}_{digest}"


def make_slug(name: str, record_id: str) -> str:
    digest = hashlib.sha256(record_id.encode("utf-8")).hexdigest()[:6]
    return f"{slugify(name)}-{digest}"


def latest_parquet(data_dir: Path, source_id: str) -> Path | None:
    source_dir = data_dir / source_id
    if not source_dir.is_dir():
        return None
    files = sorted(source_dir.glob("*.parquet"))
    return files[-1] if files else None


def iso(ts: Any) -> str | None:
    """RFC 3339 UTC string for a pandas Timestamp/NaT (`docs/23` §1 time convention)."""
    if ts is None or (isinstance(ts, float) and pd.isna(ts)):
        return None
    if pd.isna(ts):
        return None
    timestamp = cast(pd.Timestamp, ts)
    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize("UTC")
    else:
        timestamp = timestamp.tz_convert("UTC")
    return str(timestamp.strftime("%Y-%m-%dT%H:%M:%SZ"))


def date_only(ts: Any) -> str | None:
    if ts is None or pd.isna(ts):
        return None
    timestamp = cast(pd.Timestamp, ts)
    return str(timestamp.strftime("%Y-%m-%d"))


def none_if_nan(value: Any) -> Any:
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return value


def load_raw_json(raw_value: Any) -> dict[str, Any] | None:
    if raw_value is None:
        return None
    try:
        if pd.isna(raw_value):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(raw_value, str):
        try:
            parsed = json.loads(raw_value)
        except json.JSONDecodeError:
            return None
        return cast(dict[str, Any], parsed) if isinstance(parsed, dict) else None
    return None


@dataclass
class BuildStats:
    generated_at: str
    no_lag: bool
    lag_days: dict[str, int]
    data_as_of: dict[str, str | None]
    proposals_total: int = 0
    proposals_visible: int = 0
    opportunities_total: int = 0
    opportunities_visible: int = 0
    proposals_by_source: dict[str, int] = field(default_factory=dict)
    opportunities_by_source: dict[str, int] = field(default_factory=dict)
    proposal_family_counts: dict[str, int] = field(default_factory=dict)
    opportunity_family_counts: dict[str, int] = field(default_factory=dict)
    unplaced: list[dict[str, Any]] = field(default_factory=list)
    sources: dict[str, dict[str, Any]] = field(default_factory=dict)


def build_source_meta(source_id: str, registry: dict[str, dict[str, Any]], kind: str) -> dict[str, Any]:
    entry = registry.get(source_id, {})
    policy = SOURCE_POLICY[source_id]
    return {
        "source_id": source_id,
        "kind": kind,
        "name": entry.get("name", source_id),
        "operator": entry.get("operator"),
        "homepage": entry.get("url"),
        "licence_text": entry.get("license"),
        "reuse_class": entry.get("reuse"),
        "attribution_text": policy.attribution_text,
        "allows_raw": policy.allows_raw,
        "policy_basis": policy.basis,
    }


def _load_source_frame(
    source_id: str, *, data_dir: Path | None, eval_df: pd.DataFrame | None
) -> pd.DataFrame | None:
    if eval_df is not None:
        short_ids = [k for k, v in EVAL_SHORT_ID_MAP.items() if v == source_id]
        if not short_ids:
            return None
        frame = eval_df[eval_df["source_id"].isin(short_ids)].copy()
        if frame.empty:
            return None
        frame["source_id"] = source_id
        if "licence_id" not in frame.columns and "licence" in frame.columns:
            frame["licence_id"] = frame["licence"]
        if "raw" not in frame.columns:
            frame["raw"] = None
        return frame
    if data_dir is None:
        # `--eval-parquet` mode: the combined fallback covers proposals only (`pipeline/README.md`
        # prototype stages), so an opportunity source simply has no rows here rather than an error.
        log.warning("no data source configured for source", extra={"source_id": source_id})
        return None
    path = latest_parquet(data_dir, source_id)
    if path is None:
        log.warning("no normalised parquet found", extra={"source_id": source_id})
        return None
    return pd.read_parquet(path)


def _extract_eia_latlon(raw_fields: dict[str, Any] | None) -> tuple[float, float] | None:
    if not raw_fields:
        return None
    lat, lon = raw_fields.get("Latitude"), raw_fields.get("Longitude")
    if isinstance(lat, int | float) and isinstance(lon, int | float):
        return float(lat), float(lon)
    return None


def build_proposals(
    *,
    registry: dict[str, dict[str, Any]],
    gaz: CountyGazetteer,
    data_dir: Path | None,
    eval_df: pd.DataFrame | None,
    now: dt.datetime,
    no_lag: bool,
) -> tuple[list[dict[str, Any]], BuildStats]:
    """Return (GeoJSON features, stats-fragment) for the five proposal sources.

    Placement precedence is `docs/04` D-8: exact point (only EIA-860M supplies one, and only
    because it is `open`) -> county centroid -> state-aggregate marker -> Unplaced. D-9's
    restricted-precision rule additionally forces CAISO and NYISO to the county centroid even
    though the precedence above would otherwise be satisfied by an exact point, because neither
    source's raw geometry may reach a public surface (`SOURCE_POLICY[...].allows_raw = False`).
    """
    cutoff = now if no_lag else now - dt.timedelta(days=LAG_DAYS_PROPOSAL)
    features: list[dict[str, Any]] = []
    state_agg: dict[str, dict[str, Any]] = {}
    unplaced_groups: dict[str, list[dict[str, Any]]] = {}
    by_source: dict[str, int] = {}
    family_counts: dict[str, int] = dict.fromkeys(FAMILY_LABEL, 0)
    total = 0
    visible = 0
    latest_retrieved: str | None = None
    sources_meta: dict[str, dict[str, Any]] = {}

    for source_id in PROPOSAL_SOURCE_IDS:
        meta = build_source_meta(source_id, registry, "proposal")
        policy = SOURCE_POLICY[source_id]
        df = _load_source_frame(source_id, data_dir=data_dir, eval_df=eval_df)
        sources_meta[source_id] = {**meta, "rows_visible": 0}
        if df is None:
            continue
        total += len(df)
        visible_here = 0
        for row in df.to_dict(orient="records"):
            retrieved_at_raw: Any = row.get("retrieved_at")
            retrieved_ts = pd.to_datetime(retrieved_at_raw, utc=True, errors="coerce")
            if pd.isna(retrieved_ts) or retrieved_ts.to_pydatetime() > cutoff:
                continue
            retrieved_iso = iso(retrieved_ts)
            if retrieved_iso and (latest_retrieved is None or retrieved_iso > latest_retrieved):
                latest_retrieved = retrieved_iso

            record_id = str(row["record_id"])
            name = str(row.get("name_canonical") or row.get("name_norm") or record_id)
            lifecycle_state = str(row.get("lifecycle_state") or "unknown")
            family = LIFECYCLE_FAMILY.get(lifecycle_state, "neutral")
            state = none_if_nan(row.get("state"))
            county = none_if_nan(row.get("county"))
            raw_fields = load_raw_json(row.get("raw")) if policy.allows_raw else None

            props: dict[str, Any] = {
                "feature_type": "proposal",
                "public_id": public_id("prop", record_id),
                "slug": make_slug(name, record_id),
                "name": name,
                "kind": none_if_nan(row.get("kind")),
                "technology": none_if_nan(row.get("technology")),
                "technology_raw": none_if_nan(row.get("technology_raw")) if policy.allows_raw else None,
                "capacity_mw": none_if_nan(row.get("capacity_mw")),
                "storage_mwh": none_if_nan(row.get("storage_mwh")),
                "lifecycle_state": lifecycle_state,
                "lifecycle_family": family,
                "state": state,
                "county": county,
                "iso": none_if_nan(row.get("iso")),
                "jurisdiction": f"US-{state}" if state else ("GB" if source_id.startswith("gb.") else None),
                "sponsor": none_if_nan(row.get("sponsor_name")),
                "proposed_online_date": date_only(row.get("proposed_cod")),
                "queue_date": date_only(row.get("queue_date")),
                "queue_id": none_if_nan(row.get("queue_id")),
                "eia_plant_id": none_if_nan(row.get("eia_plant_id")),
                "eia_generator_id": none_if_nan(row.get("eia_generator_id")),
                "source_id": source_id,
                "source_name": meta["name"],
                "source_url": row.get("source_url"),
                "retrieved_at": retrieved_iso,
                "licence_id": row.get("licence_id"),
                "licence_text": meta["licence_text"],
                "reuse_class": meta["reuse_class"],
                "attribution_text": meta["attribution_text"],
                "allows_raw": policy.allows_raw,
                "status_raw": none_if_nan(row.get("status_raw")) if policy.allows_raw else None,
                "raw_fields": raw_fields,
            }

            point: tuple[float, float] | None = None
            precision = "unplaced"
            restricted_precision = False
            if policy.allows_raw:
                point = _extract_eia_latlon(raw_fields)
                if point:
                    precision = "exact"
            if point is None:
                cp = gaz.county_point(state, county)
                if cp:
                    point = cp
                    precision = "county_centroid"
                    restricted_precision = not policy.allows_raw

            props["location_precision"] = precision
            props["restricted_precision"] = restricted_precision

            if point:
                features.append(
                    {
                        "type": "Feature",
                        "geometry": {"type": "Point", "coordinates": [point[1], point[0]]},
                        "properties": props,
                    }
                )
                visible_here += 1
                family_counts[family] += 1
            elif state:
                agg = state_agg.setdefault(
                    state,
                    {
                        "state": state,
                        "jurisdiction": props["jurisdiction"],
                        "count": 0,
                        "capacity_mw_sum": 0.0,
                        "family_counts": dict.fromkeys(FAMILY_LABEL, 0),
                        "sample_public_ids": [],
                    },
                )
                agg["count"] += 1
                agg["capacity_mw_sum"] += float(props["capacity_mw"] or 0)
                agg["family_counts"][family] += 1
                if len(agg["sample_public_ids"]) < 5:
                    agg["sample_public_ids"].append(props["public_id"])
                visible_here += 1
                family_counts[family] += 1
            else:
                group = props["jurisdiction"] or "Unknown"
                unplaced_groups.setdefault(group, []).append(
                    {
                        "public_id": props["public_id"],
                        "slug": props["slug"],
                        "name": name,
                        "technology": props["technology"],
                        "capacity_mw": props["capacity_mw"],
                        "lifecycle_state": lifecycle_state,
                        "lifecycle_family": family,
                        "source_id": source_id,
                    }
                )
                visible_here += 1
                family_counts[family] += 1

        by_source[source_id] = visible_here
        sources_meta[source_id]["rows_visible"] = visible_here
        visible += visible_here

    for state, agg in state_agg.items():
        point = gaz.state_point(state)
        if not point:
            continue
        features.append(
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [point[1], point[0]]},
                "properties": {
                    "feature_type": "state_aggregate",
                    "state": state,
                    "jurisdiction": agg["jurisdiction"],
                    "count": agg["count"],
                    "capacity_mw_sum": round(agg["capacity_mw_sum"], 1),
                    "family_counts": agg["family_counts"],
                    "sample_public_ids": agg["sample_public_ids"],
                },
            }
        )

    unplaced = [
        {"group_label": group, "count": len(records), "records": records}
        for group, records in sorted(unplaced_groups.items())
    ]

    stats = BuildStats(
        generated_at=iso(pd.Timestamp(now)) or now.isoformat(),
        no_lag=no_lag,
        lag_days={"proposal": LAG_DAYS_PROPOSAL, "opportunity": LAG_DAYS_OPPORTUNITY},
        data_as_of={"proposal": latest_retrieved, "opportunity": None},
        proposals_total=total,
        proposals_visible=visible,
        proposals_by_source=by_source,
        proposal_family_counts=family_counts,
        unplaced=unplaced,
        sources=sources_meta,
    )
    return features, stats


def build_opportunities(
    *,
    registry: dict[str, dict[str, Any]],
    data_dir: Path | None,
    now: dt.datetime,
    no_lag: bool,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Return (opportunity list, stats-fragment). No source in this set supplies a service-
    territory polygon (`docs/21` §3.7 `location.kind`), so D-11's polygon rendering does not
    apply this sprint; opportunities show jurisdiction as text only, matching the `docs/31` §5.3
    Unplaced note for the opportunity list.
    """
    cutoff = now if no_lag else now - dt.timedelta(days=LAG_DAYS_OPPORTUNITY)
    out: list[dict[str, Any]] = []
    by_source: dict[str, int] = {}
    family_counts: dict[str, int] = dict.fromkeys(FAMILY_LABEL, 0)
    total = 0
    latest_retrieved: str | None = None
    sources_meta: dict[str, dict[str, Any]] = {}

    for source_id in OPPORTUNITY_SOURCE_IDS:
        meta = build_source_meta(source_id, registry, "opportunity")
        policy = SOURCE_POLICY[source_id]
        df = _load_source_frame(source_id, data_dir=data_dir, eval_df=None)
        sources_meta[source_id] = {**meta, "rows_visible": 0}
        if df is None:
            continue
        total += len(df)
        visible_here = 0
        for row in df.to_dict(orient="records"):
            retrieved_at_raw: Any = row.get("retrieved_at")
            retrieved_ts = pd.to_datetime(retrieved_at_raw, utc=True, errors="coerce")
            if pd.isna(retrieved_ts) or retrieved_ts.to_pydatetime() > cutoff:
                continue
            retrieved_iso = iso(retrieved_ts)
            if retrieved_iso and (latest_retrieved is None or retrieved_iso > latest_retrieved):
                latest_retrieved = retrieved_iso

            record_id = str(row["record_id"])
            title = str(row.get("title") or record_id)
            status = str(row.get("status") or "unknown")
            family = LIFECYCLE_FAMILY.get(status, "neutral")
            raw_fields = load_raw_json(row.get("raw")) if policy.allows_raw else None
            technologies_raw = row.get("technologies")
            technologies = [t for t in str(technologies_raw).split("|") if t] if technologies_raw else []

            item = {
                "public_id": public_id("opp", record_id),
                "slug": make_slug(title, record_id),
                "kind": none_if_nan(row.get("kind")),
                "issuer": none_if_nan(row.get("issuer")),
                "title": title,
                "summary": none_if_nan(row.get("summary")),
                "jurisdiction": none_if_nan(row.get("jurisdiction")),
                "technologies": technologies,
                "capacity_sought_mw": none_if_nan(row.get("capacity_sought_mw")),
                "budget_amount": none_if_nan(row.get("budget_amount")),
                "budget_currency": none_if_nan(row.get("budget_currency")),
                "open_at": date_only(row.get("open_at")),
                "due_at": iso(row.get("due_at")),
                "status": status,
                "lifecycle_family": family,
                "source_id": source_id,
                "source_name": meta["name"],
                "source_url": row.get("source_url"),
                "retrieved_at": retrieved_iso,
                "licence_id": row.get("licence_id"),
                "licence_text": meta["licence_text"],
                "reuse_class": meta["reuse_class"],
                "attribution_text": meta["attribution_text"],
                "allows_raw": policy.allows_raw,
                "status_raw": none_if_nan(row.get("status_raw")) if policy.allows_raw else None,
                "raw_fields": raw_fields,
            }
            out.append(item)
            visible_here += 1
            family_counts[family] += 1

        by_source[source_id] = visible_here
        sources_meta[source_id]["rows_visible"] = visible_here

    stats_fragment = {
        "opportunities_total": total,
        "opportunities_visible": len(out),
        "opportunities_by_source": by_source,
        "opportunity_family_counts": family_counts,
        "data_as_of_opportunity": latest_retrieved,
        "sources": sources_meta,
    }
    return out, stats_fragment


def build_all(
    *,
    data_dir: Path | None,
    sources_yaml: Path,
    eval_parquet: Path | None,
    out_dir: Path,
    no_lag: bool,
    now: dt.datetime | None = None,
) -> BuildStats:
    """Run the full build and write the three static files. Returns the stats object (also
    written as `stats.json`) so callers -- the pytest suite included -- can assert on it without
    re-reading the file.
    """
    now = now or dt.datetime.now(dt.UTC)
    registry = load_sources_yaml(sources_yaml)
    gaz = CountyGazetteer.load(COUNTY_CENTROID_TSV)
    eval_df = pd.read_parquet(eval_parquet) if eval_parquet else None

    features, stats = build_proposals(
        registry=registry, gaz=gaz, data_dir=data_dir, eval_df=eval_df, now=now, no_lag=no_lag
    )
    opportunities, opp_stats = build_opportunities(
        registry=registry, data_dir=data_dir, now=now, no_lag=no_lag
    )

    stats.opportunities_total = cast(int, opp_stats["opportunities_total"])
    stats.opportunities_visible = cast(int, opp_stats["opportunities_visible"])
    stats.opportunities_by_source = cast(dict[str, int], opp_stats["opportunities_by_source"])
    stats.opportunity_family_counts = cast(dict[str, int], opp_stats["opportunity_family_counts"])
    stats.data_as_of["opportunity"] = cast(str | None, opp_stats["data_as_of_opportunity"])
    stats.sources.update(cast(dict[str, dict[str, Any]], opp_stats["sources"]))

    out_dir.mkdir(parents=True, exist_ok=True)
    geojson = {"type": "FeatureCollection", "features": features}
    (out_dir / "proposals.geojson").write_text(json.dumps(geojson), encoding="utf-8")
    (out_dir / "opportunities.json").write_text(json.dumps(opportunities), encoding="utf-8")
    (out_dir / "stats.json").write_text(
        json.dumps(
            {
                "generated_at": stats.generated_at,
                "no_lag": stats.no_lag,
                "lag_days": stats.lag_days,
                "data_as_of": stats.data_as_of,
                "counts": {
                    "proposals_total": stats.proposals_total,
                    "proposals_visible": stats.proposals_visible,
                    "proposals_by_source": stats.proposals_by_source,
                    "opportunities_total": stats.opportunities_total,
                    "opportunities_visible": stats.opportunities_visible,
                    "opportunities_by_source": stats.opportunities_by_source,
                },
                "lifecycle_family_counts": {
                    "proposal": stats.proposal_family_counts,
                    "opportunity": stats.opportunity_family_counts,
                },
                "unplaced": stats.unplaced,
                "sources": stats.sources,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    log.info(
        "build complete",
        extra={
            "proposals_visible": stats.proposals_visible,
            "opportunities_visible": stats.opportunities_visible,
            "no_lag": no_lag,
        },
    )
    return stats


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--sources-yaml", type=Path, default=DEFAULT_SOURCES_YAML)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument(
        "--eval-parquet",
        type=Path,
        default=None,
        help="Use the combined data/eval/normalized.parquet fallback instead of --data-dir "
        "(proposals only; used by the pytest suite and for review before a full connector run).",
    )
    parser.add_argument(
        "--no-lag",
        action="store_true",
        help="Show records regardless of retrieved_at age (prototype only -- the delayed-tier "
        "notice still renders using the configured lag_days, docs/04 D-3).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = _parse_args(argv)
    data_dir = None if args.eval_parquet else args.data_dir
    stats = build_all(
        data_dir=data_dir,
        sources_yaml=args.sources_yaml,
        eval_parquet=args.eval_parquet,
        out_dir=args.out_dir,
        no_lag=args.no_lag,
    )
    print(  # CLI summary output, not application logging (T20 ignored for this file)
        f"proposals: {stats.proposals_visible}/{stats.proposals_total} visible | "
        f"opportunities: {stats.opportunities_visible}/{stats.opportunities_total} visible | "
        f"no_lag={stats.no_lag}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
