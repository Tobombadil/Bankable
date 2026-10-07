"""Shape API envelope dicts (`services/api/serialize.py`) into what the Jinja templates render,
and the handful of presentation rules that belong to the frontend rather than the API:
lifecycle-family colour grouping (docs/31 §1.2), the default "active states only" map/list filter
(product defect A, `docs/00-PLAN.md` task), and the collapsed provenance panel (product defect C).

Nothing here decides *visibility* -- that is entirely the API's `services/api/visibility.py`
predicate. This module only relabels and regroups fields the API already returned.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, Literal

from services.api.common import WEB_HOST
from web import labels
from web.api_client import ApiClient, ApiError
from web.retirement import asset_status_label

Family = Literal["neutral", "progress", "committed", "success", "danger"]

# docs/21 §7.1-§7.2; docs/31 §1.2 five-family grouping. Duplicated from the (now-unused for the
# live path) web/build_data.py table rather than imported from it: build_data.py is retained only
# for the vendored basemap per this task's brief, not as a dependency of the live app.
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

# Product defect A (docs/00-PLAN.md): "default view shows active lifecycle states only
# (announced through under_construction)". `built` and `unknown` are deliberately outside this
# set -- the task's own wording bounds the default to the six states between `announced` and
# `under_construction` inclusive; withdrawn/cancelled sit behind the explicit toggle below.
ACTIVE_PROPOSAL_STATES: tuple[str, ...] = (
    "announced",
    "filed",
    "studied",
    "permitted",
    "contracted",
    "under_construction",
)
WITHDRAWN_PROPOSAL_STATES: tuple[str, ...] = ("withdrawn", "cancelled")
# ADR 0008 sitemap task: every lifecycle state a proposal can carry, `built`/`unknown` included --
# unlike ACTIVE_PROPOSAL_STATES (product defect A's default view), the sitemap must list every
# published proposal's page regardless of which lifecycle-state bucket it is in.
ALL_PROPOSAL_LIFECYCLE_STATES: tuple[str, ...] = (
    ACTIVE_PROPOSAL_STATES
    + WITHDRAWN_PROPOSAL_STATES
    + (
        "built",
        "unknown",
    )
)

# Reader-facing words for every vocabulary token live in `web/labels.py` (audit 2026-09-30, F1/D-7);
# the names below are re-exported because pages, tests and `map.js` (via `map_labels_json`) have
# always imported them from here.
TECHNOLOGY_LABELS = labels.TECHNOLOGY_LABELS
TECHNOLOGY_LOAD_LABEL = labels.TECHNOLOGY_LOAD_LABEL
PROPOSAL_KIND_LABELS = labels.PROPOSAL_KIND_LABELS
OPPORTUNITY_KIND_LABELS = labels.OPPORTUNITY_KIND_LABELS
LIFECYCLE_LABELS = labels.LIFECYCLE_LABELS
technology_label = labels.technology_label
proposal_kind_label = labels.proposal_kind_label
opportunity_kind_label = labels.opportunity_kind_label
lifecycle_label = labels.lifecycle_label


def map_labels_json() -> str:
    """The label maps `map.js` reads from the map page's `#map-labels` JSON script tag, so the
    browser names a token exactly as the server does without a hand-kept copy of the words.
    `</` is escaped for the same reason as `web/page.py::_mini_map`: it is the only sequence that
    can end a `<script type="application/json">` early."""
    return json.dumps(labels.map_labels(), separators=(",", ":")).replace("</", "<\\/")


#: Every source whose rows are proposals, in the order a page names them, with the short name a
#: sentence uses and the group it belongs to. `data/sources.yaml`'s `name` is a register title
#: (50 to 90 characters) and its `operator` a full agency name, so neither fits a sentence; the
#: ids are the manifest's. `web/app.py::_PROPOSAL_SOURCE_IDS` is these keys, and
#: `web/test_data_centre_presentation.py` pins them to the list the dev loader loads
#: (`web/build_data.py::PROPOSAL_SOURCE_IDS`), so adding a source without naming it fails a test
#: instead of leaving the copy stale.
PROPOSAL_SOURCE_LABELS: dict[str, tuple[str, Literal["queue", "data_centre"]]] = {
    "us.iso.ercot.gen_queue": ("ERCOT", "queue"),
    "us.iso.caiso.gen_queue": ("CAISO", "queue"),
    "us.iso.nyiso.gen_queue": ("NYISO", "queue"),
    "us.eia.860m": ("EIA-860M", "queue"),
    "gb.neso.tec_register": ("NESO", "queue"),
    "us.va.deq.data_center_air_sites": ("Virginia DEQ", "data_centre"),
    "us.epa.echo.icis_air": ("EPA ICIS-Air", "data_centre"),
}


#: The short name a list's Source column prints for each opportunity source (the ids are
#: `web/build_data.py::OPPORTUNITY_SOURCE_IDS`, pinned by `web/test_source_labels.py`). Each is the
#: short form `data/sources.yaml`'s own `name` for that source already contains, not a new name.
OPPORTUNITY_SOURCE_LABELS: dict[str, str] = {
    "us.grants_gov.search2": "Grants.gov",
    "eu.ted.api": "TED",
    "gb.find_a_tender": "Find a Tender",
    "mdb.worldbank.procnotices": "World Bank",
}

#: The same for the asset registries the dev loader loads (`web/dev_up.py`); `us.eia.860m` is
#: named once, in `PROPOSAL_SOURCE_LABELS`, because it feeds both. Same rule: every word is in the
#: source's `data/sources.yaml` `name`, and `web/test_source_labels.py` checks that it is.
ASSET_SOURCE_LABELS: dict[str, str] = {
    "us.eia.atlas.gas_pipelines": "EIA Energy Atlas",
    "us.eia.atlas.gas_processing_plants": "EIA Energy Atlas",
    "us.eia.atlas.gas_storage": "EIA Energy Atlas",
    "us.eia.atlas.lng_terminals": "EIA Energy Atlas",
    "us.eia.atlas.ethanol_plants": "EIA Energy Atlas",
    "us.eia.ethanol_capacity": "EIA ethanol capacity",
    "us.epa.lmop": "EPA LMOP",
    "us.epa.agstar": "EPA AgSTAR",
    "us.lbnl.ferc_hifld_transmission_lines": "LBNL",
}


#: The registers an ownership claim on a company page cites (`organization.parent_source_id`), as
#: the sentence "Ownership stated as of ... from {label}" reads. The curated table is the
#: companies' own published statements (data/sources.yaml `curated.organization_parents`), and is
#: named as that rather than by its table name (audit 2026-09-30, F1).
OWNERSHIP_SOURCE_LABELS: dict[str, str] = {
    "curated.organization_parents": "the companies' own published statements",
    "global.gleif.lei": "GLEIF",
    "us.eia.860": "EIA-860",
    "us.epa.ghgrp": "EPA GHGRP",
}


def source_label(source_id: str | None) -> str | None:
    """The short name a list's Source column prints for `source_id`, or `None` for a source no
    map names -- the template then prints the id itself rather than a guessed name."""
    if not source_id:
        return None
    if source_id in PROPOSAL_SOURCE_LABELS:
        return PROPOSAL_SOURCE_LABELS[source_id][0]
    return (
        OPPORTUNITY_SOURCE_LABELS.get(source_id)
        or ASSET_SOURCE_LABELS.get(source_id)
        or OWNERSHIP_SOURCE_LABELS.get(source_id)
    )


def _join_names(names: list[str]) -> str:
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


def proposal_sources_phrase() -> str:
    """ "Interconnection queue and generator proposals from ERCOT, …, and data-centre sites from
    Virginia DEQ and EPA ICIS-Air": the one sentence fragment the map header and the list's meta
    description share, built from `PROPOSAL_SOURCE_LABELS` so it cannot fall behind it again."""
    queues = [label for label, group in PROPOSAL_SOURCE_LABELS.values() if group == "queue"]
    data_centres = [label for label, group in PROPOSAL_SOURCE_LABELS.values() if group == "data_centre"]
    parts = []
    if queues:
        parts.append(f"Interconnection queue and generator proposals from {_join_names(queues)}")
    if data_centres:
        parts.append(f"data-centre sites from {_join_names(data_centres)}")
    return ", and ".join(parts)


#: Why a data-centre connector kept a row (`select_basis` on the row, carried by the loader to
#: `proposal.identifiers.select_basis` as `{source_id: basis}`). Worded from docs/25 §3.3 and
#: §3.7. `naics_518210` carries its measured weakness in the sentence itself: in the hand check
#: 39 of 49 rows on that basis were data centres and the residual class is offices with a server
#: room. A basis token not listed here renders nothing rather than an unqualified claim.
SELECT_BASIS_TEXT: dict[str, str] = {
    "name": "the facility name says data centre",
    "naics_518210": (
        "its industry code is NAICS 518210 (data processing and hosting). Some sites with this code "
        "are offices with a server room: 39 of 49 checked by hand were data centres"
    ),
    "naics_541513_operator": (
        "its industry code is computer services (NAICS 541513 or 541519) and its name includes a "
        "known data-centre operator"
    ),
    "deq_flag": "Virginia DEQ flags it as a data centre",
    "principal_product": "Virginia DEQ's record gives its principal product as a data centre",
}


def select_basis_line(basis: str | None) -> str | None:
    """One sentence for a source row, or `None` when there is no basis or no wording for it."""
    text = SELECT_BASIS_TEXT.get(basis or "")
    return f"Selected as a data centre because {text}." if text else None


def attach_select_basis(record: Mapping[str, Any], rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Give each provenance row the sentence saying why that source counts this record as a
    data centre. Only for `kind == load`, and only on rows the page already shows, so a gated
    source's basis can never surface: the rows are the visible provenance, and a basis whose
    source has no row here is dropped."""
    if record.get("kind") != "load":
        return rows
    basis_by_source = record.get("select_basis") or {}
    for row in rows:
        line = select_basis_line(basis_by_source.get(row.get("source_id")))
        if line:
            row["select_basis_line"] = line
    return rows


