"""The prospecting layers per candidate segment (docs/25 "Prospecting layers", contract for the site
finder): `services.context_sample.sample_line` over the segment's 1-mile band, kept as numbers only.

Each segment stores, per layer of `SITE_FINDER_LAYERS`, its `status` and the values the drawer shows:
a raster's mean, min and max; a polygon layer's share of the band, its coverage share and its three
largest features by share (name, share; PAD-US adds the GAP status); PAD-US's hard-constraint share;
the statistical-area layer's qualifying share; and the combined energy-community share. What a value
is not, its provenance and its screening flag are the layer's, so they are stored once per run
(`layer_metadata`, `site_finder_run.layers`) and joined back by the API (`band_prospect`).

A band's values are the length-weighted means of its segments' (each segment's band has nearly the
same area); a band's features are the union of its segments' with their shares weighted the same way.
A layer's status for a band is the worst of its segments': `unavailable` or `withheld` anywhere means
no value, `partial` or `no_data` anywhere means the shares are lower bounds.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from services.context_sample.catalog import CATALOG, SCREENING_GRADE, SITE_FINDER_LAYERS

#: The band half-width the prospecting layers are sampled over (miles), and the lattice spacing:
#: about 270 points for a half-mile segment, which costs about 4.5 ms a segment warm on the Texas
#: build (1,500 points cost 15 ms) and keeps a share within a few per cent.
BUFFER_MI = 1.0
SPACING_M = 200.0
MAX_FEATURES = 3
#: Worst first: a band takes the first of these any of its segments has.
STATUS_ORDER = ("withheld", "unavailable", "no_data", "partial", "ok")


def compact(answer: dict[str, Any]) -> dict[str, Any]:
    """`sample_line`'s answer for one segment, as the numbers a candidate row keeps."""
    out: dict[str, Any] = {}
    for item in answer.get("layers") or []:
        lid = str(item["layer"])
        info = CATALOG.get(lid)
        rec: dict[str, Any] = {"st": item.get("status")}
        summary = item.get("summary")
        if isinstance(summary, dict) and item.get("status") in ("ok", "partial"):
            for key in ("mean", "min", "max"):
                if summary.get(key) is not None:
                    rec[key] = summary[key]
            if summary.get("coverage_share") is not None:
                rec["cov"] = summary["coverage_share"]
        for key, short in (("share", "share"), ("coverage_share", "cov"), ("hard_constraint_share", "hard")):
            if item.get(key) is not None:
                rec[short] = item[key]
        if item.get("qualifying_share") is not None:
            rec["q"] = item["qualifying_share"]
        feats = item.get("features") or []
        if feats and info is not None and info.name_field:
            rows = []
            for f in feats[:MAX_FEATURES]:
                row: list[Any] = [f.get(info.name_field), f.get("share")]
                if lid == "protected_lands":
                    row.append(f.get("gap_status"))
                rows.append(row)
            rec["f"] = rows
        out[lid] = rec
    ec = answer.get("energy_community")
    if isinstance(ec, dict):
        out["energy_community"] = {"st": ec.get("status"), "share": ec.get("share")}
    return out


def layer_metadata(sampler: Any, layers: Sequence[str] = SITE_FINDER_LAYERS) -> dict[str, Any]:
    """Per layer: what a reader needs beside a value, from the prospect build the run sampled."""
    out: dict[str, Any] = {}
    for lid in layers:
        info = CATALOG[lid]
        entry = sampler.entry(lid) if sampler is not None else None
        out[lid] = {
            "title": info.title,
            "unit": info.unit,
            "group": info.group,
            "kind": info.kind,
            "not": info.not_statement,
            "screening": SCREENING_GRADE,
            "source_id": info.source_id,
            "built": entry is not None,
            "provenance": (entry or {}).get("provenance"),
            "licence_name": (entry or {}).get("licence_name"),
        }
    return out


def _worst(statuses: Sequence[str | None]) -> str:
    present = [s for s in statuses if s]
    for status in STATUS_ORDER:
        if status in present:
            return status
    return "unavailable"


