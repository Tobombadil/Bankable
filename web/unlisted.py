"""Records no register lists any more, hidden from the main list and the map by default (owner,
2026-10-11: "for an investor, a project that has dropped off every register is not live pipeline;
the toggle keeps it findable").

A project whose every register has dropped it keeps the lifecycle state the register last stated
(a removal is not a withdrawal, docs/51 §2.7), so without this it reads as active pipeline. The API
says which records these are (`listed`, `services/api/listing.py`); this module decides when the
list (`/proposals`) and the map (`/`) ask the API for listed records only.

**The rule.** A view hides them when every lifecycle state it selects is active
(`ACTIVE_PROPOSAL_STATES`): the default view, or any selection made only of active states. Then
every read the page makes for that view carries `listed=true`: the list, the map's geo and cluster
reads (through `/api/proposals/geo`), the lifecycle notice's active count, the empty-state facet
counts. So the counts on the page and the rows under them describe the same set. A view that
selects any other state (built, withdrawn, cancelled, unknown) hides nothing: a reader looking at
withdrawn or built projects expects them whether or not a register still lists them. A view whose
link sets `listed` itself (a company page's "No longer listed" row) keeps that filter and is not
changed either.

**The toggle.** `unlisted=include` is a web-only URL parameter that turns the hiding off. It is
never sent to the API: it is not one of the passthrough filters (`web/page.py`), which are pinned
to the API's own filter set (`web/test_list_filter_passthrough.py`), and every read builds its
parameters from those. The map and the list carry it between each other (`list_view_href`,
`map_view_href`, `map.js`).

**What is not changed.** Saved searches: the hiding is not in the query string, so "Save this
search as an alert" stays on the default view (`web/app.py::_alert_cannot_watch`), and an alert goes
on matching every record, which is right, because a project leaving its register is exactly what a
follower wants to hear. The RSS and JSON feeds are change feeds and keep every record. The company
pipeline and record pages already say "no longer listed" in their own words.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any
from urllib.parse import urlencode

from web.api_client import ApiClient, ApiError
from web.viewmodels import ACTIVE_PROPOSAL_STATES, resolve_proposal_lifecycle_param

#: The web-only parameter and the one value that turns the hiding off.
UNLISTED_PARAM = "unlisted"
UNLISTED_INCLUDE = "include"


def include_unlisted(qp: Mapping[str, str]) -> bool:
    """Whether the reader asked to see records no register lists any more (`unlisted=include`)."""
    return (qp.get(UNLISTED_PARAM) or "").strip().lower() == UNLISTED_INCLUDE


def _view_states(qp: Mapping[str, str]) -> list[str]:
    csv, _explicit, _withdrawn = resolve_proposal_lifecycle_param(qp)
    return [s for s in csv.split(",") if s]


def unlisted_rule_applies(qp: Mapping[str, str]) -> bool:
    """Whether this view's lifecycle selection is made only of active states and its link sets no
    `listed` filter of its own: the views the hiding is for, whether or not the toggle is on."""
    if (qp.get("listed") or "").strip():
        return False
    states = _view_states(qp)
    return bool(states) and all(s in ACTIVE_PROPOSAL_STATES for s in states)


def hides_unlisted(qp: Mapping[str, str]) -> bool:
    """Whether the page asks the API for listed records only (module docstring, "The rule")."""
    return unlisted_rule_applies(qp) and not include_unlisted(qp)


def listed_param(qp: Mapping[str, str]) -> dict[str, str]:
    """`{"listed": "true"}` for a view that hides them, else nothing: merged into every API read
    the page makes for the view (rows, geo, counts), never anything else."""
    return {"listed": "true"} if hides_unlisted(qp) else {}


def unlisted_query_items(qp: Mapping[str, str]) -> list[tuple[str, str]]:
    """The toggle as the links between the map and the list carry it: one pair, or nothing."""
    return [(UNLISTED_PARAM, UNLISTED_INCLUDE)] if include_unlisted(qp) else []


def unlisted_toggle(qp: Mapping[str, str]) -> dict[str, Any]:
    """The filter bar's checkbox (`_macros.html` `status_filter`)."""
    return {"name": UNLISTED_PARAM, "value": UNLISTED_INCLUDE, "checked": include_unlisted(qp)}


def _items(qp: Mapping[str, str]) -> list[tuple[str, str]]:
    multi = getattr(qp, "multi_items", None)
    return list(multi()) if callable(multi) else list(qp.items())


def show_unlisted_href(path: str, qp: Mapping[str, str], *, drop: Iterable[str] = ()) -> str:
    """The same view with the toggle on: the hidden-records line's link."""
    dropped = {"cursor", "focus", UNLISTED_PARAM, *drop}
    kept = [(k, v) for k, v in _items(qp) if v and k not in dropped]
    kept.append((UNLISTED_PARAM, UNLISTED_INCLUDE))
    return path + "?" + urlencode(kept)


def hidden_unlisted_count(
    api: ApiClient, qp: Mapping[str, str], filters: Iterable[str], *, surface: str = "list"
) -> int | None:
    """How many records the hiding takes out of this view: the view's own filters and lifecycle
    states with `listed=false`, one `limit=1&include=count` call (the cost of one lifecycle-notice
    bucket; docs/30 §5.1 has the measurement). On the map it counts every placement grade, as the
    map's count line does. `None` when the view hides nothing or the API cannot say."""
    if not hides_unlisted(qp):
        return None
    params: dict[str, str] = {n: qp[n] for n in filters if qp.get(n)}
    if surface == "map":
        params.pop("placement", None)
    params["lifecycle_state"] = ",".join(_view_states(qp))
    try:
        envelope = api.get(
            "/v1/proposals", params={**params, "listed": "false", "limit": 1, "include": "count"}
        )
        return int((envelope.get("meta") or {}).get("total") or 0)
    except (ApiError, TypeError, ValueError):
        return None


def unlisted_notice(
    api: ApiClient,
    qp: Mapping[str, str],
    filters: Iterable[str],
    *,
    path: str,
    surface: str = "list",
    drop: Iterable[str] = (),
) -> dict[str, Any] | None:
    """The one line saying how many such records the view hides, with the link that shows them;
    `None` when it hides none (or the count failed: the line is a pointer, not a claim)."""
    count = hidden_unlisted_count(api, qp, filters, surface=surface)
    if not count:
        return None
    return {"count": count, "show_href": show_unlisted_href(path, qp, drop=drop)}
