"""The prospecting layers' catalogue: what each layer is, its unit, and the one sentence of what it is not.

One entry per layer the sampler can serve (docs/25 "Prospecting layers"; docs/00-PLAN.md decision row of
2026-10-22 "Prospecting layers ... screening grade only"). The catalogue is static: it holds what does not
change from one build to the next (title, unit, kind, the screening sentence, which attributes a polygon
layer exposes). What does change -- the file, its sha256, the source's `retrieved_at` and the dataset's own
vintage -- comes from the build's manifest (`layers.json`, written by `pipeline/context/prospect/build.py`),
and both are merged into every served value by `services/context_sample/sampler.py`.

`pipeline/context/prospect/*` imports this module so a layer's title and screening sentence are written in
one place; nothing here imports the pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

LayerKind = Literal["raster", "polygons"]

#: Every value served carries this flag (owner, 2026-10-22: "screening grade only").
SCREENING_GRADE = "screening"


@dataclass(frozen=True)
class LayerInfo:
    layer_id: str
    title: str
    kind: LayerKind
    source_id: str
    #: Raster: the unit of the value. Polygons: what the share is a share of.
    unit: str
    #: One plain sentence: what this value is not (docs/00-PLAN.md 2026-10-22).
    not_statement: str
    #: Raster: decimals the value is rounded to when served (the source's own precision).
    decimals: int = 2
    #: Polygons: the attribute columns a matching feature exposes, in order.
    fields: tuple[str, ...] = ()
    #: Polygons: the attribute that names a feature in a summary ("Permian Basin").
    name_field: str | None = None
    #: Polygons: a column whose truthy value marks a hard constraint (PAD-US GAP status 1-2).
    hard_constraint_field: str | None = None
    #: Polygons: a column whose truthy value marks a feature that qualifies (an FFE area that is an energy
    #: community this year); without one, every feature of the layer qualifies.
    qualifying_field: str | None = None
    #: Part of the combined IRA energy-community answer (`energy_community` in a sample).
    energy_community: bool = False
    #: Grouping for a reader ("resource", "constraint", "incentive", "subsurface", "hazard").
    group: str = "resource"
    notes: tuple[str, ...] = field(default_factory=tuple)


SOLAR_NOT = (
    "A satellite-modelled 1998-2016 average over a 4 km cell, not a bankable resource assessment, "
    "which needs on-site measurement."
)
WIND_NOT = (
    "A modelled long-term mean wind speed on a 250 m grid, not a bankable resource assessment, which "
    "needs on-site measurement (a met mast or lidar)."
)
ENERGY_COMMUNITY_NOT = (
    "A screening map of IRA energy-community areas, not tax advice: DOE NETL says taxpayers may not rely "
    "on it to substantiate a tax return position."
)

CATALOG: dict[str, LayerInfo] = {
    info.layer_id: info
    for info in (
        LayerInfo(
            layer_id="solar_ghi",
            title="Solar irradiance, global horizontal (annual average)",
            kind="raster",
            source_id="us.nrel.nsrdb.psm3_annual",
            unit="kWh/m²/day",
            not_statement=SOLAR_NOT,
            decimals=2,
            group="resource",
        ),
        LayerInfo(
            layer_id="solar_dni",
            title="Solar irradiance, direct normal (annual average)",
            kind="raster",
            source_id="us.nrel.nsrdb.psm3_annual",
            unit="kWh/m²/day",
            not_statement=SOLAR_NOT,
            decimals=2,
            group="resource",
        ),
        LayerInfo(
            layer_id="wind_speed_100m",
            title="Mean wind speed at 100 m",
            kind="raster",
            source_id="global.dtu.global_wind_atlas",
            unit="m/s",
            not_statement=WIND_NOT,
            decimals=2,
            group="resource",
        ),
        LayerInfo(
            layer_id="oil_gas_basins",
            title="Sedimentary basins with shale plays (EIA)",
            kind="polygons",
            source_id="us.eia.atlas.sedimentary_basins",
            unit="share of the area inside a basin",
            not_statement=(
                "A basin outline, not evidence of producible oil or gas at this site or of who holds the "
                "mineral rights."
            ),
            fields=("name",),
            name_field="name",
            group="subsurface",
        ),
        LayerInfo(
            layer_id="oil_gas_plays",
            title="Low-permeability (tight oil and shale gas) plays (EIA)",
            kind="polygons",
            source_id="us.eia.atlas.shale_plays",
            unit="share of the area inside a play",
            not_statement=(
                "A general play area, not evidence of producible reserves at this site, of mineral "
                "ownership or of drilling activity."
            ),
            fields=("name", "basin", "lithology", "age"),
            name_field="name",
            group="subsurface",
        ),
        LayerInfo(
            layer_id="oil_gas_reserves_state",
            title="Proved reserves of the state, year-end 2024 (EIA)",
            kind="polygons",
            source_id="us.eia.proved_reserves",
            unit="share of the area in the state",
            not_statement=(
                "A statewide total of proved reserves at year-end 2024, not a measure of what lies under "
                "this site."
            ),
            fields=(
                "state",
                "crude_oil_lease_condensate_mmbbl",
                "natural_gas_wet_bcf",
                "report_year",
            ),
            name_field="state",
            group="subsurface",
        ),
        LayerInfo(
            layer_id="energy_community_coal_closure",
            title="IRA energy community: coal closure census tracts and adjoining tracts",
            kind="polygons",
            source_id="us.doe.netl.energy_communities",
            unit="share of the area in a qualifying tract",
            not_statement=ENERGY_COMMUNITY_NOT,
            fields=("tract_geoid", "tract_name", "county", "state", "category", "dataset_as_of"),
            name_field="tract_name",
            group="incentive",
            energy_community=True,
        ),
        LayerInfo(
            layer_id="energy_community_ffe",
            title="IRA energy community: statistical areas meeting the fossil-fuel employment test",
            kind="polygons",
            source_id="us.doe.netl.energy_communities",
            unit="share of the area in a qualifying statistical area",
            not_statement=ENERGY_COMMUNITY_NOT,
            fields=(
                "statistical_area",
                "county",
                "state",
                "meets_ffe_threshold",
                "is_energy_community",
                "status",
                "dataset_as_of",
            ),
            name_field="statistical_area",
            group="incentive",
            qualifying_field="is_energy_community",
            energy_community=True,
            notes=(
                "Every area here meets the fossil-fuel employment threshold; only those also meeting the "
                "unemployment test (is_energy_community) qualify that year.",
            ),
        ),
        LayerInfo(
            layer_id="protected_lands",
            title="Protected lands (USGS PAD-US): fee and designation",
            kind="polygons",
            source_id="us.usgs.padus",
            unit="share of the area on protected land",
            not_statement=(
                "A national inventory compiled from many managers, not a title or survey record; land "
                "outside these polygons can still carry easements or restrictions."
            ),
            fields=(
                "unit_name",
                "category",
                "designation",
                "manager",
                "manager_type",
                "gap_status",
                "public_access",
                "hard_constraint",
            ),
            name_field="unit_name",
            hard_constraint_field="hard_constraint",
            group="constraint",
            notes=("GAP status 1 or 2 (managed for biodiversity) is the hard constraint.",),
        ),
        LayerInfo(
            layer_id="water_stress",
            title="Baseline water stress (WRI Aqueduct 4.0)",
            kind="polygons",
            source_id="global.wri.aqueduct40",
            unit="share of the area in a scored sub-basin",
            not_statement=(
                "A sub-basin index of baseline water stress, not a water-rights or supply assessment for "
                "this site."
            ),
            fields=("score", "category", "label", "raw_ratio", "basin_id"),
            name_field="label",
            group="constraint",
        ),
        LayerInfo(
            layer_id="seismic_pga_2pct50",
            title="Seismic hazard: peak ground acceleration, 2% in 50 years (site class BC)",
            kind="raster",
            source_id="us.usgs.nshm2023",
            unit="g",
            not_statement=(
                "A probabilistic hazard value for a reference site class (Vs30 760 m/s), not a "
                "site-specific geotechnical or design value."
            ),
            decimals=3,
            group="hazard",
        ),
    )
}

#: The layers the site finder samples per corridor segment, in display order (docs/25 "Prospecting
#: layers", contract for the site finder).
SITE_FINDER_LAYERS: tuple[str, ...] = (
    "protected_lands",
    "energy_community_coal_closure",
    "energy_community_ffe",
    "solar_ghi",
    "wind_speed_100m",
    "water_stress",
    "seismic_pga_2pct50",
    "oil_gas_plays",
    "oil_gas_basins",
)


#: The layers whose qualifying features together make the IRA energy-community answer.
ENERGY_COMMUNITY_LAYERS: tuple[str, ...] = tuple(
    lid for lid, info in CATALOG.items() if info.energy_community
)


def layer(layer_id: str) -> LayerInfo:
    """The catalogue entry, or KeyError naming the known ids."""
    try:
        return CATALOG[layer_id]
    except KeyError:
        raise KeyError(f"unknown layer {layer_id!r}; known: {sorted(CATALOG)}") from None