ALL_OPPORTUNITY_STATUSES: tuple[str, ...] = (
    "unknown",
    "announced",
    "open",
    "frozen",
    "reinstated",
    "closed",
    "cancelled",
    "awarded",
)

WORLD_BBOX = "-179,-85,179,85"


def resolve_proposal_lifecycle_param(qp: Mapping[str, str]) -> tuple[str, bool, bool]:
    """Return `(lifecycle_state_csv, explicit, include_withdrawn)` for a request.

    If the caller passed `lifecycle_state=` explicitly (e.g. from a saved link, or picking one
    state in `/proposals`), that set wins verbatim -- same "explicit overrides the default"
    pattern the opportunities list already used for `status=all` before this task. Otherwise the
    default is `ACTIVE_PROPOSAL_STATES`, extended with `WITHDRAWN_PROPOSAL_STATES` when
    `include_withdrawn` is truthy.
    """
    explicit_value = qp.get("lifecycle_state")
    if explicit_value:
        return explicit_value, True, _is_truthy(qp.get("include_withdrawn"))
    include_withdrawn = _is_truthy(qp.get("include_withdrawn"))
    states = list(ACTIVE_PROPOSAL_STATES)
    if include_withdrawn:
        states += list(WITHDRAWN_PROPOSAL_STATES)
    return ",".join(states), False, include_withdrawn


