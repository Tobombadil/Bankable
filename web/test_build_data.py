"""Builds the static site data from `data/eval/normalized.parquet` (the versioned fallback,
`pipeline/README.md`) and asserts the provenance quartet plus the tier/gating rules `docs/04`
DA-2/D-9/E-13 require: every feature/opportunity carries `source_id`, `source_url`,
`retrieved_at`, `licence_id`; a `restricted` source never appears; CAISO/NYISO never render an
exact point (D-9); the delayed-tier notice fields are present regardless of `--no-lag`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from web.build_data import LAG_DAYS_OPPORTUNITY, LAG_DAYS_PROPOSAL, build_all

REPO_ROOT = Path(__file__).resolve().parent.parent
EVAL_PARQUET = REPO_ROOT / "data" / "eval" / "normalized.parquet"
SOURCES_YAML = REPO_ROOT / "data" / "sources.yaml"
# CLAUDE.md / docs/04 DA-2: source_id, source_url, retrieved_at, licence carried by every record.
PROVENANCE_FIELDS = ("source_id", "source_url", "retrieved_at", "licence_id")

# Restricted/unknown sources present in the eval fallback (docs/13-legal-data-rights.md); a
# passing build must exclude every row from these, not merely relabel them.
RESTRICTED_SHORT_IDS = {"spp", "isone"}


@pytest.fixture()
def built(tmp_path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    out_dir = tmp_path / "data"
    build_all(
        data_dir=None,
        sources_yaml=SOURCES_YAML,
        eval_parquet=EVAL_PARQUET,
        out_dir=out_dir,
        no_lag=True,
    )
    geojson = json.loads((out_dir / "proposals.geojson").read_text())
    opportunities = json.loads((out_dir / "opportunities.json").read_text())
    stats = json.loads((out_dir / "stats.json").read_text())
    return geojson["features"], opportunities, stats


def test_eval_fallback_produces_features(
    built: tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]],
) -> None:
    features, _opportunities, stats = built
    assert len(features) > 0
    assert stats["counts"]["proposals_visible"] > 0


def test_every_proposal_feature_carries_provenance_quartet(
    built: tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]],
) -> None:
    features, _opportunities, _stats = built
    proposal_features = [f for f in features if f["properties"]["feature_type"] == "proposal"]
    assert proposal_features, "expected at least one point-placed proposal feature"
    for feature in proposal_features:
        props = feature["properties"]
        for field_name in PROVENANCE_FIELDS:
            assert props.get(field_name), f"{field_name} missing on {props.get('public_id')}"


def test_every_unplaced_and_state_aggregate_record_traces_to_a_source(
    built: tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]],
) -> None:
    features, _opportunities, stats = built
    for group in stats["unplaced"]:
        for record in group["records"]:
            assert record["source_id"], "unplaced record missing source_id"
    for feature in features:
        if feature["properties"]["feature_type"] == "state_aggregate":
            assert feature["properties"]["sample_public_ids"], "empty state aggregate marker"


def test_restricted_sources_never_appear(
    built: tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]],
) -> None:
    features, _opportunities, stats = built
    seen_source_ids = {f["properties"].get("source_id") for f in features}
    seen_source_ids |= {r["source_id"] for group in stats["unplaced"] for r in group["records"]}
    for short_id in RESTRICTED_SHORT_IDS:
        assert short_id not in seen_source_ids
    assert "us.iso.spp.gen_queue" not in seen_source_ids
    assert "us.iso.isone.gen_queue" not in seen_source_ids
    assert set(stats["counts"]["proposals_by_source"]).issubset(
        {
            "us.iso.ercot.gen_queue",
            "us.iso.caiso.gen_queue",
            "us.iso.nyiso.gen_queue",
            "us.eia.860m",
            "gb.neso.tec_register",
        }
    )


def test_restricted_precision_never_yields_an_exact_caiso_or_nyiso_point(
    built: tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]],
) -> None:
    """docs/04 D-9: a source with `allows_raw = False` never renders an exact point."""
    features, _opportunities, _stats = built
    for feature in features:
        props = feature["properties"]
        if props.get("source_id") in {"us.iso.caiso.gen_queue", "us.iso.nyiso.gen_queue"}:
            assert props["location_precision"] != "exact"
            if props["location_precision"] == "county_centroid":
                assert props["restricted_precision"] is True


def test_delayed_tier_fields_present_regardless_of_no_lag(
    built: tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]],
) -> None:
    _features, _opportunities, stats = built
    assert stats["no_lag"] is True
    assert stats["lag_days"]["proposal"] == LAG_DAYS_PROPOSAL
    assert stats["lag_days"]["opportunity"] == LAG_DAYS_OPPORTUNITY


def test_lag_filtering_excludes_freshly_retrieved_rows_without_no_lag(tmp_path: Path) -> None:
    """Without `--no-lag`, today's fixtures (retrieved "now") are older than 0 days, never older
    than the 14/7-day lag, so the public build is empty -- proving the filter is real, not a
    no-op (docs/04 D-3).
    """
    out_dir = tmp_path / "data"
    stats = build_all(
        data_dir=None,
        sources_yaml=SOURCES_YAML,
        eval_parquet=EVAL_PARQUET,
        out_dir=out_dir,
        no_lag=False,
    )
    assert stats.proposals_visible == 0
    assert stats.no_lag is False
