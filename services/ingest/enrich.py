"""Merge the objective feature sets onto existing `asset` rows (ADR 0008 §4; docs/21 §3.22
`attributes`; owner decisions 2026-09-18 and 2026-09-19: objective features only, valuation
excluded).

One entry point, called once after the asset loads:

    apply_context_features(session, data_root) -> dict[str, Any]

It reads the three feature parquets this lane's connectors write under
`<data_root>/normalized/context/` and merges what it finds into assets that already exist. It
creates nothing -- no assets, no organisations, no aliases, no `asset_owner` edges -- so it is safe
to run at any point after the loaders, and a source whose parquet is absent is reported as
`missing` rather than failing the run.

* `us.phmsa.pipeline_operator_reports` -> `attributes.phmsa` (mileage, vintage, diameter mix,
  incident counts) on `gas_pipeline`, joined operator -> organisation -> that organisation's assets.
* `us.eia.form923` -> `attributes.capacity_factor_<year>` and `heat_rate_btu_kwh_<year>` (plus the
  two inputs) on `power_plant`, joined `Plant Id` == `asset.source_asset_id`.
* `us.epa.rfs_public_data` -> `attributes.rfs` (D codes, pathway count) on `ethanol_plant` and
  `rng_project`, joined facility name + state, then company name + state.

**Idempotent.** Every run recomputes each block from the parquet and compares it with what the row
already carries; an unchanged row is counted as `unchanged` and its `last_changed` is not touched.
Re-running with the same parquets is a no-op, and re-running after a fresh connector run moves only
the rows whose features moved.

**Matching the PHMSA operator names is the hard part** and is done in three deterministic steps, in
this order, with the step recorded on the row as `attributes.phmsa.matched_on`:

1. `org_key` -- `pipeline.normalize.org_key`, the one organisation key the resolver uses
   (`services/ingest/ownership.py`): legal forms and punctuation removed, industry and geography
   words kept. Applied over every `organization.name_canonical` and `organization_alias.alias`,
   it matches "TALLGRASS INTERSTATE GAS TRANSMISSION, LLC" to "Tallgrass Interstate Gas
   Transmission" unaided.
2. `alias_file` -- `data/vendored/organizations/external_operator_aliases.yaml`, the curated,
   evidence-carrying list (PHMSA's "RUBY LLC" is the EIA Atlas's "Ruby Pipeline LLC"). A curated
   rule wins over step 1's answer where both fire, because a human checked it. That file is not
   `aliases.yaml`, which records renames of one legal entity; its own header explains the split.
3. `expanded_key` -- the same key computed after expanding the abbreviations the Atlas
   layer uses in operator strings (`PL` -> pipeline, `Trans` -> transmission, `Nat` -> natural,
   `Sys` -> system, `Elec` -> electric, `Am` -> america, parentheticals dropped). Only fires when
   the expanded key is unique on both sides *and* has at least two tokens, so a bare surname
   shared by two unrelated operators cannot carry another company's mileage.

No fuzzy scoring runs here: a pair either meets one of those three rules or the features do not
reach the asset, and the unmatched count is reported.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import re
import time
from collections import defaultdict
from typing import Any

import pandas as pd
import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from pipeline.connectors.base import json_default
from pipeline.normalize import org_key
from services.db.models import Asset, AssetOwner, Organization, OrganizationAlias
from services.db.session import get_engine, get_sessionmaker, init_db

DEFAULT_DB_PATH = pathlib.Path("web/.data/dev.db")
DEFAULT_DATA_ROOT = pathlib.Path("data")
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
ALIASES_PATH = _REPO_ROOT / "data" / "vendored" / "organizations" / "external_operator_aliases.yaml"

PHMSA_SOURCE_ID = "us.phmsa.pipeline_operator_reports"
EIA923_SOURCE_ID = "us.eia.form923"
RFS_SOURCE_ID = "us.epa.rfs_public_data"

#: `asset.attributes` key per block. The EIA-923 features are flat, year-suffixed keys (docs/21
#: §3.22's own example: `{"heat_rate_btu_kwh": 7150, "capacity_factor_2025": 0.41}`); PHMSA and RFS
#: are nested dicts because they carry sub-tables (mileage by decade, the D-code list).
PHMSA_KEY = "phmsa"
RFS_KEY = "rfs"
FLAGS_KEY = "feature_flags"
EIA923_FLAG_PREFIX = "eia923:"

#: Physically impossible derived values are stored as null with a flag rather than clamped
#: (task rule, and docs/04's "never invent a number"): a capacity factor above this is a capacity
#: or a generation figure that disagrees with the other, not a plant running at 110%.
MAX_PLAUSIBLE_CAPACITY_FACTOR = 1.05
#: Thermal heat-rate window. Below ~5,000 Btu/kWh implies >68% thermal efficiency (no fleet unit
#: does that); above 30,000 implies <11%, which in practice means a misreported fuel or a plant
#: whose net generation is near zero.
MIN_PLAUSIBLE_HEAT_RATE = 5_000.0
MAX_PLAUSIBLE_HEAT_RATE = 30_000.0
HOURS_PER_YEAR = 8_760

#: EIA-923 fuel codes whose "fuel consumption" is not fuel burned at the plant. EIA fills the
#: MMBtu column for non-combustion generation at the 3,412 Btu/kWh energy equivalence (measured on
#: the 2025 final file: Bankhead Dam, hydro, reports 514,451 MMBtu against 150,777 MWh -- exactly
#: 3,412), so "fuel consumption > 0" is *not* the test for a thermal plant: it would hand every
#: hydro, wind and solar plant a 3,412 Btu/kWh "heat rate", or, with the plausibility window
#: applied, a flag on ~10,000 plants that are simply not thermal. A plant is thermal here when it
#: reports at least one combustible fuel code.
NON_COMBUSTION_FUEL_CODES = frozenset({"SUN", "WND", "WAT", "GEO", "MWH", "PUR", "WH"})

#: Abbreviations the EIA Atlas operator strings use; expanded before the step-3 key comparison.
_ABBREVIATIONS: dict[str, str] = {
    "pl": "pipeline",
    "trans": "transmission",
    "transm": "transmission",
    "nat": "natural",
    "elec": "electric",
    "sys": "system",
    "systems": "system",
    "am": "america",
    "gath": "gathering",
    "distr": "distribution",
}
_PARENTHETICAL = re.compile(r"\s*\([^)]*\)")
#: Leading "Project #1 - " / "Project #2, Expansion #1 - " on EPA LMOP project names.
_LMOP_PREFIX = re.compile(r"^project\s*#\d+(,\s*expansion\s*#\d+)?\s*-\s*", re.IGNORECASE)


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


# ------------------------------------------------------------------------------------- helpers
def _text(value: Any) -> str:
    """Any scalar (including `pd.NA`, `NaN` and `None`, which a parquet column hands back) as a
    plain string; missing becomes empty. `str(value or "")` is not safe here: `bool(pd.NA)` raises."""
    if value is None:
        return ""
    if not isinstance(value, str):
        try:
            if pd.isna(value):
                return ""
        except (TypeError, ValueError):
            pass
    return str(value)


def _expanded_key(name: Any) -> str:
    """`org_key` over an operator string with its abbreviations expanded and any parenthetical
    dropped: "Natural Gas PL Co of Am" and "NATURAL GAS PIPELINE CO OF AMERICA (KMI)" both give
    NATURAL GAS PIPELINE OF AMERICA."""
    text = _PARENTHETICAL.sub(" ", _text(name).lower())
    text = re.sub(r"[^a-z0-9 ]", " ", text).replace("pipe line", "pipeline")
    return org_key(" ".join(_ABBREVIATIONS.get(t, t) for t in text.split()))


def _plain_key(name: Any) -> str:
    return org_key(_text(name))


def _facility_key(name: Any) -> str:
    """Comparison key for a facility name: the LMOP project prefix and any "(City, ST)" suffix
    removed, then the shared `org_key`."""
    text = _LMOP_PREFIX.sub("", _text(name).strip())
    text = _PARENTHETICAL.sub("", text)
    return org_key(text)


def _state(value: Any) -> str:
    text = _text(value).strip().upper()
    if text.startswith("US-"):
        text = text[3:]
    return text if re.fullmatch(r"[A-Z]{2}", text) else ""


def _clean(value: Any) -> Any:
    """Parquet/pandas scalars -> plain Python, `NaN`/`NaT`/`pd.NA` -> None, numpy arrays -> list."""
    if value is None:
        return None
    if isinstance(value, list | tuple):
        return [_clean(v) for v in value]
    if hasattr(value, "tolist") and not isinstance(value, str):
        listed = value.tolist()
        return [_clean(v) for v in listed] if isinstance(listed, list) else _clean(listed)
    if isinstance(value, dict):
        return {str(k): _clean(v) for k, v in value.items()}
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        return float(value)
    return value


def _json_field(value: Any) -> Any:
    """A parquet column written by `to_parquet_safe` may hold a JSON string or the object."""
    cleaned = _clean(value)
    if isinstance(cleaned, str):
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            return cleaned
    return cleaned


def _read_parquet(context_dir: pathlib.Path, source_id: str) -> pd.DataFrame | None:
    path = context_dir / f"{source_id}.parquet"
    if not path.exists():
        return None
    frame = pd.read_parquet(path)
    frame.attrs["path"] = str(path)
    return frame


def _set_block(asset: Asset, key: str, payload: dict[str, Any], now: dt.datetime) -> bool:
    """Merge one nested feature block; returns True when the row actually changed."""
    attributes = dict(asset.attributes or {})
    if attributes.get(key) == payload:
        return False
    attributes[key] = payload
    asset.attributes = attributes
    asset.last_changed = now
    return True


def _set_flat(asset: Asset, values: dict[str, Any], flags: list[str], prefix: str, now: dt.datetime) -> bool:
    """Merge flat feature keys plus this block's `feature_flags`, leaving other lanes' flags (and
    any other attribute) untouched."""
    attributes = dict(asset.attributes or {})
    existing_flags = [f for f in (attributes.get(FLAGS_KEY) or []) if not str(f).startswith(prefix)]
    merged_flags = sorted({*existing_flags, *(f"{prefix}{f}" for f in flags)})
    candidate = {**attributes, **values}
    if merged_flags:
        candidate[FLAGS_KEY] = merged_flags
    elif FLAGS_KEY in candidate and not existing_flags:
        candidate.pop(FLAGS_KEY)
    if candidate == attributes:
        return False
    asset.attributes = candidate
    asset.last_changed = now
    return True


# ------------------------------------------------------------------------------- alias file
def read_alias_rules(path: pathlib.Path = ALIASES_PATH) -> dict[str, list[dict[str, Any]]]:
    """`{source_id: [rule, ...]}` from the curated alias file; an absent file is an empty map (the
    resolver then relies on the shared key and the expansion alone)."""
    if not path.exists():
        return {}
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    rows = payload.get("aliases") if isinstance(payload, dict) else payload
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for index, row in enumerate(rows or []):
        if not isinstance(row, dict):
            raise ValueError(f"{path}: row {index} is not a mapping")
        missing = [k for k in ("alias", "organization", "source_id") if not row.get(k)]
        if missing:
            raise ValueError(f"{path}: row {index} lacks {missing}")
        grouped[str(row["source_id"])].append(row)
    return dict(grouped)


# --------------------------------------------------------------------------------------- PHMSA
def _phmsa_payload(row: dict[str, Any], matched_on: str) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "operator_id": _clean(row.get("operator_id")),
        "operator_name": _clean(row.get("operator_name")),
        "report_year": _clean(row.get("report_year")),
        "onshore_transmission_miles": _clean(row.get("onshore_transmission_miles")),
        "miles_by_decade": _json_field(row.get("miles_by_decade")) or {},
        "miles_by_diameter": _json_field(row.get("miles_by_diameter")) or {},
        "incidents_5y_total": _clean(row.get("incidents_5y_total")),
        "incidents_5y_significant": _clean(row.get("incidents_5y_significant")),
        "incidents_5y_with_fatality": _clean(row.get("incidents_5y_with_fatality")),
        "incidents_5y_with_injury": _clean(row.get("incidents_5y_with_injury")),
        "incidents_5y_with_ignition": _clean(row.get("incidents_5y_with_ignition")),
        "incidents_5y_with_explosion": _clean(row.get("incidents_5y_with_explosion")),
        "incident_years": _json_field(row.get("incident_years")) or [],
        "matched_on": matched_on,
        "source_id": PHMSA_SOURCE_ID,
        "source_url": _clean(row.get("source_url")),
        "retrieved_at": _clean(row.get("retrieved_at")),
        FLAGS_KEY: _json_field(row.get("feature_flags")) or [],
    }
    return payload


def apply_phmsa(
    session: Session,
    frame: pd.DataFrame,
    *,
    alias_rules: list[dict[str, Any]],
    now: dt.datetime,
) -> dict[str, Any]:
    """`attributes.phmsa` on every `gas_pipeline` asset whose operator resolves to a PHMSA
    operator (module docstring for the three matching steps)."""
    assets = list(session.scalars(select(Asset).where(Asset.asset_type == "gas_pipeline")).unique())
    result: dict[str, Any] = {
        "status": "ok",
        "rows": len(frame),
        "assets_total": len(assets),
        "assets_matched": 0,
        "updated": 0,
        "unchanged": 0,
        "matched_by": {},
        "operators_matched": 0,
        "unmatched_operator_examples": [],
    }
    if not len(frame) or not assets:
        result["match_rate"] = 0.0
        return result

    rows = [{str(k): v for k, v in r.items()} for r in frame.to_dict("records")]
    by_plain: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_expanded: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_operator_id: dict[str, dict[str, Any]] = {}
    for row in rows:
        name = row.get("operator_name")
        by_operator_id[str(row.get("operator_id"))] = row
        if key := _plain_key(name):
            by_plain[key].append(row)
        if key := _expanded_key(name):
            by_expanded[key].append(row)

    # Step 2's curated rules, keyed by the organisation they name.
    alias_by_org: dict[str, dict[str, Any]] = {}
    inert_rules: list[str] = []
    for rule in alias_rules:
        external_id = str(rule.get("external_id") or "")
        rule_row: dict[str, Any] | None = by_operator_id.get(external_id)
        if rule_row is None:
            candidates = by_plain.get(_plain_key(rule["alias"]), [])
            rule_row = candidates[0] if len(candidates) == 1 else None
        if rule_row is None:
            inert_rules.append(str(rule["alias"]))
            continue
        alias_by_org[_plain_key(rule["organization"])] = rule_row

    # Every spelling an asset can be reached by: its own operator string, plus the canonical name
    # and every recorded alias of each organisation it has an operator/owner edge to. Aliases
    # matter because `services/ingest/ownership.py` records each raw filing spelling as one, so a
    # registry that spells the operator differently from the map layer can still resolve.
    org_names: dict[Any, list[str]] = defaultdict(list)
    for org in session.scalars(select(Organization)):
        org_names[org.id].append(org.name_canonical)
    for alias, organization_id in session.execute(
        select(OrganizationAlias.alias, OrganizationAlias.organization_id)
    ).all():
        org_names[organization_id].append(alias)
    edges: dict[Any, list[Any]] = defaultdict(list)
    for edge in session.scalars(select(AssetOwner).where(AssetOwner.role.in_(("operator", "owner")))):
        edges[edge.asset_id].append(edge.organization_id)

    matched_operator_ids: set[str] = set()
    unmatched: list[str] = []
    for asset in assets:
        names = [asset.operator_name]
        for organization_id in edges.get(asset.id, []):
            names.extend(org_names.get(organization_id, []))
        names = [n for n in names if n]
        matched_row: dict[str, Any] | None = None
        matched_on = ""
        for name in names:  # curated rules first: a human checked them
            if (hit := alias_by_org.get(_plain_key(name))) is not None:
                matched_row, matched_on = hit, "alias_file"
                break
        if matched_row is None:
            for name in names:
                candidates = by_plain.get(_plain_key(name), [])
                if len(candidates) == 1:
                    matched_row, matched_on = candidates[0], "org_key"
                    break
        if matched_row is None:
            for key in (_expanded_key(n) for n in names):
                candidates = by_expanded.get(key, [])
                if key and len(key.split()) >= 2 and len(candidates) == 1:
                    matched_row, matched_on = candidates[0], "expanded_key"
                    break
        if matched_row is None:
            if asset.operator_name:
                unmatched.append(str(asset.operator_name))
            continue
        matched_operator_ids.add(str(matched_row.get("operator_id")))
        result["assets_matched"] += 1
        result["matched_by"][matched_on] = result["matched_by"].get(matched_on, 0) + 1
        if _set_block(asset, PHMSA_KEY, _phmsa_payload(matched_row, matched_on), now):
            result["updated"] += 1
        else:
            result["unchanged"] += 1

    result["operators_matched"] = len(matched_operator_ids)
    result["match_rate"] = round(result["assets_matched"] / len(assets), 3)
    result["unmatched_operator_examples"] = sorted(set(unmatched))[:10]
    result["alias_rules"] = len(alias_rules)
    result["alias_rules_without_a_phmsa_operator"] = sorted(set(inert_rules))
    return result


# -------------------------------------------------------------------------------------- EIA-923
def derive_plant_features(
    *,
    capacity_mw: float | None,
    net_generation_mwh: float | None,
    total_fuel_mmbtu: float | None,
    year: int,
    fuel_types: list[str] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """`({attribute: value}, [flag, ...])` for one plant-year.

    Capacity factor is net generation over nameplate capacity times 8,760 h; heat rate is fuel
    energy over net generation, for thermal plants only -- a plant that burns something
    (`NON_COMBUSTION_FUEL_CODES`) and reported fuel. Nothing is clamped: a value outside the
    plausible window is stored as `None` with a flag naming what was seen, so a reader can tell
    "we do not know" from "it is low".
    """
    values: dict[str, Any] = {
        f"net_generation_mwh_{year}": None if net_generation_mwh is None else round(net_generation_mwh, 3),
        f"fuel_mmbtu_{year}": None if total_fuel_mmbtu is None else round(total_fuel_mmbtu, 3),
    }
    flags: list[str] = []

    cf_key = f"capacity_factor_{year}"
    if not capacity_mw or capacity_mw <= 0:
        values[cf_key] = None
        flags.append(f"{cf_key}_null: asset has no nameplate capacity")
    elif net_generation_mwh is None:
        values[cf_key] = None
        flags.append(f"{cf_key}_null: no net generation reported")
    elif net_generation_mwh <= 0:
        values[cf_key] = None
        flags.append(
            f"{cf_key}_null: net generation {round(net_generation_mwh, 1)} MWh is not positive "
            "(the plant consumed at least as much as it produced over the year)"
        )
    else:
        factor = net_generation_mwh / (capacity_mw * HOURS_PER_YEAR)
        if factor > MAX_PLAUSIBLE_CAPACITY_FACTOR:
            values[cf_key] = None
            flags.append(
                f"{cf_key}_null: computed {round(factor, 3)} exceeds {MAX_PLAUSIBLE_CAPACITY_FACTOR}"
            )
        else:
            values[cf_key] = round(factor, 4)

    hr_key = f"heat_rate_btu_kwh_{year}"
    combusts = bool(set(fuel_types or []) - NON_COMBUSTION_FUEL_CODES)
    if combusts and total_fuel_mmbtu is not None and total_fuel_mmbtu > 0:
        if net_generation_mwh is None or net_generation_mwh <= 0:
            values[hr_key] = None
            flags.append(f"{hr_key}_null: fuel burned but net generation is not positive")
        else:
            heat_rate = total_fuel_mmbtu * 1_000 / net_generation_mwh
            if MIN_PLAUSIBLE_HEAT_RATE <= heat_rate <= MAX_PLAUSIBLE_HEAT_RATE:
                values[hr_key] = round(heat_rate, 1)
            else:
                values[hr_key] = None
                flags.append(
                    f"{hr_key}_null: computed {round(heat_rate, 1)} outside "
                    f"{int(MIN_PLAUSIBLE_HEAT_RATE)}-{int(MAX_PLAUSIBLE_HEAT_RATE)} Btu/kWh"
                )
    return values, flags


def apply_eia923(session: Session, frame: pd.DataFrame, *, now: dt.datetime) -> dict[str, Any]:
    """Year-suffixed operating features on `power_plant` assets, joined on the EIA plant id."""
    assets = {
        a.source_asset_id: a for a in session.scalars(select(Asset).where(Asset.asset_type == "power_plant"))
    }
    result: dict[str, Any] = {
        "status": "ok",
        "rows": len(frame),
        "assets_total": len(assets),
        "assets_matched": 0,
        "updated": 0,
        "unchanged": 0,
        "with_capacity_factor": 0,
        "with_heat_rate": 0,
        "flagged": 0,
        "rows_without_an_asset": 0,
    }
    if not len(frame) or not assets:
        result["match_rate"] = 0.0
        return result

    for record in frame.to_dict("records"):
        row = {str(k): v for k, v in record.items()}
        plant_id = str(_clean(row.get("plant_id")) or "")
        asset = assets.get(plant_id)
        if asset is None:
            result["rows_without_an_asset"] += 1
            continue
        year = _clean(row.get("data_year"))
        if year is None:
            continue
        fuel_types = [str(c).upper() for c in (_json_field(row.get("fuel_types")) or [])]
        values, flags = derive_plant_features(
            capacity_mw=None if asset.capacity_mw is None else float(asset.capacity_mw),
            net_generation_mwh=_clean(row.get("net_generation_mwh")),
            total_fuel_mmbtu=_clean(row.get("total_fuel_mmbtu")),
            year=int(year),
            fuel_types=fuel_types,
        )
        result["assets_matched"] += 1
        if values.get(f"capacity_factor_{int(year)}") is not None:
            result["with_capacity_factor"] += 1
        if values.get(f"heat_rate_btu_kwh_{int(year)}") is not None:
            result["with_heat_rate"] += 1
        if flags:
            result["flagged"] += 1
        if _set_flat(asset, values, flags, EIA923_FLAG_PREFIX, now):
            result["updated"] += 1
        else:
            result["unchanged"] += 1

    result["match_rate"] = round(result["assets_matched"] / len(assets), 3)
    return result


# ------------------------------------------------------------------------------------------ RFS
def apply_rfs(session: Session, frame: pd.DataFrame, *, now: dt.datetime) -> dict[str, Any]:
    """`attributes.rfs` on `ethanol_plant` and `rng_project` assets, matched on facility name +
    state first and company name + state second (both through the shared `org_key`). A key
    that names more than one registered facility is left unmatched and counted, never guessed."""
    assets = list(
        session.scalars(select(Asset).where(Asset.asset_type.in_(("ethanol_plant", "rng_project")))).unique()
    )
    result: dict[str, Any] = {
        "status": "ok",
        "rows": len(frame),
        "assets_total": len(assets),
        "assets_matched": 0,
        "updated": 0,
        "unchanged": 0,
        "matched_by": {},
        "by_asset_type": {},
        "ambiguous": 0,
    }
    if not len(frame) or not assets:
        result["match_rate"] = 0.0
        return result

    facility_index: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    company_index: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for record in frame.to_dict("records"):
        row = {str(k): v for k, v in record.items()}
        state = _state(row.get("facility_state"))
        if not state:
            continue
        if key := _facility_key(row.get("facility_name")):
            facility_index[(key, state)].append(row)
        if key := _facility_key(row.get("company_name")):
            company_index[(key, state)].append(row)

    totals: dict[str, int] = defaultdict(int)
    matched_counts: dict[str, int] = defaultdict(int)
    for asset in assets:
        totals[asset.asset_type] += 1
        state = _state(asset.state_code)
        if not state:
            continue
        keys = [k for k in (_facility_key(asset.name), _facility_key(asset.operator_name)) if k]
        matched_row: dict[str, Any] | None = None
        matched_on = ""
        ambiguous = False
        for index, label in ((facility_index, "facility_name"), (company_index, "company_name")):
            for key in keys:
                candidates = index.get((key, state), [])
                if len(candidates) == 1:
                    matched_row, matched_on = candidates[0], label
                    break
                if len(candidates) > 1:
                    ambiguous = True
            if matched_row is not None:
                break
        if matched_row is None:
            if ambiguous:
                result["ambiguous"] += 1
            continue
        payload = {
            "d_codes": _json_field(matched_row.get("d_codes")) or [],
            "pathway_count": _clean(matched_row.get("pathway_count")),
            "first_registered_year": _clean(matched_row.get("first_registered_year")),
            "facility_name": _clean(matched_row.get("facility_name")),
            "company_name": _clean(matched_row.get("company_name")),
            "facility_type": _clean(matched_row.get("facility_type")),
            "matched_on": matched_on,
            "source_id": RFS_SOURCE_ID,
            "source_url": _clean(matched_row.get("source_url")),
            "retrieved_at": _clean(matched_row.get("retrieved_at")),
            FLAGS_KEY: _json_field(matched_row.get("feature_flags")) or [],
        }
        result["assets_matched"] += 1
        matched_counts[asset.asset_type] += 1
        result["matched_by"][matched_on] = result["matched_by"].get(matched_on, 0) + 1
        if _set_block(asset, RFS_KEY, payload, now):
            result["updated"] += 1
        else:
            result["unchanged"] += 1

    result["match_rate"] = round(result["assets_matched"] / len(assets), 3)
    result["by_asset_type"] = {
        asset_type: {
            "assets": count,
            "matched": matched_counts.get(asset_type, 0),
            "match_rate": round(matched_counts.get(asset_type, 0) / count, 3) if count else 0.0,
        }
        for asset_type, count in sorted(totals.items())
    }
    return result


# ------------------------------------------------------------------------------- entry point
def apply_context_features(session: Session, data_root: pathlib.Path) -> dict[str, Any]:
    """Merge every available feature parquet under `<data_root>/normalized/context/` into the
    assets already loaded, and return one report per source (module docstring for the rules).

    Idempotent: running it twice over the same parquets leaves the second run reporting only
    `unchanged`. Creates nothing; a source whose parquet is absent is reported `missing`.
    """
    t0 = time.monotonic()
    context_dir = pathlib.Path(data_root) / "normalized" / "context"
    now = utcnow()
    alias_rules = read_alias_rules(ALIASES_PATH)
    report: dict[str, Any] = {"data_root": str(data_root), "context_dir": str(context_dir)}

    for source_id, key in (
        (PHMSA_SOURCE_ID, "phmsa"),
        (EIA923_SOURCE_ID, "eia923"),
        (RFS_SOURCE_ID, "rfs"),
    ):
        frame = _read_parquet(context_dir, source_id)
        if frame is None:
            report[key] = {
                "status": "missing",
                "path": str(context_dir / f"{source_id}.parquet"),
                "assets_matched": 0,
                "match_rate": 0.0,
            }
            continue
        if key == "phmsa":
            report[key] = apply_phmsa(
                session, frame, alias_rules=alias_rules.get(PHMSA_SOURCE_ID, []), now=now
            )
        elif key == "eia923":
            report[key] = apply_eia923(session, frame, now=now)
        else:
            report[key] = apply_rfs(session, frame, now=now)
        report[key]["parquet"] = frame.attrs.get("path")

    session.commit()
    report["elapsed_s"] = round(time.monotonic() - t0, 2)
    return report


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--data-root", type=pathlib.Path, default=DEFAULT_DATA_ROOT)
    parser.add_argument("--db", type=pathlib.Path, default=DEFAULT_DB_PATH)
    args = parser.parse_args(argv)

    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        args.db.parent.mkdir(parents=True, exist_ok=True)
        db_url = f"sqlite+pysqlite:///{args.db}"
    engine = get_engine(db_url)
    init_db(engine)
    session = get_sessionmaker(engine)()
    try:
        report = apply_context_features(session, args.data_root)
    finally:
        session.close()
    print(json.dumps(report, default=json_default))  # noqa: T201 — CLI summary line


if __name__ == "__main__":
    main()
