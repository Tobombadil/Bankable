"""In-memory query layer over the static files `build_data.py` writes (docs/23 §7 filter grammar,
API-3, API-2 cursor pagination -- approximated here without a database, `docs/20` §15 web-app row:
"a JS framework only if product-designer's flows need it", the store itself needs nothing more
than the files on disk for this prototype).

Loaded once at process start (`app.py` startup) and filtered/sorted per request. No source in this
sprint has enough rows to need anything heavier, and every filter name here matches the URL
parameter name verbatim (`docs/04` D-17) so a copied URL reproduces the view.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

Kind = Literal["proposal", "opportunity"]

PROPOSAL_SORT_ALLOWLIST = {"capacity_mw", "proposed_online_date", "name", "queue_date"}
OPPORTUNITY_SORT_ALLOWLIST = {"due_at", "open_at", "budget_amount", "title"}


@dataclass
class Store:
    proposals: list[dict[str, Any]]
    state_aggregates: list[dict[str, Any]]
    opportunities: list[dict[str, Any]]
    stats: dict[str, Any]
    proposals_by_slug: dict[str, dict[str, Any]]
    opportunities_by_slug: dict[str, dict[str, Any]]

    @classmethod
    def load(cls, data_dir: Path) -> Store:
        geojson = json.loads((data_dir / "proposals.geojson").read_text(encoding="utf-8"))
        proposals: list[dict[str, Any]] = []
        state_aggregates: list[dict[str, Any]] = []
        for feature in geojson["features"]:
            props = dict(feature["properties"])
            lon, lat = feature["geometry"]["coordinates"]
            props["lon"], props["lat"] = lon, lat
            if props["feature_type"] == "state_aggregate":
                state_aggregates.append(props)
            else:
                proposals.append(props)
        opportunities = json.loads((data_dir / "opportunities.json").read_text(encoding="utf-8"))
        stats = json.loads((data_dir / "stats.json").read_text(encoding="utf-8"))
        return cls(
            proposals=proposals,
            state_aggregates=state_aggregates,
            opportunities=opportunities,
            stats=stats,
            proposals_by_slug={p["slug"]: p for p in proposals},
            opportunities_by_slug={o["slug"]: o for o in opportunities},
        )


def _matches_csv(value: Any, csv_param: str | None) -> bool:
    if not csv_param:
        return True
    wanted = {v.strip() for v in csv_param.split(",") if v.strip()}
    return value in wanted


def _matches_text(record: dict[str, Any], fields: list[str], q: str | None) -> bool:
    if not q:
        return True
    needle = q.strip().lower()
    if not needle:
        return True
    return any(needle in str(record.get(f, "")).lower() for f in fields)


def filter_proposals(
    records: list[dict[str, Any]],
    *,
    technology: str | None = None,
    lifecycle_state: str | None = None,
    kind: str | None = None,
    jurisdiction: str | None = None,
    capacity_gte: float | None = None,
    capacity_lte: float | None = None,
    q: str | None = None,
) -> list[dict[str, Any]]:
    out = []
    for r in records:
        if not _matches_csv(r.get("technology"), technology):
            continue
        if not _matches_csv(r.get("lifecycle_state"), lifecycle_state):
            continue
        if not _matches_csv(r.get("kind"), kind):
            continue
        if not _matches_csv(r.get("state"), jurisdiction):
            continue
        capacity = r.get("capacity_mw")
        if capacity_gte is not None and (capacity is None or capacity < capacity_gte):
            continue
        if capacity_lte is not None and (capacity is None or capacity > capacity_lte):
            continue
        if not _matches_text(r, ["name", "sponsor", "queue_id", "county"], q):
            continue
        out.append(r)
    return out


def filter_opportunities(
    records: list[dict[str, Any]],
    *,
    kind: str | None = None,
    status: str | None = None,
    jurisdiction: str | None = None,
    technology: str | None = None,
    q: str | None = None,
) -> list[dict[str, Any]]:
    out = []
    for r in records:
        if not _matches_csv(r.get("kind"), kind):
            continue
        if not _matches_csv(r.get("status"), status):
            continue
        if not _matches_csv(r.get("jurisdiction"), jurisdiction):
            continue
        if technology:
            wanted = {v.strip() for v in technology.split(",") if v.strip()}
            if not wanted.intersection(r.get("technologies") or []):
                continue
        if not _matches_text(r, ["title", "issuer"], q):
            continue
        out.append(r)
    return out


def sort_records(
    records: list[dict[str, Any]], sort_expr: str | None, allowlist: set[str], default: str
) -> list[dict[str, Any]]:
    """`sort=-field` per `docs/23` §7. A record missing the sort field sorts last regardless of
    direction (no null-ordering rule is stated in `docs/31` §5.5; interleaving unknowns into a
    descending sort would be worse).
    """
    expr = sort_expr or default
    field = expr[1:] if expr.startswith("-") else expr
    if field not in allowlist:
        expr = default
        field = default[1:] if default.startswith("-") else default
    reverse = expr.startswith("-")
    with_value = [r for r in records if r.get(field) is not None]
    without_value = [r for r in records if r.get(field) is None]
    with_value.sort(key=lambda r: r[field], reverse=reverse)
    return with_value + without_value


def paginate(
    records: list[dict[str, Any]], *, offset: int, limit: int
) -> tuple[list[dict[str, Any]], bool, int]:
    total = len(records)
    page = records[offset : offset + limit]
    has_more = offset + limit < total
    return page, has_more, total
