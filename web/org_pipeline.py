"""A company's pipeline at a glance, for the top of `/organizations/{ident}` (owner, 2026-10-10:
the beta's first job is "check a developer's or owner's pipeline with a source for every claim").

Built from one read, `GET /v1/organizations/{id}/pipeline` at the page's ownership scope (lane P,
2026-10-10). It replaced paging `/v1/organizations/{id}/proposals` (up to 5 x 200 rows, "partial"
beyond that): the API now counts and sums over every record, so the summary is never partial and
nothing here estimates or extrapolates.

Every count links to `/proposals` filtered to the same rows. The sponsor part of each link is the
API's own `list_query` (`sponsor_id`, plus `sponsor_scope` when the page covers subsidiaries), and
each bucket adds the filter the API documents for it, so each count is the total its link opens:

- the status table's active groups (announced, in process, contracted) count only records some
  register still lists (`listed=true`); the records every register has dropped, whose last stated
  status is active, are their own row, "No longer listed" (`listed=false`); built, withdrawn and
  unknown count every record in those states;
- the technology and grid-operator tables cover the active pipeline (active states and listed),
  which is the list's default view plus `listed=true`; a grid operator is one name however the
  register spelt it (`iso=ERCOT` also opens EIA-860M's `ERCO` rows);
- each register links with `source_id` over every status.

Capacity is the API's sum over generation and storage; a data centre's MW is demand, a line's is
transfer capability, and a CO2 well has none, so those records are counted, not summed, and the
page says how many.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

from web.api_client import ApiClient, ApiError
from web.labels import proposal_kind_label, technology_label
from web.viewmodels import (
    ACTIVE_PROPOSAL_STATES,
    ALL_PROPOSAL_LIFECYCLE_STATES,
    PROPOSAL_STATUS_GROUPS,
    iso_label,
    source_label,
)

#: Rows the page reads for its Proposals section (it prints `ORG_PROPOSALS_LISTED` of them); the
#: pipeline summary does not read rows at all.
PAGE_SIZE = 100
#: The status chip family of each status group (docs/31 §1.2), for the icon beside its name.
STATUS_GROUP_FAMILY = {
    "announced": "neutral",
    "in_process": "progress",
    "contracted": "committed",
    "built": "success",
    "withdrawn": "danger",
    "unknown": "neutral",
}
#: The status groups whose records are active pipeline: counted only while a register lists them.
ACTIVE_GROUPS = frozenset(
    key for key, _label, states in PROPOSAL_STATUS_GROUPS if set(states) <= set(ACTIVE_PROPOSAL_STATES)
)
NOT_LISTED_LABEL = "No longer listed"


@dataclass
class ProposalPages:
    """The first page of proposal rows read for one company page's list section."""

    rows: list[dict[str, Any]] = field(default_factory=list)
    complete: bool = True
    failed: bool = False


def fetch_proposals(api: ApiClient, public_id: str, params: Mapping[str, Any]) -> ProposalPages:
    """The most recently changed `PAGE_SIZE` proposals the organisation (at the page's scope)
    sponsors, for the list section. `complete` is whether that is all of them. A failed call leaves
    the section empty, as before."""
    pages = ProposalPages()
    try:
        envelope = api.get(f"/v1/organizations/{public_id}/proposals", params={"limit": PAGE_SIZE, **params})
    except ApiError:
        pages.failed = True
        pages.complete = False
        return pages
    pages.rows = [dict(row) for row in envelope.get("data") or [] if isinstance(row, Mapping)]
    page = envelope.get("page")
    pages.complete = not (isinstance(page, Mapping) and page.get("has_more"))
    return pages


def fetch_pipeline(api: ApiClient, public_id: str, params: Mapping[str, Any]) -> dict[str, Any] | None:
    """`GET /v1/organizations/{id}/pipeline` at the page's scope (`params` carries `scope` when it
    is not `self`), or `None` when it cannot be read: the section is then left out."""
    try:
        envelope = api.get(f"/v1/organizations/{public_id}/pipeline", params=dict(params))
    except ApiError:
        return None
    data = envelope.get("data")
    return dict(data) if isinstance(data, Mapping) else None


def _list_href(list_query: Sequence[tuple[str, str]], *pairs: tuple[str, str]) -> str:
    return "/proposals?" + urlencode([*list_query, *pairs])


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _tally(raw: Any) -> dict[str, Any]:
    """An API `PipelineTally` as the three numbers the template reads."""
    raw = raw if isinstance(raw, Mapping) else {}
    mw = raw.get("capacity_mw")
    return {
        "count": _int(raw.get("records")),
        "mw": round(float(mw), 1) if isinstance(mw, int | float) and not isinstance(mw, bool) else None,
        "capacity_records": _int(raw.get("capacity_records")),
        "not_summed_records": _int(raw.get("not_summed_records")),
    }


