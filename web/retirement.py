"""The "Retirement" section of a power-plant page, and the words the map uses for asset statuses
(lane R1, owner request 2026-10-06: retired and retiring plants as a powered-land signal).

Presentation only. Everything here reads what `GET /v1/assets/{id}` and
`GET /v1/assets/{id}/nearby-grid` already returned (`attributes.retirement`, `attributes.grid`,
`retirement_year`, `retirement_changes`) and turns tokens into words: no token such as `retiring`,
`retirement_date_changed` or `2028-12` reaches the page as itself.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from web import formatting, labels

#: `asset.status` -> the word a page prints; the table lives in `web/labels.py` with every other
#: vocabulary's words and is re-exported here, where R1 introduced it.
ASSET_STATUS_LABELS = labels.ASSET_STATUS_LABELS

#: `event.event_type` on an asset (services/db/models.py ASSET_EVENT_TYPES) -> a heading.
RETIREMENT_EVENT_LABELS: dict[str, str] = {
    "retirement_planned": "Retirement planned",
    "retirement_date_changed": "Planned retirement date moved",
    "retirement_cancelled": "Planned retirement withdrawn",
    "retired": "Retired",
    "returned_to_service": "Returned to service",
}

#: Generator state in `attributes.retirement.units[]` -> the word a units table prints.
UNIT_STATE_LABELS: dict[str, str] = {"retired": "Retired", "retiring": "Scheduled to retire"}

#: Why the plant has the status it has (`pipeline/context/retirements.py` rule ids).
STATUS_RULE_TEXT: dict[str, str] = {
    "all_units_retired": "Every generator EIA lists at this plant has retired.",
    "majority_mw_retiring": (
        "At least half of the plant's in-service nameplate capacity is scheduled to retire, on dates its"
        " owner has reported to EIA."
    ),
    "some_units_retiring": (
        "Some generators are scheduled to retire; most of the plant's capacity is not, so it is shown as"
        " operating."
    ),
    "some_units_retired": "Some generators have retired; the rest of the plant is still in service.",
}

#: The plain-language note on reusing a retired plant's grid connection. Worded as a possibility:
#: whether any right survives, who holds it and on what terms is a question for the owner and the
#: transmission provider, and nothing on the page establishes it (docs/27 section R1.3).
INTERCONNECTION_REUSE_NOTE = (
    "A plant that retires usually leaves its substation, transmission connection and interconnection"
    " agreement behind, and those may be reusable by a new project at the same site. FERC Order No. "
    "845 lets an interconnection customer use surplus interconnection service at an existing site, "
    "and some transmission providers' tariffs let a replacement resource take over a retiring unit's"
    " interconnection service, usually within a set time after the retirement. Whether any such "
    "right still exists here, who holds it and on what terms depends on the owner, the transmission "
    "provider and timing; this page does not establish any of that."
)

_MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)


def asset_status_label(status: str | None) -> str | None:
    if not status:
        return None
    return ASSET_STATUS_LABELS.get(status, status.replace("_", " ").capitalize())


def month_label(value: Any) -> str | None:
    """`"2028-12"` -> `"December 2028"`; `"2032"` -> `"2032"` (EIA stated the year alone);
    anything else, or nothing, -> None."""
    if value is None:
        return None
    text = str(value).strip()
    if len(text) == 4 and text.isdigit():
        return text
    if len(text) >= 7 and text[:4].isdigit() and text[4] == "-" and text[5:7].isdigit():
        month = int(text[5:7])
        if 1 <= month <= 12:
            return f"{_MONTHS[month - 1]} {text[:4]}"
    return None


def _mw(value: Any) -> str | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number <= 0:
        return None
    return f"{formatting.mw(number)} MW"  # docs/31 §4 (UX-8)


def _kv(values: Any) -> str | None:
    if not isinstance(values, list) or not values:
        return None
    return ", ".join(f"{float(v):,.0f} kV" for v in values if isinstance(v, int | float))


def _change_rows(changes: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for change in changes:
        units = []
        for unit in change.get("units") or []:
            moved = [month_label(unit.get("date_before")), month_label(unit.get("date_after"))]
            when = " to ".join(m for m in moved if m) if any(moved) else None
            units.append(
                {
                    "generator_id": unit.get("generator_id"),
                    "technology": unit.get("technology"),
                    "capacity": _mw(unit.get("capacity_mw")),
                    "when": when,
                }
            )
        observed = str(change.get("observed_at") or "")[:10] or None
        rows.append(
            {
                "label": RETIREMENT_EVENT_LABELS.get(str(change.get("event_type")), "Retirement change"),
                "observed": observed,
                "capacity": _mw(change.get("capacity_mw")),
                "units": units,
            }
        )
    return rows


def retirement_view(
    entity: Mapping[str, Any], nearby_grid: Mapping[str, Any] | None = None
) -> dict[str, Any] | None:
    """The Retirement section's rows, or None when the plant has nothing to say: no retired or
    scheduled unit, no retirement news. Every row is conditional on its value (the "None" gate)."""
    if entity.get("asset_type") != "power_plant":
        return None
    attributes = entity.get("attributes")
    bag: Mapping[str, Any] = attributes if isinstance(attributes, Mapping) else {}
    block = bag.get("retirement")
    retirement: Mapping[str, Any] = block if isinstance(block, Mapping) else {}
    changes = [c for c in (entity.get("retirement_changes") or []) if isinstance(c, Mapping)]
    if not retirement and not changes:
        return None
    status = entity.get("status")
    facts: list[tuple[str, str]] = []

    def add(label: str, value: str | None) -> None:
        if value:
            facts.append((label, value))

    add("Status", asset_status_label(status))
    if status == "retired":
        add("Retired", month_label(retirement.get("last_retired")))
        if retirement.get("first_retired") and retirement.get("first_retired") != retirement.get(
            "last_retired"
        ):
            add("First unit retired", month_label(retirement.get("first_retired")))
    add("Capacity retired", _mw(retirement.get("retired_mw")))
    if retirement.get("retired_units"):
        add("Generators retired", str(retirement["retired_units"]))
    add("Next scheduled retirement", month_label(retirement.get("next_planned")))
    if retirement.get("last_planned") and retirement.get("last_planned") != retirement.get("next_planned"):
        add("Last scheduled retirement", month_label(retirement.get("last_planned")))
    add("Capacity scheduled to retire", _mw(retirement.get("retiring_mw")))
    if retirement.get("retiring_units"):
        add("Generators scheduled to retire", str(retirement["retiring_units"]))
    share = retirement.get("retiring_share")
    if isinstance(share, int | float) and retirement.get("retiring_mw"):
        add("Share of in-service capacity scheduled", f"{share * 100:.0f}%")
    add("Capacity still in service", _mw(retirement.get("in_service_mw")) if status != "retired" else None)

    grid_block = bag.get("grid")
    grid: Mapping[str, Any] = grid_block if isinstance(grid_block, Mapping) else {}
    grid_rows: list[tuple[str, str]] = []
    for label, value in (
        ("Grid voltage at the plant", _kv(grid.get("grid_voltage_kv"))),
        ("Transmission or distribution system owner", grid.get("transmission_owner")),
        ("Balancing authority", grid.get("balancing_authority_name") or bag.get("balancing_authority_code")),
        ("NERC region", grid.get("nerc_region")),
    ):
        if value:
            grid_rows.append((label, str(value)))
    grid_year = grid.get("report_year")

    by_technology = [
        {
            "technology": row.get("technology") or "Not stated",
            "retired": _mw(row.get("retired_mw")),
            "retiring": _mw(row.get("retiring_mw")),
        }
        for row in (retirement.get("by_technology") or [])
        if isinstance(row, Mapping)
    ]
    units = [
        {
            "generator_id": u.get("generator_id"),
            "technology": u.get("technology"),
            "capacity": _mw(u.get("capacity_mw")),
            "state": UNIT_STATE_LABELS.get(str(u.get("state")), str(u.get("state") or "")),
            "when": month_label(u.get("date")),
        }
        for u in (retirement.get("units") or [])[:30]
        if isinstance(u, Mapping)
    ]
    grid_near = nearby_grid or {}
    return {
        "status_label": asset_status_label(status),
        "rule_text": STATUS_RULE_TEXT.get(str(retirement.get("status_rule") or "")),
        "facts": facts,
        "as_of": month_label(retirement.get("as_of")),
        "by_technology": by_technology,
        "units": units,
        "units_more": max(0, len(retirement.get("units") or []) - len(units)),
        "grid_rows": grid_rows,
        "grid_year": grid_year,
        "lines": list(grid_near.get("transmission_lines") or []),
        "points": list(grid_near.get("interconnection_points") or []),
        "radius_km": grid_near.get("radius_km"),
        "changes": _change_rows(changes),
        "reuse_note": INTERCONNECTION_REUSE_NOTE,
    }
