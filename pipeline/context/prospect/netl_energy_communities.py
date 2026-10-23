"""IRA energy-community bonus areas: DOE NETL's coal closure tracts and fossil-fuel-employment areas.

Source: the IRA Energy Community Data Layers that DOE NETL publishes with the Interagency Working Group
and Treasury, served as ArcGIS feature services on ``arcgis.netl.doe.gov`` (measured 2026-10-22):

- ``Hosted/2024_Coal_Closure_Energy_Communities``: census tracts with a coal mine closure since 1999 or a
  coal-fired unit retirement since 2009, and the tracts directly adjoining them ("as of June 7, 2024");
  fields ``mine_closure``, ``generator_closure``, ``adjacent_to_closure``, ``geoid_tract_2020``;
- ``Hosted/2024_MSAs_NonMSAs_that_are_Energy_Communities``: counties of the MSAs and non-MSAs that meet
  both the fossil-fuel employment (FFE) threshold and the unemployment test ("as of May 31, 2024, and
  until the release of the 2025 ... Update");
- ``Hosted/2024_MSAs_NonMSAs_that_only_meet_the_FFE_Threshold``: the counties of the areas that meet the
  FFE threshold but not the unemployment test, so are not energy communities that year.

The two statistical-area services become one layer, ``energy_community_ffe``, whose features say whether
the area meets the FFE threshold (all of them do) and whether it is an energy community that year
(``is_energy_community``). NETL's EDX download pages are not used: they refuse the project User-Agent and
EDX's robots.txt disallows its resource downloads (data/sources.yaml `us.doe.netl.energy_communities`).

Terms: "License: Creative Commons Attribution" with NETL's citation, and the line every served value
repeats: "These spatial data may not be relied upon by taxpayers to substantiate a tax return position or
for determining whether certain penalties apply and will not be used by the IRS for examination purposes."
The build re-reads each item's licenseInfo and stops if either clause is gone.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import time
from typing import Any

import pyarrow as pa  # type: ignore[import-untyped, unused-ignore]

from pipeline.connectors.http import PoliteSession
from pipeline.connectors.store import Store
from pipeline.context.prospect.arcgis import query_geojson
from pipeline.context.prospect.common import (
    REGION_STATES,
    REGIONS,
    FetchLog,
    now_iso,
    output_dir,
    provenance_for,
    snapshot_time,
    verify_terms,
)
from pipeline.context.prospect.coverage import for_state_filtered, public
from pipeline.context.prospect.geoparquet import write_geoparquet

SOURCE_ID = "us.doe.netl.energy_communities"
LICENCE_NAME = "Creative Commons Attribution (DOE NETL; version not stated)"
HOSTED = "https://arcgis.netl.doe.gov/server/rest/services/Hosted"
SERVICES = {
    "coal": f"{HOSTED}/2024_Coal_Closure_Energy_Communities/FeatureServer",
    "ec": f"{HOSTED}/2024_MSAs_NonMSAs_that_are_Energy_Communities/FeatureServer",
    "ffe_only": f"{HOSTED}/2024_MSAs_NonMSAs_that_only_meet_the_FFE_Threshold/FeatureServer",
}
TERMS_PHRASES = (
    "These spatial data may not be relied upon by taxpayers to substantiate a tax return position",
    "License: Creative Commons Attribution.",
)
TAXPAYER_LINE = (
    "These spatial data may not be relied upon by taxpayers to substantiate a tax return position or for "
    "determining whether certain penalties apply and will not be used by the IRS for examination purposes."
)
COAL_AS_OF = "2024-06-07"
EC_AS_OF = "2024-05-31"
FFE_AS_OF = "2024-06-07"
NETL_RPS = 1.0
#: Server-side generalisation of tract and county outlines (~5 m): below TIGER's own accuracy, and it cuts
#: the vertices a corridor test walks.
GENERALISE_DEG = 0.00005

COAL_SCHEMA = [
    ("tract_geoid", pa.string()),
    ("tract_name", pa.string()),
    ("county", pa.string()),
    ("state", pa.string()),
    ("category", pa.string()),
    ("mine_closure", pa.bool_()),
    ("generator_closure", pa.bool_()),
    ("adjacent_to_closure", pa.bool_()),
    ("dataset_as_of", pa.string()),
    ("record_updated", pa.string()),
]
FFE_SCHEMA = [
    ("county_geoid", pa.string()),
    ("county", pa.string()),
    ("state", pa.string()),
    ("statistical_area", pa.string()),
    ("area_type", pa.string()),
    ("meets_ffe_threshold", pa.bool_()),
    ("is_energy_community", pa.bool_()),
    ("status", pa.string()),
    ("dataset_as_of", pa.string()),
    ("record_updated", pa.string()),
]


def _yes(value: Any) -> bool:
    s = str(value or "").strip().lower()
    return s in ("yes", "y", "true", "1") or s.startswith("yes")


def _text(value: Any) -> str | None:
    s = str(value).strip() if value is not None else ""
    return s or None


def _date(ms: Any) -> str | None:
    try:
        return dt.datetime.fromtimestamp(int(ms) / 1000, dt.UTC).date().isoformat()
    except (TypeError, ValueError, OverflowError):
        return None


def normalise_coal(features: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows, geoms = [], []
    for f in features:
        p, g = f.get("properties") or {}, f.get("geometry")
        if not g:
            continue
        mine, gen, adj = (
            _yes(p.get("mine_closure")),
            _yes(p.get("generator_closure")),
            _yes(p.get("adjacent_to_closure")),
        )
        parts = [s for s, flag in (("coal mine closure", mine), ("coal generator retirement", gen)) if flag]
        category = " and ".join(parts) if parts else ("adjoins a closure tract" if adj else None)
        rows.append(
            {
                "tract_geoid": _text(p.get("geoid_tract_2020")),
                "tract_name": _text(p.get("tract_name")),
                "county": _text(p.get("county_name")),
                "state": _text(p.get("state_name")),
                "category": category or _text(p.get("label")),
                "mine_closure": mine,
                "generator_closure": gen,
                "adjacent_to_closure": adj,
                "dataset_as_of": COAL_AS_OF,
                "record_updated": _date(p.get("date_last_update")),
            }
        )
        geoms.append(g)
    return rows, geoms


def normalise_ffe(
    ec_features: list[dict[str, Any]], ffe_only_features: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows, geoms = [], []
    for features, is_ec, as_of, service in (
        (ec_features, True, EC_AS_OF, "ec"),
        (ffe_only_features, False, FFE_AS_OF, "ffe_only"),
    ):
        for f in features:
            p, g = f.get("properties") or {}, f.get("geometry")
            if not g:
                continue
            rows.append(
                {
                    # Each row names the service it came from (the layer joins two); the GeoParquet
                    # writer puts this in the row's provenance `source_url`.
                    "source_url": f"{SERVICES[service]}/0",
                    "county_geoid": _text(p.get("geoid_cty_2020")),
                    "county": _text(p.get("county_name_2020")),
                    "state": _text(p.get("state_name")),
                    "statistical_area": _text(p.get("msa_area_name")),
                    "area_type": _text(p.get("msa_qual")),
                    "meets_ffe_threshold": True,
                    "is_energy_community": is_ec,
                    "status": _text(p.get("label_ec") if is_ec else p.get("label_fee"))
                    or _text(p.get("ec_qual_status")),
                    "dataset_as_of": as_of,
                    "record_updated": _date(p.get("date_last_update")),
                }
            )
            geoms.append(g)
    return rows, geoms


def _where(region: str) -> str:
    states = REGION_STATES.get(region)
    if not states:
        return "1=1"
    quoted = ", ".join("'" + s.replace("'", "''") + "'" for s in states)
    return f"state_name IN ({quoted})"


def build_layers(
    coal: list[dict[str, Any]],
    ec: list[dict[str, Any]],
    ffe_only: list[dict[str, Any]],
    *,
    out_dir: pathlib.Path,
    retrieved_at: str,
    coverage: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    outputs = []
    # The statistical-area layer joins two services: its provenance names both (`source_urls`), its
    # `source_url` is the energy-community service (the one that decides `qualifies`), and every row
    # carries the URL of the service it came from.
    for layer_id, urls, as_of, (rows, geoms), schema in (
        (
            "energy_community_coal_closure",
            [f"{SERVICES['coal']}/0"],
            COAL_AS_OF,
            normalise_coal(coal),
            COAL_SCHEMA,
        ),
        (
            "energy_community_ffe",
            [f"{SERVICES['ec']}/0", f"{SERVICES['ffe_only']}/0"],
            EC_AS_OF,
            normalise_ffe(ec, ffe_only),
            FFE_SCHEMA,
        ),
    ):
        vintage = (
            f"IRA energy community data layers, 2024 (as of {as_of}); "
            "NETL: taxpayers may not rely on these data"
        )
        prov = provenance_for(
            SOURCE_ID, source_url=urls[0], retrieved_at=retrieved_at, licence=LICENCE_NAME, vintage=vintage
        )
        out = out_dir / f"{layer_id}.parquet"
        extra: dict[str, Any] = {"taxpayer_line": TAXPAYER_LINE}
        if coverage:
            extra["coverage"] = coverage
        summary = write_geoparquet(out, rows, geoms, schema=schema, provenance=prov, extra_metadata=extra)
        output = {
            "layer_id": layer_id,
            "file": out.name,
            "kind": "polygons",
            "provenance": {
                **prov.record(),
                "source_urls": urls,
                "taxpayer_line": TAXPAYER_LINE,
                "dataset_as_of": as_of,
            },
            "licence_name": LICENCE_NAME,
            "summary": summary,
        }
        if coverage:
            output["coverage"] = public(coverage)
        outputs.append(output)
    return outputs


def build(
    *,
    region: str = "texas",
    out_dir: pathlib.Path | None = None,
    snapshot_dir: pathlib.Path | None = None,
    session: PoliteSession | None = None,
    store: Store | None = None,
) -> list[dict[str, Any]]:
    """`snapshot_dir` holds recorded `coal.geojson`, `ec.geojson` and `ffe_only.geojson`."""
    out_dir = output_dir(out_dir)
    if snapshot_dir is not None:
        pages = {k: json.loads((snapshot_dir / f"{k}.geojson").read_text())["features"] for k in SERVICES}
        when = snapshot_time(snapshot_dir / "coal.geojson")
        return build_layers(
            pages["coal"],
            pages["ec"],
            pages["ffe_only"],
            out_dir=out_dir,
            retrieved_at=when,
            coverage=for_state_filtered(region),
        )
    session = session or PoliteSession(rate_limits={"arcgis.netl.doe.gov": NETL_RPS})
    log = FetchLog(SOURCE_ID)
    pages = {}
    for key, service in SERVICES.items():
        verify_terms(session, f"{service}/info/iteminfo?f=json", TERMS_PHRASES, log=log, honour_robots=False)
        pages[key] = query_geojson(
            session, f"{service}/0", log=log, where=_where(region), max_allowable_offset=GENERALISE_DEG
        )
    outputs = build_layers(
        pages["coal"],
        pages["ec"],
        pages["ffe_only"],
        out_dir=out_dir,
        retrieved_at=now_iso(),
        coverage=for_state_filtered(region),
    )
    log.write(store, region=region, features={k: len(v) for k, v in pages.items()})
    return outputs


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--region", choices=sorted(REGIONS), default="texas")
    parser.add_argument("--snapshot-dir", type=pathlib.Path)
    parser.add_argument("--out-dir", type=pathlib.Path)
    args = parser.parse_args(argv)
    t0 = time.monotonic()
    outputs = build(region=args.region, out_dir=args.out_dir, snapshot_dir=args.snapshot_dir, store=Store())
    print(json.dumps({"layers": outputs, "elapsed_s": round(time.monotonic() - t0, 2)}))  # noqa: T201


if __name__ == "__main__":
    main()