def _is_truthy(value: str | None) -> bool:
    return (value or "").lower() in ("1", "true", "yes", "on")


def opportunity_status_param(qp: Mapping[str, str]) -> str:
    """`status=all` means "every status" -- the API itself only knows a literal CSV list (it
    defaults to `open` if the parameter is absent), so the web layer expands `all` to the full
    vocabulary rather than passing the literal word through.
    """
    value = qp.get("status", "open")
    if value == "all":
        return ",".join(ALL_OPPORTUNITY_STATUSES)
    return value


def lifecycle_family(state: str | None) -> Family:
    return LIFECYCLE_FAMILY.get(state or "", "neutral")


#: How loud each slip bucket renders. `under_1y` is deliberately plain text, not a coloured
#: badge: 67 of the 129 slipped rows on the 2026-09-21 load are in it, and a connection date that
#: has moved by a few months is ordinary for a consented project -- colouring all of them is the
#: cry-wolf failure. `1_to_3y` and `over_3y` (48 and 14 rows) get the warning treatment, because
#: at that distance the stated date has stopped describing the project.
SLIP_TONE: dict[str, str] = {"under_1y": "quiet", "1_to_3y": "warn", "over_3y": "warn"}


def slip_display(slip: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """The API's `schedule_slip` object -> what a template prints, or `None` for no signal.

    A record with no `proposed_online_date` never reaches here with a value: the API returns
    `None` for it, and `None` in means `None` out, so "nothing promised" can never render as
    "overdue" (`web/test_slippage_view.py`).
    """
    if not slip:
        return None
    days = int(slip["days_late"])
    if days <= 365:
        months = max(1, round(days / 30.44))
        amount = f"{months} month{'s' if months != 1 else ''}"
    else:
        amount = f"{days / 365.25:.1f} years"
    bucket = str(slip["bucket"])
    return {
        "bucket": bucket,
        "tone": SLIP_TONE.get(bucket, "quiet"),
        "days_late": days,
        "target_date": slip["target_date"],
        "amount": amount,
        "label": f"overdue by {amount}",
        "detail": (
            f"Target commercial-operation date {slip['target_date']} passed {amount} ago and the "
            f"record is still in an active lifecycle state."
        ),
    }


#: How a stored ISO token reads on a page (lane E15). The API and the store keep the spec's `Iso`
#: token (`ISONE`, what `?iso=`, alerts and social copy match on, docs/00-PLAN.md 2026-09-27 lane E14);
#: readers know the operator as "ISO-NE". Display only: links and query strings keep the token.
ISO_DISPLAY_LABELS: dict[str, str] = {"ISONE": "ISO-NE"}


def iso_label(token: str | None) -> str | None:
    """The reader-facing label for an ISO token; any token without a mapping reads as itself."""
    if not token:
        return token
    return ISO_DISPLAY_LABELS.get(token, token)


def flatten_proposal(entity: Mapping[str, Any]) -> dict[str, Any]:
    """API `serialize_proposal()` shape -> the flat dict the detail/list templates read."""
    location = entity.get("location") or {}
    primary_source = _primary_provenance(entity.get("provenance") or [])
    identifiers = entity.get("identifiers") or {}
    queue_ids = identifiers.get("queue_ids") or []
    return {
        "public_id": entity["public_id"],
        "slug": entity["slug"],
        "name": entity["name_canonical"],
        "kind": entity.get("kind"),
        "kind_label": proposal_kind_label(entity.get("kind")),
        "technology": entity.get("technology"),
        "technology_label": technology_label(entity.get("technology")),
        "technology_raw": entity.get("technology_raw"),
        "select_basis": _mapping_or_empty(identifiers.get("select_basis")),
        "capacity_mw": entity.get("capacity_mw"),
        "storage_mwh": entity.get("storage_mwh"),
        "jurisdiction": entity.get("jurisdiction"),
        "state": (location.get("state_code") or entity.get("jurisdiction") or "").rsplit("-", 1)[-1] or None,
        "county": location.get("county_name"),
        "location_precision": location.get("precision"),
        "restricted_precision": location.get("precision_reason") == "licence",
        "iso": entity.get("iso"),
        "iso_label": iso_label(entity.get("iso")),
        "sponsor": (entity.get("sponsor") or {}).get("name_canonical"),
        "lifecycle_state": entity.get("lifecycle_state"),
        "lifecycle_family": lifecycle_family(entity.get("lifecycle_state")),
        "status_raw": entity.get("status_raw"),
        "queue_id": queue_ids[0]["id"] if queue_ids else None,
        "queue_ids": [str(q.get("id")) for q in queue_ids if isinstance(q, Mapping) and q.get("id")],
        "eia_plant_id": identifiers.get("eia_plant_id"),
        "eia_generator_id": identifiers.get("eia_generator_id"),
        "eia_ids": _eia_ids(identifiers),
        "queue_date": None,
        "proposed_online_date": entity.get("proposed_online_date"),
        "slip": slip_display(entity.get("schedule_slip")),
        "source_count": entity.get("source_count"),
        "source_id": primary_source.get("source_id"),
        "source_label": source_label(primary_source.get("source_id")),
        "source_name": primary_source.get("source_name"),
        "source_url": primary_source.get("source_url"),
        "retrieved_at": primary_source.get("retrieved_at"),
        "reuse_class": primary_source.get("reuse_class"),
        "attribution_text": primary_source.get("attribution_text"),
        "allows_raw": primary_source.get("source_record_id") is not None,
        "provenance": entity.get("provenance") or [],
        # The source that supplied each served value (docs/22 §23.4), so the status and date lines
        # name the register they came from rather than the first provenance row (audit F2).
        "status_source": field_source(entity, "lifecycle_state"),
        "cod_source": field_source(entity, "proposed_online_date"),
        "capacity_note": capacity_note(entity),
        "members": member_rows(entity.get("members") or []),
        "merge_history": entity.get("merge_history") or [],
    }


def _eia_ids(identifiers: Mapping[str, Any]) -> str | None:
    """ "69662 / IPD2S", or every generator of a merged record ("69661 / IPD1B, 69661 / IPD1S, …")."""
    generators = identifiers.get("eia_generators")
    if isinstance(generators, list) and generators:
        return ", ".join(
            f"{g.get('plant_id')} / {g.get('generator_id')}" for g in generators if isinstance(g, Mapping)
        )
    plant = identifiers.get("eia_plant_id")
    if not plant:
        return None
    generator = identifiers.get("eia_generator_id")
    return f"{plant} / {generator}" if generator else str(plant)


def field_source(entity: Mapping[str, Any], field: str) -> dict[str, Any]:
    """The provenance row of the source that supplied `field`'s served value (the API's
    `field_sources`; the latest-retrieved row when one source has several links). Without a
    supplier named, only a record with a single provenance row can be credited (audit F2: the first
    row of a merged record is not the source of its status); otherwise nothing is named."""
    rows: list[dict[str, Any]] = [dict(r) for r in entity.get("provenance") or [] if isinstance(r, Mapping)]
    supplied = (entity.get("field_sources") or {}).get(field) or {}
    if supplied.get("rule") == "override":
        return {}  # an admin decision: no register stated this value
    named = supplied.get("source_ids")
    if not named:
        return dict(rows[0]) if len(rows) == 1 else {}
    supplying = [r for r in rows if r.get("source_id") in set(named)]
    if not supplying:
        return {}
    latest: dict[str, Any] = max(supplying, key=lambda r: str(r.get("retrieved_at") or ""))
    row = dict(latest)
    row["source_label"] = source_label(row.get("source_id"))
    return row


def capacity_note(entity: Mapping[str, Any]) -> str | None:
    """One sentence on what the capacity figure is, for a record that several sources feed: the
    request MW stands beside the plant inventory's own total, which can differ (a hybrid's solar and
    storage generators are listed separately; the request is the grid-connection limit)."""
    members = [m for m in entity.get("members") or [] if isinstance(m, Mapping)]
    if len(members) < 2:
        return None
    rule = ((entity.get("field_sources") or {}).get("capacity_mw") or {}).get("rule") or ""
    inventory = [m for m in members if m.get("role") == "inventory" and m.get("capacity_mw")]
    requests = [m for m in members if m.get("role") == "request" and m.get("capacity_mw")]
    if rule.startswith("interconnection_request_plus_inventory"):
        return (
            "Capacity is the interconnection request plus the generators listed below whose "
            "technology no request covers."
        )
    if rule.startswith("interconnection_request") and inventory:
        names = sorted({str(m.get("source_name")) for m in requests}) or ["the interconnection queue"]
        total = sum(float(m["capacity_mw"]) for m in inventory)
        inventory_names = sorted({str(m.get("source_name")) for m in inventory})
        return (
            f"Capacity is the interconnection request in {_join_names(names)}. "
            f"The {len(inventory)} generator{'s' if len(inventory) != 1 else ''} listed in "
            f"{_join_names(inventory_names)} below total {total:,.1f} MW nameplate."
        )
    if rule.startswith("plant_inventory") and len(inventory) > 1:
        return f"Capacity is the sum of the {len(inventory)} generators listed below."
    if rule.endswith("_sum") and len(requests) > 1:
        return f"Capacity is the sum of the {len(requests)} interconnection requests listed below."
    return None


_ROLE_LABELS = {"request": "Request", "inventory": "Generator"}


def member_rows(members: list[Any]) -> list[dict[str, Any]]:
    """`members` from the API, with reader labels."""
    out: list[dict[str, Any]] = []
    for m in members:
        if not isinstance(m, Mapping):
            continue
        out.append(
            {
                **m,
                "source_label": source_label(m.get("source_id")) or m.get("source_name"),
                "technology_label": technology_label(m.get("technology")),
                "lifecycle_label": lifecycle_label(m.get("lifecycle_state")),
                "role_label": _ROLE_LABELS.get(str(m.get("role")), "Record"),
            }
        )
    return out


#: How a change event reads on the record's history (D-5).
_EVENT_FIELD_LABELS: dict[str, str] = {
    "lifecycle_state": "status",
    "capacity_mw": "capacity (MW)",
    "proposed_online_date": "proposed commercial-operation date",
    "storage_mwh": "storage (MWh)",
}


def _event_line(event: Mapping[str, Any]) -> str | None:
    kind = event.get("event_type")
    source = (event.get("provenance") or {}).get("source_name") or "its source"
    before, after = event.get("before") or {}, event.get("after") or {}
    if kind == "created":
        return f"First published from {source}."
    if kind == "removed":
        return f"Left the {source} register."
    keys = list(event.get("changed_keys") or [])
    key = keys[0] if keys else next(iter(after or before), None)
    if key is None:
        return None
    label = _EVENT_FIELD_LABELS.get(str(key), labels.humanise(str(key)))

    def shown(value: Any) -> str:
        if value is None:
            return "none"
        if key == "lifecycle_state":
            return str(lifecycle_label(value) or value).lower()
        return str(value)

    return f"{source}: {label} {shown(before.get(key))} → {shown(after.get(key))}."


#: Detail-only parts of `GET /v1/proposals/{id}` the page reads (docs/22 §23.4); the page resolves
#: the record through the slug-filtered list, which does not carry them.
_COMPOSITION_KEYS: tuple[str, ...] = ("field_sources", "members", "merge_history")


def with_composition(api: ApiClient, entity: Mapping[str, Any]) -> dict[str, Any]:
    """`entity` with the detail route's `field_sources`, `members` and `merge_history`. A failed
    detail call leaves them out: the page then names a status source only for a single-source
    record (`field_source`) and shows no member table, never a wrong credit."""
    out = dict(entity)
    public_id = entity.get("public_id")
    if not public_id:
        return out
    try:
        detail = api.get(f"/v1/proposals/{public_id}")["data"]
    except (ApiError, KeyError, TypeError):
        return out
    if isinstance(detail, Mapping):
        out.update({key: detail[key] for key in _COMPOSITION_KEYS if key in detail})
    return out


def proposal_history(api: ApiClient, record: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The record's history, newest first (designer audit D-5): its public change events
    (`/v1/proposals/{id}/events`, first page) and the merges that brought its readable source rows
    (`merge_history`). Each item is `{date, text}`; a failed events call leaves the merges."""
    items: list[dict[str, Any]] = []
    for merge in record.get("merge_history") or []:
        parts = []
        for m in merge.get("members") or []:
            name = m.get("name_canonical") or "a record"
            ref = f" ({m['source_record_id']})" if m.get("source_record_id") else ""
            label = source_label(m.get("source_id")) or m.get("source_name")
            parts.append(f"{name}{ref} from {label}")
        if parts:
            items.append(
                {"date": merge.get("merged_at"), "text": f"Merged into this record: {_join_names(parts)}."}
            )
    try:
        events = api.get(f"/v1/proposals/{record['public_id']}/events", params={"limit": "20"})["data"]
    except (ApiError, KeyError, TypeError):
        events = []
    for event in events:
        if not isinstance(event, Mapping):
            continue
        text = _event_line(event)
        if text:
            items.append({"date": event.get("observed_at"), "text": text})
    return sorted(items, key=lambda i: str(i.get("date") or ""), reverse=True)


def _mapping_or_empty(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _known_budget(amount: Any) -> Any:
    """A budget of 0 or less is a source placeholder (TED's 0 / -1), shown as a dash like no budget.
    The loader stores those as null (lane I3, 2026-09-29); this covers one that reaches the page
    another way. The currency is left as stated."""
    is_number = isinstance(amount, int | float) and not isinstance(amount, bool)
    return amount if is_number and amount > 0 else None


def flatten_opportunity(entity: Mapping[str, Any]) -> dict[str, Any]:
    primary_source = _primary_provenance(entity.get("provenance") or [])
    return {
        "public_id": entity["public_id"],
        "slug": entity["slug"],
        "title": entity["title"],
        "kind": entity.get("kind"),
        "kind_label": opportunity_kind_label(entity.get("kind")),
        "issuer": (entity.get("issuer") or {}).get("name_canonical"),
        "jurisdiction": entity.get("jurisdiction"),
        "technologies": entity.get("technologies") or [],
        "capacity_sought_mw": entity.get("capacity_sought_mw"),
        "budget_amount": _known_budget(entity.get("budget_amount")),
        "budget_currency": entity.get("budget_currency"),
        "open_at": entity.get("open_at"),
        "due_at": entity.get("due_at"),
        "status": entity.get("status"),
        "lifecycle_family": lifecycle_family(entity.get("status")),
        "status_raw": entity.get("status_raw"),
        "summary": entity.get("summary"),
        "source_id": primary_source.get("source_id"),
        "source_label": source_label(primary_source.get("source_id")),
        "source_name": primary_source.get("source_name"),
        "source_url": primary_source.get("source_url"),
        "retrieved_at": primary_source.get("retrieved_at"),
        "reuse_class": primary_source.get("reuse_class"),
        "attribution_text": primary_source.get("attribution_text"),
        "allows_raw": primary_source.get("source_record_id") is not None,
        "provenance": entity.get("provenance") or [],
    }


def flatten_asset(entity: Mapping[str, Any]) -> dict[str, Any]:
    """ADR 0008 `asset` shape (`docs/21` §3.22) -> the flat dict `asset_detail.html` reads.
    `owners[]` (docs/23 §3.1 `/v1/assets/{public_id}`: "organisation public id, name, role,
    share_pct, as_of, source") is kept as its own list of small dicts rather than merged into the
    top level, since a page renders it as its own table.
    """
    primary_source = _primary_provenance(entity.get("provenance") or [])
    owners = [
        {
            "public_id": o.get("public_id") or o.get("organization_public_id"),
            "name": o.get("name") or o.get("name_canonical"),
            "role": o.get("role"),
            "share_pct": o.get("share_pct"),
            "as_of": o.get("as_of"),
            "source_name": o.get("source_name") or o.get("source"),
        }
        for o in (entity.get("owners") or [])
    ]
    return {
        "public_id": entity["public_id"],
        "slug": entity["slug"],
        "name": entity.get("name"),
        "asset_type": entity.get("asset_type"),
        "status": entity.get("status"),
        "status_label": asset_status_label(entity.get("status")),
        "retirement_year": entity.get("retirement_year"),
        "operator_name": entity.get("operator_name"),
        "technology": entity.get("technology"),
        "technology_raw": entity.get("technology_raw"),
        "technologies": entity.get("technologies") or {},
        "capacity_mw": entity.get("capacity_mw"),
        "capacity_value": entity.get("capacity_value"),
        "capacity_unit": entity.get("capacity_unit"),
        "commissioned_year": entity.get("commissioned_year"),
        "unit_count": entity.get("unit_count"),
        "state": entity.get("state_code"),
        "county": entity.get("county_name"),
        "county_fips": entity.get("county_fips"),
        "country": entity.get("country"),
        "attributes": entity.get("attributes") or {},
        "owners": owners,
        "source_id": primary_source.get("source_id"),
        "source_label": source_label(primary_source.get("source_id")),
        "source_name": primary_source.get("source_name"),
        "source_url": primary_source.get("source_url"),
        "retrieved_at": primary_source.get("retrieved_at"),
        "reuse_class": primary_source.get("reuse_class"),
        "attribution_text": primary_source.get("attribution_text"),
        "allows_raw": primary_source.get("source_record_id") is not None,
        "provenance": entity.get("provenance") or [],
    }


def flatten_org_asset_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """`GET /v1/organizations/{public_id}/assets` row (docs/23 §3.1: "through `asset_owner`, with
    role and share") -> the flat dict `organization_detail.html`'s assets table reads. Tolerant of
    either an embedded `asset` sub-object or a row whose asset fields are already flattened onto
    it, since the exact embed shape is not pinned down in `docs/23`'s terse table row.
    """
    asset_raw = row.get("asset")
    asset = asset_raw if isinstance(asset_raw, Mapping) else row
    return {
        "public_id": asset.get("public_id"),
        "slug": asset.get("slug"),
        "name": asset.get("name"),
        "asset_type": asset.get("asset_type"),
        "capacity_mw": asset.get("capacity_mw"),
        "role": row.get("role"),
        "share_pct": row.get("share_pct"),
    }


def flatten_organization(entity: Mapping[str, Any]) -> dict[str, Any]:
    """`organization` shape (`docs/21` §3.5, plus ADR 0008's `parent_org_id`) -> the flat dict
    `organization_detail.html` and the `/search` organisations section read."""
    parent_raw = entity.get("parent")
    parent: Mapping[str, Any] = parent_raw if isinstance(parent_raw, Mapping) else {}
    return {
        "public_id": entity["public_id"],
        "slug": entity.get("slug"),
        "name": entity.get("name_canonical") or entity.get("name"),
        "type": entity.get("type"),
        "country": entity.get("country"),
        "jurisdiction": entity.get("jurisdiction"),
        "website": entity.get("website"),
        "is_curated_issuer": entity.get("is_curated_issuer", False),
        # A natural person named in a register (migration 0033; docs/13 §5.5): the page is `noindex`.
        "personal_data": bool(entity.get("personal_data", False)),
        "parent_public_id": parent.get("public_id"),
        "parent_name": parent.get("name_canonical") or parent.get("name"),
        "provenance": entity.get("provenance") or [],
    }


def _primary_provenance(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return rows[0] if rows else {}


def provenance_panel_rows(api: ApiClient, provenance: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Product defect C: one row per source, collapsed behind the source name -- classification
    (`reuse_class`) and retrieval date visible, the licence's quote text behind an expandable
    `<details>` (docs/31 §5.2 anatomy). Gated sources are already omitted server-side
    (`provenance_row`/the visibility predicate), so every row here is one the public tier may show.

    `Licence.quote_text` (`services/README.md`'s "Sprint 2 fixes" #5, exposed on the embedded
    licence shape returned by `/v1/sources/{id}`) carries `data/sources.yaml`'s free-text `license`
    clause verbatim -- rendered as-is here rather than composed from the licence's boolean
    permission flags, which is what this module did before that field existed (web/README.md
    "Missing from the API" item 3, now resolved).
    """
    out: list[dict[str, Any]] = []
    licence_cache: dict[str, dict[str, Any]] = {}
    for row in provenance:
        source_id = row.get("source_id")
        licence = licence_cache.get(source_id) if source_id else None
        if licence is None and source_id:
            try:
                licence = api.get(f"/v1/sources/{source_id}")["data"]["licence"]
            except ApiError:
                licence = None
            licence_cache[source_id] = licence or {}
        out.append(
            {
                "source_id": source_id,
                "source_name": row.get("source_name"),
                "source_url": row.get("source_url"),
                "retrieved_at": row.get("retrieved_at"),
                "reuse_class": row.get("reuse_class"),
                "attribution_text": row.get("attribution_text"),
                "licence_url": (licence or {}).get("url"),
                "allows_raw": row.get("source_record_id") is not None,
                "active": row.get("active", True),
                "licence_quote": _licence_quote_text(licence),
            }
        )
    return out


def _licence_quote_text(licence: dict[str, Any] | None) -> str:
    quote = licence.get("quote_text") if licence else None
    return quote if quote else "No licence quote recorded for this source."


def web_relative_url(url: str | None) -> str | None:
    """The API's own `url` fields are absolute, built from `services/api/common.WEB_HOST` --
    which is still the literal `infraque.com` placeholder token (docs/00-PLAN.md: the product name
    is not chosen yet), so they are not navigable links on whatever host this site is actually
    served from. Anywhere the map (`web/static/js/map.js`, via the `/api/proposals/geo` proxy)
    needs to link to a full record, this strips that placeholder host down to a same-origin path.
    """
    if url and url.startswith(WEB_HOST):
        return url[len(WEB_HOST) :] or "/"
    return url


def relativize_geo_feature_urls(feature_collection: dict[str, Any]) -> dict[str, Any]:
    for feature in feature_collection.get("features", []):
        props = feature.get("properties") or {}
        if "url" in props:
            props["url"] = web_relative_url(props["url"])
    return feature_collection


def restricted_precision_note(location: Mapping[str, Any] | None) -> str | None:
    if not location:
        return None
    if location.get("precision_reason") == "licence":
        return "Location shown at county level (source licence)."
    return None


def absence_note(facts: Mapping[str, Any], filters: Mapping[str, Any]) -> dict[str, Any] | None:
    """Why an empty result may be empty, for the one place a reader actually asks.

    An empty list is the moment an absence is most likely to be read as a fact about the world —
    "there are no data-centre proposals in Texas" rather than "we have no source that publishes
    them". This is deliberately *not* rendered next to a result that has rows: a caveat printed
    beside 5,000 matching records is a disclaimer, gets skimmed, and costs the page more than it
    pays. `None` when there is nothing specific to say.

    Both branches are measured. The technology line fires only for a token the normaliser can
    emit and no source has ever produced; the register line lists the withheld supply registers
    from `data/sources.yaml` by name. Neither is a sentence anyone typed about a particular
    source, so neither can survive the fact that justified it.
    """
    if not facts:
        return None
    lines: list[str] = []
    absent = set(facts.get("technologies", {}).get("absent") or [])
    requested = {token.strip() for token in str(filters.get("technology") or "").split(",") if token.strip()}
    for token in sorted(requested & absent):
        lines.append(
            f"No source in this register publishes {token.replace('_', ' ')} proposals at all, "
            "so this filter cannot match — the category is missing, not filtered out."
        )
    withheld = [s for s in (facts.get("sources", {}).get("withheld") or []) if s.get("supply")]
    if withheld:
        names = ", ".join(str(s.get("operator") or s.get("name")) for s in withheld[:4])
        more = len(withheld) - 4
        lines.append(
            f"{len(withheld)} interconnection registers are withheld pending licence clearance "
            f"({names}{f' and {more} more' if more > 0 else ''}). No row from them appears in any "
            "result, on any tier."
        )
    if not lines:
        return None
    return {"lines": lines, "href": "/methodology#absences"}


def asset_count_note(facts: Mapping[str, Any], selected_types: set[str]) -> dict[str, Any] | None:
    """The one case where a caveat belongs beside a populated list: when the number of rows is
    itself the wrong number.

    `absence_note` deliberately says nothing next to rows, because a caveat beside five thousand
    correct records is a disclaimer. An asset type drawn from two sources with no resolution
    layer is a different case (docs/24 §6.1): a plant present in both sources is two rows, and
    the row count over-states the asset count by a measured amount. There the line is not a
    disclaimer, it is the corrected figure, and it belongs where the wrong one is read.

    It fires only when *both* halves are present: the derived fact (more than one source, not
    resolved -- `coverage.assets.by_type`) and a note that measured the over-statement
    (`figures.distinct_estimate`). Two sources with disjoint populations (RNG) have the fact and
    no estimate, and a count over their rows is right, so nothing is printed. When the link
    table lands the fact flips, the note retires, and this returns `None` with no edit.
    `None` for anything but a single selected type, because the sentence is about one type's
    rows and the unfiltered index mixes seven."""
    if len(selected_types) != 1 or not facts:
        return None
    asset_type = next(iter(selected_types))
    entry = ((facts.get("assets") or {}).get("by_type") or {}).get(asset_type) or {}
    if entry.get("source_count", 0) < 2 or entry.get("resolved"):
        return None
    note = next(
        (
            n
            for n in facts.get("notes") or []
            if (n.get("applies_to") or {}).get("unresolved_asset_type") == asset_type
        ),
        None,
    )
    if note is None:
        return None
    figures = note.get("figures") or {}
    estimate = figures.get("distinct_estimate")
    if not isinstance(estimate, int | float):
        return None
    return {
        "rows": int(entry.get("rows") or 0),
        "located": int(entry.get("located") or 0),
        "source_count": int(entry.get("source_count") or 0),
        "estimate": int(estimate),
        "measured": str(figures.get("measured") or note.get("written") or ""),
        "href": f"/methodology#note-{note.get('id')}",
    }


def coverage_facts(request: Any, api: Any) -> dict[str, Any]:
    """The measured coverage numbers, for the in-place notes that appear where an absence bites.

    Cached on `app.state` for the life of the process, like `footer_build` and the lag figures
    and for the same reason: these numbers move when a load runs, not between two requests, and
    `/v1/coverage` runs a dozen aggregates that no list page should pay for per row. The
    `/methodology` page deliberately does *not* use this cache — it is the canonical statement and
    reads the API fresh, so the one surface whose whole job is being accurate never serves a
    number a restart-old cache is holding.

    Any failure yields an empty mapping and every caller renders nothing. A note about coverage
    is worth having; it is not worth a 500.
    """
    cached: dict[str, Any] | None = getattr(request.app.state, "coverage_facts", None)
    if cached is None:
        try:
            cached = dict(api.get("/v1/coverage")["data"])
        except Exception:
            cached = {}
        request.app.state.coverage_facts = cached
    return cached


def footer_build(request: Any, api: Any) -> dict[str, Any]:
    """The commit the API is running and the vintage of the rows it serves, for the footer
    (`services/api/build_info.py`). Read from `/v1/health` once per app process and cached on
    `app.state` beside the lag figures, for the same reason: it is server configuration, not
    per-request data. Any failure renders nothing rather than an error, because a diagnostic line
    must never take a page down.

    It lives here rather than in `web/app.py` because `web/auth.py` and `web/legal.py` keep their
    own `Jinja2Templates`, `get_api` and footer globals on purpose (see `web/auth.py`'s module
    docstring) and importing from `web.app` would close the circle those copies exist to avoid.
    Each module passes its own `get_api(request)`; the cache is shared, since all three routers
    are mounted on one FastAPI application.

    The web process reports the API's commit, not its own: they are deployed together, and the
    question a reader is asking is which build served this page.
    """
    cached: dict[str, Any] | None = getattr(request.app.state, "build_info", None)
    if cached is None:
        try:
            health = api.get("/v1/health")
            build = health.get("build") or {}
            vintage = health.get("source_vintage") or {}
            cached = {
                "commit": build.get("commit"),
                "dirty": build.get("dirty"),
                "source_data_as_of": health.get("source_data_as_of"),
                # The oldest release the sources themselves state, which is the honest bound on
                # the data's age and is not the fetch date beside it. The footer printed only
                # "sources last fetched <date>" until 2026-09-21, and readers took that for the
                # data's age; on that load the oldest loaded release was nine years older.
                "oldest_vintage": vintage.get("oldest"),
                "oldest_vintage_label": vintage.get("oldest_label"),
                "sources_stating_none": vintage.get("sources_stating_none"),
            }
        except Exception:
            cached = {
                "commit": None,
                "dirty": None,
                "source_data_as_of": None,
                "oldest_vintage": None,
                "oldest_vintage_label": None,
                "sources_stating_none": None,
            }
        request.app.state.build_info = cached
    return cached
