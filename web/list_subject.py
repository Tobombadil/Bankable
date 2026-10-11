"""What a filtered proposal list is about, said in its heading (2026-10-10, lane P).

`/proposals?sponsor_id=...` is where every count on a company page's pipeline leads, and
`/proposals?interconnection_point_id=...` is the connection-point page's "every project queued
here" link; both opened an unlabelled "Proposals" list, so a reader who followed one had to trust
the URL. The list now names its subject, links back to the subject's own page, and keeps the
subject filter when the other filters change (`web/templates/proposals_list.html`).

- One sponsor: "Proposals sponsored by <name>", "and its subsidiaries" under `sponsor_scope=all`,
  "and its direct subsidiaries" under `children`; the name and slug come from the API
  (`GET /v1/organizations/{id}`, or `?slug=` for a slug). Several sponsors: how many, no names.
- One connection point: "Proposals connecting at <name>", the name from the rows' own
  `interconnection_point` embed when a row carries it, else `GET /v1/interconnection-points/{id}`.

Any failed read leaves the heading as it was ("Proposals"), never the page.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import quote

from web.api_client import ApiClient, ApiError
from web.formatting import readable_name

#: The query parameters that make a list about one thing; the list form carries them as hidden fields.
SUBJECT_FILTERS = ("sponsor_id", "sponsor_scope", "interconnection_point_id")
_SCOPE_WORDS = {"children": " and its direct subsidiaries", "all": " and its subsidiaries"}
_SCOPE_WORDS_PLURAL = {"children": " and their direct subsidiaries", "all": " and their subsidiaries"}


def _csv(value: str | None) -> list[str]:
    return [v.strip() for v in (value or "").split(",") if v.strip()]


def _organization(api: ApiClient, ref: str) -> Mapping[str, Any] | None:
    try:
        if ref.startswith("org_"):
            data = api.get(f"/v1/organizations/{quote(ref, safe='')}").get("data")
            return data if isinstance(data, Mapping) else None
        rows = api.get("/v1/organizations", params={"slug": ref, "limit": 1}).get("data") or []
    except ApiError:
        return None
    return rows[0] if rows and isinstance(rows[0], Mapping) else None


def _sponsor_subject(api: ApiClient, qp: Mapping[str, str]) -> dict[str, Any] | None:
    refs = _csv(qp.get("sponsor_id"))
    if not refs:
        return None
    scope = (qp.get("sponsor_scope") or "").strip().lower()
    if len(refs) > 1:
        heading = f"Proposals sponsored by any of {len(refs)} companies{_SCOPE_WORDS_PLURAL.get(scope, '')}"
        return {"heading": heading, "name": None, "href": None, "link_label": None}
    org = _organization(api, refs[0])
    if org is None or not org.get("name_canonical"):
        return None
    name = readable_name(org["name_canonical"])
    slug = org.get("slug") or org.get("public_id")
    return {
        "heading": f"Proposals sponsored by {name}{_SCOPE_WORDS.get(scope, '')}",
        "name": name,
        "href": f"/organizations/{quote(str(slug), safe='')}" if slug else None,
        "link_label": "Company page",
    }


def _point_subject(
    api: ApiClient, qp: Mapping[str, str], rows: Sequence[Mapping[str, Any]]
) -> dict[str, Any] | None:
    refs = _csv(qp.get("interconnection_point_id"))
    if len(refs) != 1:
        return None
    point_id = refs[0]
    name: Any = None
    for row in rows:
        embed = row.get("interconnection_point")
        if isinstance(embed, Mapping) and embed.get("public_id") == point_id:
            name = embed.get("name")
            break
    if not name:
        try:
            name = (api.get(f"/v1/interconnection-points/{quote(point_id, safe='')}").get("data") or {}).get(
                "name"
            )
        except (ApiError, AttributeError):
            name = None
    if not name:
        return None
    return {
        "heading": f"Proposals connecting at {name}",
        "name": str(name),
        "href": f"/interconnection-points/{quote(point_id, safe='')}",
        "link_label": "Connection point",
    }


def list_subject(
    api: ApiClient, qp: Mapping[str, str], rows: Sequence[Mapping[str, Any]]
) -> dict[str, Any] | None:
    """The list's subject (module docstring), or `None` when it is about no one thing. `rows` are
    the API's proposal rows on this page, as served."""
    return _sponsor_subject(api, qp) or _point_subject(api, qp, rows)


def subject_hidden_fields(qp: Mapping[str, str]) -> list[tuple[str, str]]:
    """`(name, value)` for each subject filter in the URL, for the filter form's hidden inputs: a
    reader who changes the technology on a company's list stays on that company's list."""
    return [(name, qp[name]) for name in SUBJECT_FILTERS if qp.get(name)]