def _merge(tallies: Iterable[dict[str, Any]]) -> dict[str, Any]:
    out = {"count": 0, "mw": None, "capacity_records": 0, "not_summed_records": 0}
    for t in tallies:
        out["count"] += t["count"]
        out["capacity_records"] += t["capacity_records"]
        out["not_summed_records"] += t["not_summed_records"]
        if t["mw"] is not None:
            out["mw"] = round((out["mw"] or 0.0) + t["mw"], 1)
    return out


def _row(label: str, tally: Mapping[str, Any], href: str | None, **extra: Any) -> dict[str, Any]:
    return {"label": label, "count": tally["count"], "mw": tally["mw"], "href": href, **extra}


def pipeline_summary(aggregate: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """The pipeline block's context from the API's aggregate, or `None` when there is none or the
    company sponsors no proposal at this scope."""
    if not aggregate:
        return None
    totals = _tally(aggregate.get("totals"))
    if not totals["count"]:
        return None
    raw_query = aggregate.get("list_query")
    list_query = [
        (key, str(raw_query[key]))
        for key in ("sponsor_id", "sponsor_scope")
        if isinstance(raw_query, Mapping) and raw_query.get(key)
    ]
    if not list_query:
        return None
    scope = aggregate.get("scope")
    group = _int(scope.get("organizations") if isinstance(scope, Mapping) else 1) > 1
    active_states = [str(s) for s in aggregate.get("active_states") or ACTIVE_PROPOSAL_STATES]
    listed = ("listed", "true")
    all_states = ("lifecycle_state", ",".join(ALL_PROPOSAL_LIFECYCLE_STATES))

    by_state: dict[str, dict[bool, dict[str, Any]]] = {}
    for entry in aggregate.get("by_lifecycle_state") or []:
        if isinstance(entry, Mapping) and entry.get("lifecycle_state"):
            by_state[str(entry["lifecycle_state"])] = {
                True: _tally(entry.get("listed")),
                False: _tally(entry.get("not_listed")),
            }
    not_listed = _tally(aggregate.get("not_listed"))
    status_rows: list[dict[str, Any]] = []
    for key, label, states in PROPOSAL_STATUS_GROUPS:
        active_group = key in ACTIVE_GROUPS
        parts = [
            by_state[s][flag]
            for s in states
            if s in by_state
            for flag in ((True,) if active_group else (True, False))
        ]
        tally = _merge(parts)
        if tally["count"]:
            pairs = [("lifecycle_state", ",".join(states)), *([listed] if active_group else [])]
            status_rows.append(
                _row(label, tally, _list_href(list_query, *pairs), family=STATUS_GROUP_FAMILY[key])
            )
        if key == "contracted" and not_listed["count"]:
            # After the last active group: the records whose last stated status is active and that
            # no register lists any more.
            href = _list_href(list_query, ("lifecycle_state", ",".join(active_states)), ("listed", "false"))
            status_rows.append(_row(NOT_LISTED_LABEL, not_listed, href, family="neutral", not_listed=True))

    def ranked(entries: Any, key: str, label_of: Any, empty: str) -> list[dict[str, Any]]:
        rows = []
        for entry in entries or []:
            if not isinstance(entry, Mapping):
                continue
            token = entry.get(key)
            href = _list_href(list_query, (key, str(token)), listed) if token else None
            rows.append(_row(str(label_of(token) or token) if token else empty, _tally(entry), href))
        return rows

    sources = [
        {
            "name": source_label(str(s["source_id"])) or s.get("name") or s["source_id"],
            "count": _int(s.get("records")),
            "retrieved_at": s.get("retrieved_at_max"),
            "href": _list_href(list_query, ("source_id", str(s["source_id"])), all_states),
        }
        for s in aggregate.get("sources") or []
        if isinstance(s, Mapping) and s.get("source_id")
    ]
    not_summed = [
        {
            "kind": str(k["kind"]),
            "label": str(proposal_kind_label(k["kind"]) or k["kind"]),
            "count": _int(k["records"]),
        }
        for k in aggregate.get("not_summed_by_kind") or []
        if isinstance(k, Mapping) and k.get("kind")
    ]
    return {
        "group": group,
        "total": _row("All statuses", totals, _list_href(list_query, all_states)),
        "active": _row("Active pipeline", _tally(aggregate.get("active")), _list_href(list_query, listed)),
        "not_listed": _row(
            NOT_LISTED_LABEL,
            not_listed,
            _list_href(list_query, ("lifecycle_state", ",".join(active_states)), ("listed", "false")),
        ),
        "by_status": status_rows,
        "by_technology": ranked(aggregate.get("by_technology"), "technology", technology_label, "Not stated"),
        "by_iso": ranked(aggregate.get("by_iso"), "iso", iso_label, "None stated"),
        "sources": sources,
        "not_summed": not_summed,
        "no_capacity": max(totals["count"] - totals["capacity_records"] - totals["not_summed_records"], 0),
        "linked": True,
    }
