"""A company's pipeline at a glance, for the top of `/organizations/{ident}` (owner, 2026-10-10:
the beta's first job is "check a developer's or owner's pipeline with a source for every claim").

Built only from what `GET /v1/organizations/{id}/proposals` already serves for the page's
ownership scope: each proposal's `lifecycle_state`, `technology`, `iso`, `kind`, `capacity_mw`,
`sponsor` and `provenance`. Nothing is estimated and the API is not asked for anything new; where a
figure would need the API to aggregate (a count beyond the pages read), the summary says it is
partial rather than extrapolating.

Every count links to `/proposals` filtered to the same rows: the sponsors behind the rows as
`sponsor_id` (one id, or the group's ids when the page covers subsidiaries; `sponsor_id` matches
exact organisations, `services/api/records.py::visible_organization_ids`), plus the status group,
technology, ISO or source. The status table counts every status, and its links name the states
outright; the technology and ISO tables count the active pipeline only (announced, in process,
contracted), which is the list's default view, so their links need no status at all.

Capacity is summed only where adding it means something: generation and storage. A data centre's
MW is demand, a line's is transfer capability, and a CO2 well has none, so those records are
counted, not summed, and the page says how many.
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

#: Rows per call (the API's `Limit` maximum) and calls per page view. 1,000 proposals covers every
#: sponsor in the current store (the largest sponsors 157 on the 2026-10-10 e2e load); past it the
#: summary says it is partial.
PAGE_SIZE = 200
MAX_PAGES = 5
#: Kinds whose `capacity_mw` is not generating or storage capacity, so it is never added to it.
NOT_SUMMED_KINDS = frozenset({"load", "transmission", "pipeline", "lng", "ccs", "hydrogen"})
#: The status chip family of each status group (docs/31 §1.2), for the icon beside its name.
STATUS_GROUP_FAMILY = {
    "announced": "neutral",
    "in_process": "progress",
    "contracted": "committed",
    "built": "success",
    "withdrawn": "danger",
    "unknown": "neutral",
}


@dataclass
class ProposalPages:
    """The raw proposal rows read for one company page, and whether that is all of them."""

    rows: list[dict[str, Any]] = field(default_factory=list)
    complete: bool = True
    failed: bool = False


def fetch_proposals(api: ApiClient, public_id: str, params: Mapping[str, Any]) -> ProposalPages:
    """Every proposal the organisation (at the page's scope) sponsors, up to `MAX_PAGES` pages. A
    failed first call leaves the section empty, as before; a failed later call keeps what was read
    and marks the result partial."""
    pages = ProposalPages()
    cursor: str | None = None
    for page_number in range(MAX_PAGES):
        query: dict[str, Any] = {"limit": PAGE_SIZE, **params}
        if cursor:
            query["cursor"] = cursor
        try:
            envelope = api.get(f"/v1/organizations/{public_id}/proposals", params=query)
        except ApiError:
            pages.failed = page_number == 0
            pages.complete = False
            return pages
        data = envelope.get("data")
        pages.rows.extend(dict(row) for row in data or [] if isinstance(row, Mapping))
        page = envelope.get("page")
        page = page if isinstance(page, Mapping) else {}
        cursor = page.get("next_cursor") if page.get("has_more") else None
        if not cursor:
            return pages
    pages.complete = False
    return pages


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _sponsor_id(row: Mapping[str, Any]) -> str | None:
    sponsor = row.get("sponsor")
    if isinstance(sponsor, Mapping) and sponsor.get("public_id"):
        return str(sponsor["public_id"])
    return None


def _list_href(sponsor_csv: str | None, *pairs: tuple[str, str]) -> str | None:
    if not sponsor_csv:
        return None
    return "/proposals?" + urlencode([("sponsor_id", sponsor_csv), *pairs])


@dataclass
class _Tally:
    count: int = 0
    mw: float = 0.0
    mw_rows: int = 0

    def add(self, row: Mapping[str, Any]) -> None:
        self.count += 1
        if row.get("kind") in NOT_SUMMED_KINDS:
            return
        mw = _number(row.get("capacity_mw"))
        if mw is not None:
            self.mw += mw
            self.mw_rows += 1

    def as_row(self, label: str, href: str | None) -> dict[str, Any]:
        return {
            "label": label,
            "count": self.count,
            "mw": round(self.mw, 1) if self.mw_rows else None,
            "href": href,
        }


def _ranked(tallies: Mapping[str | None, _Tally]) -> list[tuple[str | None, _Tally]]:
    """Largest capacity first, then most records; the "not stated" bucket last."""
    return sorted(
        tallies.items(),
        key=lambda item: (item[0] is None, -(item[1].mw if item[1].mw_rows else -1), -item[1].count),
    )


def pipeline_summary(
    rows: Sequence[Mapping[str, Any]], *, complete: bool = True, group: bool = False
) -> dict[str, Any] | None:
    """The pipeline block's context, or `None` when the company sponsors no proposal at this scope.

    `rows` are `GET /v1/organizations/{id}/proposals` entities as served. `complete` is whether
    they are all of them (`fetch_proposals`); `group` whether the page covers more than one
    organisation (it changes only the wording)."""
    if not rows:
        return None
    sponsor_ids = [_sponsor_id(row) for row in rows]
    # Links only when every row names its sponsor: otherwise the filtered list would hold fewer rows
    # than the count beside the link.
    distinct = sorted({s for s in sponsor_ids if s})
    sponsor_csv = ",".join(distinct) if distinct and all(sponsor_ids) else None

    by_status: dict[str, _Tally] = {key: _Tally() for key, _label, _states in PROPOSAL_STATUS_GROUPS}
    state_group = {state: key for key, _label, states in PROPOSAL_STATUS_GROUPS for state in states}
    active = _Tally()
    by_technology: dict[str | None, _Tally] = {}
    by_iso: dict[str | None, _Tally] = {}
    not_summed: dict[str, int] = {}
    no_capacity = 0
    sources: dict[str, dict[str, Any]] = {}
    total = _Tally()

    for row in rows:
        state = str(row.get("lifecycle_state") or "unknown")
        by_status[state_group.get(state, "unknown")].add(row)
        total.add(row)
        kind = row.get("kind")
        if kind in NOT_SUMMED_KINDS:
            not_summed[str(kind)] = not_summed.get(str(kind), 0) + 1
        elif _number(row.get("capacity_mw")) is None:
            no_capacity += 1
        if state in ACTIVE_PROPOSAL_STATES:
            active.add(row)
            by_technology.setdefault(row.get("technology") or None, _Tally()).add(row)
            by_iso.setdefault(row.get("iso") or None, _Tally()).add(row)
        for prov in _provenance(row):
            sid = str(prov.get("source_id") or "")
            if not sid:
                continue
            entry = sources.setdefault(
                sid,
                {
                    "source_id": sid,
                    "name": source_label(sid) or prov.get("source_name") or sid,
                    "ids": set(),
                    "retrieved_at": None,
                },
            )
            entry["ids"].add(row.get("public_id") or id(row))
            retrieved = prov.get("retrieved_at")
            if retrieved and (entry["retrieved_at"] is None or str(retrieved) > str(entry["retrieved_at"])):
                entry["retrieved_at"] = str(retrieved)

    all_states = ("lifecycle_state", ",".join(ALL_PROPOSAL_LIFECYCLE_STATES))
    status_rows = [
        {
            **by_status[key].as_row(label, _list_href(sponsor_csv, ("lifecycle_state", ",".join(states)))),
            "family": STATUS_GROUP_FAMILY[key],
        }
        for key, label, states in PROPOSAL_STATUS_GROUPS
        if by_status[key].count
    ]
    technology_rows = [
        tally.as_row(
            str(technology_label(token) or token) if token else "Not stated",
            _list_href(sponsor_csv, ("technology", token)) if token else None,
        )
        for token, tally in _ranked(by_technology)
    ]
    iso_rows = [
        tally.as_row(
            str(iso_label(token) or token) if token else "None stated",
            _list_href(sponsor_csv, ("iso", token)) if token else None,
        )
        for token, tally in _ranked(by_iso)
    ]
    source_rows = sorted(
        (
            {
                "name": entry["name"],
                "count": len(entry["ids"]),
                "retrieved_at": entry["retrieved_at"],
                "href": _list_href(sponsor_csv, ("source_id", sid), all_states),
            }
            for sid, entry in sources.items()
        ),
        key=lambda s: (-s["count"], s["name"]),
    )
    return {
        "group": group,
        "complete": complete,
        "total": total.as_row("All statuses", _list_href(sponsor_csv, all_states)),
        "active": active.as_row("Active pipeline", _list_href(sponsor_csv)),
        "by_status": status_rows,
        "by_technology": technology_rows,
        "by_iso": iso_rows,
        "sources": source_rows,
        "not_summed": [
            {"kind": kind, "label": str(proposal_kind_label(kind) or kind), "count": count}
            for kind, count in sorted(not_summed.items())
        ],
        "no_capacity": no_capacity,
        "linked": sponsor_csv is not None,
    }


def _provenance(row: Mapping[str, Any]) -> Iterable[Mapping[str, Any]]:
    raw = row.get("provenance")
    if not isinstance(raw, list):
        return ()
    return (p for p in raw if isinstance(p, Mapping))