def _weighted(values: Sequence[tuple[float | None, float]]) -> float | None:
    pairs = [(v, w) for v, w in values if v is not None]
    total = sum(w for _, w in pairs)
    if not pairs or total <= 0:
        return None
    return sum(float(v) * w for v, w in pairs) / total


def band_prospect(
    segments: Sequence[tuple[dict[str, Any], float]], layers_meta: dict[str, Any]
) -> dict[str, Any]:
    """The prospecting layers for a band, from its segments' `(prospect, length_mi)` pairs and the
    run's `layers` metadata: one entry per layer with status, values, `not`, `screening` and
    `provenance`; plus the combined energy-community share."""
    out_layers: list[dict[str, Any]] = []
    for lid, meta in layers_meta.items():
        recs = [((p or {}).get(lid) or {}, w) for p, w in segments]
        status = _worst([r.get("st") for r, _ in recs]) if recs else "unavailable"
        entry: dict[str, Any] = {
            "layer": lid,
            "title": meta.get("title"),
            "unit": meta.get("unit"),
            "group": meta.get("group"),
            "status": status,
            "screening": meta.get("screening") or SCREENING_GRADE,
            "not": meta.get("not"),
            "provenance": meta.get("provenance") if status not in ("withheld", "unavailable") else None,
        }
        if status not in ("withheld", "unavailable"):
            if meta.get("kind") == "raster":
                mean = _weighted([(r.get("mean"), w) for r, w in recs])
                mins = [r["min"] for r, _ in recs if r.get("min") is not None]
                maxs = [r["max"] for r, _ in recs if r.get("max") is not None]
                entry["summary"] = {
                    "mean": round(mean, 3) if mean is not None else None,
                    "min": min(mins) if mins else None,
                    "max": max(maxs) if maxs else None,
                }
            else:
                for key, name in (
                    ("share", "share"),
                    ("hard", "hard_constraint_share"),
                    ("q", "qualifying_share"),
                ):
                    value = _weighted([(r.get(key), w) for r, w in recs])
                    if value is not None:
                        entry[name] = round(value, 4)
                cov = _weighted([(r.get("cov"), w) for r, w in recs])
                if cov is not None:
                    entry["coverage_share"] = round(cov, 4)
                total_w = sum(w for _, w in recs) or 1.0
                feats: dict[str, dict[str, Any]] = {}
                for r, w in recs:
                    for row in r.get("f") or []:
                        feature = str(row[0]) if row and row[0] is not None else None
                        if feature is None:
                            continue
                        f = feats.setdefault(feature, {"name": feature, "share": 0.0})
                        f["share"] += float(row[1] or 0.0) * w / total_w
                        if lid == "protected_lands" and len(row) > 2:
                            f["gap_status"] = row[2]
                entry["features"] = [
                    {**f, "share": round(f["share"], 4)}
                    for f in sorted(feats.values(), key=lambda f: -float(f["share"]))[:MAX_FEATURES]
                ]
        out_layers.append(entry)
    ec_recs = [((p or {}).get("energy_community") or {}, w) for p, w in segments]
    ec_status = _worst([r.get("st") for r, _ in ec_recs]) if ec_recs else "unavailable"
    ec_share = _weighted([(r.get("share"), w) for r, w in ec_recs])
    ec_meta = layers_meta.get("energy_community_coal_closure") or {}
    energy_community = {
        "status": ec_status,
        "share": round(ec_share, 4)
        if ec_share is not None and ec_status not in ("unavailable", "withheld")
        else None,
        "screening": SCREENING_GRADE,
        "not": ec_meta.get("not"),
        "dataset_as_of": {
            lid: ((layers_meta.get(lid) or {}).get("provenance") or {}).get("dataset_as_of")
            for lid in ("energy_community_coal_closure", "energy_community_ffe")
            if lid in layers_meta
        },
    }
    return {
        "buffer_mi": BUFFER_MI,
        "screening": SCREENING_GRADE,
        "layers": out_layers,
        "energy_community": energy_community,
    }
