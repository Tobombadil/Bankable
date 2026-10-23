"""The map page's opening figures and its regional landing links (owner, 2026-10-23: "just rather a site
link that will do well when it lands in a contact's inbox. The product should do most of the work").

A contact who opens the link sees, before the map has drawn anything, three live counts under the
heading, each a link to the list behind it: active projects, planned data centres and other large
loads, and RFPs and funding calls open now. The same counts open the page's meta description, so the
preview card a mail client, LinkedIn, Slack or Teams draws says something specific. A count of zero
is left out (an empty figure is a poor first impression and says nothing a contact can act on); if
every count is zero or the API fails, the page keeps its usual description and shows no figures.

There is no "new this week" figure. A proposal carries no filing date of its own, only `first_seen`
(when this register first read it), so the week after a source's first load would count its whole
backlog as new: on 2026-10-23 the production store was four days old and every row would have
counted. It can come back once records carry the source's own filing date.

The figures appear on the plain map (`/`) and on a landing view (`/texas`), never on a view someone
filtered: that view's own notice already counts what it shows. Counts come from the public API as
an anonymous visitor sees it, so they never include a row the public tier may not show.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

from starlette.datastructures import QueryParams

from web.api_client import ApiClient, ApiError
from web.viewmodels import ACTIVE_PROPOSAL_STATES


@dataclass(frozen=True)
class Landing:
    """A short link that opens the map on one place, filtered to it: `/<slug>`."""

    slug: str
    jurisdiction: str
    name: str
    #: The map's `center` (lon, lat) and `zoom`: framed so the whole place fits a phone's width.
    center: tuple[float, float]
    zoom: float

    @property
    def href(self) -> str:
        return "/?" + urlencode(
            {
                "jurisdiction": self.jurisdiction,
                "center": f"{self.center[0]:.4f},{self.center[1]:.4f}",
                "zoom": f"{self.zoom:.2f}",
            }
        )


#: Texas first: the owner's own powered-land deals are there (docs/00-PLAN.md, 2026-10-23).
LANDINGS: tuple[Landing, ...] = (Landing("texas", "US-TX", "Texas", (-99.3, 31.3), 4.9),)
LANDINGS_BY_JURISDICTION = {landing.jurisdiction: landing for landing in LANDINGS}

#: Query parameters a landing view carries besides its jurisdiction: the map's framing and the
#: tracking tags a link in an email may carry. Anything else is a filter, and a filtered view shows
#: no figures.
_FRAMING_PARAMS = frozenset({"center", "zoom", "region", "placement", "layers", "asset_type"})
_TRACKING_PREFIXES = ("utm_",)

#: Seconds a set of counts is reused. They move hourly at most (the scheduler's cadence), and two
#: count queries on every home render would undo frontend audit F9's savings.
CACHE_SECONDS = 600


@dataclass(frozen=True)
class Figure:
    key: str
    count: int
    #: The words after the number, singular and plural.
    one: str
    many: str
    href: str

    @property
    def words(self) -> str:
        return self.one if self.count == 1 else self.many

    @property
    def sentence(self) -> str:
        return f"{self.count:,} {self.words}"


def landing_for(qp: QueryParams) -> Landing | None:
    """The landing a request is, if its only filter is a landing's jurisdiction."""
    landing = LANDINGS_BY_JURISDICTION.get((qp.get("jurisdiction") or "").strip().upper())
    if landing is None or not _only_framing(qp, allowed={"jurisdiction"}):
        return None
    return landing


def shows_figures(qp: QueryParams) -> bool:
    """The plain map or a landing: no filter beyond the map's framing and tracking tags."""
    return landing_for(qp) is not None or _only_framing(qp, allowed=set())


def _only_framing(qp: QueryParams, *, allowed: set[str]) -> bool:
    for name, value in qp.multi_items():
        if not value or name in allowed or name in _FRAMING_PARAMS or name.startswith(_TRACKING_PREFIXES):
            continue
        return False
    return True


def _scope(landing: Landing | None) -> dict[str, str]:
    return {"jurisdiction": landing.jurisdiction} if landing else {}


def _list_href(path: str, params: dict[str, str]) -> str:
    return f"{path}?{urlencode(params)}" if params else path


def active_figure(active: int, landing: Landing | None) -> Figure:
    """The active projects, from the count the map's own notice already made (`lifecycle_counts`'s
    active bucket for the same view), so this figure costs no call of its own."""
    return Figure(
        "active", active, "active project", "active projects", _list_href("/proposals", _scope(landing))
    )


def compute_figures(api: ApiClient, landing: Landing | None) -> list[Figure]:
    """Planned large loads and open RFPs and funding calls, for the whole store or one landing. A
    count the API refuses comes back as zero, so the page renders without it rather than failing."""
    scope = _scope(landing)
    specs: tuple[tuple[str, str, dict[str, Any], str, dict[str, str], str, str], ...] = (
        (
            "loads",
            "/v1/proposals",
            {**scope, "lifecycle_state": ",".join(ACTIVE_PROPOSAL_STATES), "kind": "load"},
            "/proposals",
            {**scope, "kind": "load"},
            "planned data centre or other large load",
            "planned data centres and other large loads",
        ),
        (
            "open",
            "/v1/opportunities",
            # The energy-relevant ones only, as the RFPs page shows by default (`web/app.py`
            # `opportunity_relevance`): on 2026-10-23, 490 of the 892 open notices; the rest are tenders
            # such as building services that the page leaves out, so the figure and its list agree.
            {**scope, "status": "open", "relevance": "energy"},
            "/opportunities",
            {**scope, "status": "open"},
            "RFP or funding call open now",
            "RFPs and funding calls open now",
        ),
    )
    figures = []
    for key, path, params, list_path, list_params, one, many in specs:
        try:
            envelope = api.get(path, params={**params, "limit": 1, "include": "count"})
            count = int((envelope.get("meta") or {}).get("total") or 0)
        except ApiError:
            count = 0
        figures.append(Figure(key, count, one, many, _list_href(list_path, list_params)))
    return figures


class FigureCache:
    """Counts per (API client, landing), kept `CACHE_SECONDS`. Keyed on the client itself, so a test
    that installs a new client never reads another test's counts."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._client: ApiClient | None = None
        self._entries: dict[str, tuple[float, list[Figure]]] = {}

    def get(self, api: ApiClient, landing: Landing | None) -> list[Figure]:
        key = landing.slug if landing else ""
        now = self._clock()
        with self._lock:
            if self._client is not api:
                self._client, self._entries = api, {}
            hit = self._entries.get(key)
            if hit is not None and now - hit[0] < CACHE_SECONDS:
                return hit[1]
        figures = compute_figures(api, landing)
        with self._lock:
            if self._client is api:
                self._entries[key] = (now, figures)
        return figures


FIGURES = FigureCache()


def opening_figures(api: ApiClient, landing: Landing | None, active: int) -> list[Figure]:
    """The figures the page shows, in order, zeros left out."""
    figures = [active_figure(active, landing), *FIGURES.get(api, landing)]
    return [f for f in figures if f.count > 0]


def figures_sentence(figures: list[Figure], landing: Landing | None) -> str:
    """ "12,431 active projects, 412 planned data centres and other large loads and 9 RFPs and funding
    calls open now in Texas." -- the meta description's opening, from the figures shown."""
    parts = [f.sentence for f in figures]
    if not parts:
        return ""
    joined = parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]
    return joined + (f" in {landing.name}" if landing else "") + "."
