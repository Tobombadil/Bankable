"""The map landing's front strip: its one line, its three figures, when the sources were read, and the
regional landing links (owner, 2026-10-23: "just rather a site link that will do well when it lands in a
contact's inbox. The product should do most of the work"; 2026-10-24: direction A, "Night survey").

The strip (docs/31-brand-identity.md M-3, docs/30 §11.6) says what the product is in one plain line,
"Planned US energy projects, each linked to its source.", then three live figures, each a link to the
list behind it: the active US projects, the data centres among them, and the calls for bids and
funding open now. Then "Sources read {date}", from the scheduled sources' last fetches. The same
figures open the page's meta description, so the preview card a mail client, LinkedIn, Slack or Teams
draws says something specific. A count of zero is left out (an empty figure is a poor first impression
and says nothing a contact can act on); if every count is zero or the API fails, the page keeps its
usual description and shows no figures.

**Each count is what its link lists.** A figure's link is built first, and its count asks the API
exactly what the linked list page asks it for that link (`proposal_list_query`,
`opportunity_list_query`: the list pages' own filters, defaults and the hidden-records rule), so the
number on the landing and the "N match" on the list it opens are one query (web/test_headline.py holds
the two to the same parameters). "The US" is every US jurisdiction by code (`US_JURISDICTIONS`):
`jurisdiction` takes exact codes, and the list pages do not pass `country` through yet.

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
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

from starlette.datastructures import QueryParams

from web.api_client import ApiClient, ApiError
from web.formatting import display_date
from web.page import OPPORTUNITY_PASSTHROUGH_FILTERS, PROPOSAL_PASSTHROUGH_FILTERS
from web.unlisted import listed_param
from web.viewmodels import opportunity_status_param, resolve_proposal_lifecycle_param

#: Every jurisdiction code that is the United States: the country itself, the 50 states and the
#: District of Columbia (ISO 3166-2:US), the six outlying areas, and the outer continental shelf the
#: federal registers file offshore wind under (`US-OCS`, services/api/country.py).
US_JURISDICTIONS: tuple[str, ...] = (
    "US",
    *(
        "US-" + code
        for code in (
            "AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH "
            "NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY AS GU MP PR UM VI OCS"
        ).split()
    ),
)


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

#: Seconds a set of counts is served before it is asked again. The lists behind the figures are live, so
#: after an hourly ingest a figure may disagree with the "N match" of the list it opens until the next
#: refresh: a minute at most, not the ten it was. The refresh runs beside the request that finds the
#: counts stale (stale-while-refresh, as `coverage_data` does), so no visitor waits for the three count
#: queries (about 170 ms on the dev store, 2026-10-24) after the first render of a process; that keeps
#: frontend audit F9's savings. The web process cannot ask the API for its data version cheaply (only
#: the API holds `geo_cache.data_version`), so the window is a time, not a version.
CACHE_SECONDS = 60


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


def place_name(landing: Landing | None) -> str:
    """The place the strip's line and figures are about: a landing's, else the United States."""
    return landing.name if landing else "US"


def strip_line(landing: Landing | None) -> str:
    """The landing's one plain line, its h1 (owner, 2026-10-24, the share card's line)."""
    return f"Planned {place_name(landing)} energy projects, each linked to its source."


def _scope(landing: Landing | None) -> dict[str, str]:
    return {"jurisdiction": landing.jurisdiction if landing else ",".join(US_JURISDICTIONS)}


def _href(path: str, query: Mapping[str, str]) -> str:
    """The link, its comma-separated codes left readable (a comma is legal in a query string)."""
    return f"{path}?{urlencode(query, safe=',')}" if query else path


def proposal_list_query(query: Mapping[str, str]) -> dict[str, str]:
    """What `/proposals?<query>` asks `GET /v1/proposals` for to list and count its rows (web/app.py
    `_proposal_params`): the filters the list passes through, its lifecycle default (active) and, on a
    view of active states, `listed=true`, so records no register lists any more are left out of the
    count as the list leaves them out of its rows."""
    params = {name: query[name] for name in PROPOSAL_PASSTHROUGH_FILTERS if query.get(name)}
    params["lifecycle_state"] = resolve_proposal_lifecycle_param(query)[0]
    params.update(listed_param(query))
    return params


def opportunity_list_query(query: Mapping[str, str]) -> dict[str, str]:
    """What `/opportunities?<query>` asks `GET /v1/opportunities` for (web/app.py
    `opportunities_list`): the filters it passes through, its status and its default scope, the
    energy-relevant notices (`relevance=energy` unless the link says `relevance=all`)."""
    params = {name: query[name] for name in OPPORTUNITY_PASSTHROUGH_FILTERS if query.get(name)}
    params["status"] = opportunity_status_param(query)
    if (query.get("relevance") or "").strip().lower() != "all":
        params["relevance"] = "energy"
    return params


#: `(key, list path, the link's own query beyond the place, singular, plural)`, in the strip's order.
#: The words are the share card's (owner, 2026-10-24): "projects", "data centres", "open calls". A
#: `load` record is a data centre where a data-centre connector wrote it; the classifier's `load` rule can
#: also write a queue's large load. On the store measured for this lane every load came from a
#: data-centre source (Virginia DEQ's flagged air sites, EPA ECHO's data-centre facilities), as on the
#: live register of 2026-10-24; should a queue's large loads join them, the words widen to "data centres
#: and large loads" (docs/30 §11.6 notes). A call is open for bids or applications: an RFP or a funding
#: notice.
_FIGURES: tuple[tuple[str, str, dict[str, str], str, str], ...] = (
    ("active", "/proposals", {}, "project", "projects"),
    ("loads", "/proposals", {"kind": "load"}, "data centre", "data centres"),
    ("open", "/opportunities", {"status": "open"}, "open call", "open calls"),
)


def compute_figures(api: ApiClient, landing: Landing | None) -> list[Figure]:
    """The strip's three figures for the United States or one landing, each counted with exactly the
    query its link's list page sends (`proposal_list_query`, `opportunity_list_query`). A count the
    API refuses comes back as zero, so the page renders without it rather than failing."""
    figures = []
    for key, path, own, one, many in _FIGURES:
        link = {**_scope(landing), **own}
        if path == "/proposals":
            endpoint, params = "/v1/proposals", proposal_list_query(link)
        else:
            endpoint, params = "/v1/opportunities", opportunity_list_query(link)
        try:
            envelope = api.get(endpoint, params={**params, "limit": 1, "include": "count"})
            count = int((envelope.get("meta") or {}).get("total") or 0)
        except ApiError:
            count = 0
        figures.append(Figure(key, count, one, many, _href(path, link)))
    return figures


def _in_thread(work: Callable[[], None]) -> None:
    threading.Thread(target=work, daemon=True).start()


class FigureCache:
    """Counts per (API client, landing). The first request of a process counts them while it renders;
    after that, counts older than `CACHE_SECONDS` are served once more while one refresh runs beside
    the request (`spawn`, a daemon thread unless a test passes its own), and the next render reads the
    new counts. Keyed on the client itself, so a test that installs a new client never reads another
    test's counts."""

    def __init__(
        self,
        clock: Callable[[], float] = time.monotonic,
        spawn: Callable[[Callable[[], None]], None] = _in_thread,
    ) -> None:
        self._clock = clock
        self._spawn = spawn
        self._lock = threading.Lock()
        self._client: ApiClient | None = None
        self._entries: dict[str, tuple[float, list[Figure]]] = {}
        self._refreshing: set[str] = set()

    def get(self, api: ApiClient, landing: Landing | None) -> list[Figure]:
        key = landing.slug if landing else ""
        now = self._clock()
        with self._lock:
            if self._client is not api:
                self._client, self._entries, self._refreshing = api, {}, set()
            hit = self._entries.get(key)
            if hit is not None and (now - hit[0] < CACHE_SECONDS or key in self._refreshing):
                return hit[1]
            if hit is not None:
                self._refreshing.add(key)
        if hit is None:
            return self._count(api, landing, key)
        try:
            self._spawn(lambda: self._refresh(api, landing, key))
        except RuntimeError:
            # No thread to run it in (`can't start new thread`): the last counts stand, and the next
            # request tries again rather than finding the refresh marked as running for good.
            self._release(api, key)
        return hit[1]

    def _count(self, api: ApiClient, landing: Landing | None, key: str) -> list[Figure]:
        stamp = self._clock()
        figures = compute_figures(api, landing)
        with self._lock:
            if self._client is api:
                self._entries[key] = (stamp, figures)
        return figures

    def _refresh(self, api: ApiClient, landing: Landing | None, key: str) -> None:
        try:
            self._count(api, landing, key)
        finally:
            self._release(api, key)

    def _release(self, api: ApiClient, key: str) -> None:
        """Ends `key`'s refresh, unless the cache has moved to another client since: that client's own
        refresh of the same key is not this one's to end."""
        with self._lock:
            if self._client is api:
                self._refreshing.discard(key)


FIGURES = FigureCache()


def opening_figures(api: ApiClient, landing: Landing | None) -> list[Figure]:
    """The figures the strip shows, in order, zeros left out."""
    return [f for f in FIGURES.get(api, landing) if f.count > 0]


def figures_sentence(figures: list[Figure], landing: Landing | None) -> str:
    """ "3,787 projects (73 of them data centres) and 10 open calls in the US." -- the meta description's
    opening, from the figures shown. The data centres are counted among the projects (the same list,
    `kind=load`), so the sentence says so rather than reading as three separate sets; without the
    projects' figure they stand on their own."""
    by_key = {f.key: f for f in figures}
    active, loads = by_key.get("active"), by_key.get("loads")
    parts = []
    for f in figures:
        if f is loads and active is not None:
            continue
        if f is active and loads is not None:
            among = "a data centre" if loads.count == 1 else "data centres"
            parts.append(f"{f.sentence} ({loads.count:,} of them {among})")
        else:
            parts.append(f.sentence)
    if not parts:
        return ""
    joined = parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]
    return f"{joined} in {landing.name if landing else 'the US'}."


@dataclass(frozen=True)
class SourcesRead:
    """When the scheduled sources were last read: the oldest and newest fetch dates, as one stamp."""

    oldest: str
    newest: str

    @property
    def parts(self) -> list[tuple[str, str]]:
        """`(text, ISO date)` for each `<time>` of the stamp: one day ("24 Oct 2026"), or a range that
        names a month or year once where the two share it ("22–24 Oct 2026", "30 Sep – 2 Oct 2026",
        "30 Dec 2025 – 2 Jan 2026")."""
        first, last = display_date(self.oldest), display_date(self.newest)
        if self.oldest == self.newest:
            return [(last, self.newest)]
        if self.oldest[:7] == self.newest[:7]:
            return [(first.split(" ", 1)[0], self.oldest), (last, self.newest)]
        if self.oldest[:4] == self.newest[:4]:
            return [(first.rsplit(" ", 1)[0], self.oldest), (last, self.newest)]
        return [(first, self.oldest), (last, self.newest)]

    @property
    def separator(self) -> str:
        """An en dash, spaced when either side has more than a day in it."""
        parts = self.parts
        if len(parts) < 2:
            return ""
        return "–" if " " not in parts[0][0] else " – "

    @property
    def text(self) -> str:
        return self.separator.join(text for text, _iso in self.parts)


def sources_read(freshness: Mapping[str, Any]) -> SourcesRead | None:
    """The stamp from `web.viewmodels.source_freshness` (the scheduled sources' `fetched_at` from
    `/v1/coverage`), or `None` when the API could not say: the strip then claims nothing."""
    if not freshness.get("available"):
        return None
    oldest, newest = freshness.get("oldest_fetch"), freshness.get("newest_fetch")
    if not oldest or not newest:
        return None
    return SourcesRead(str(oldest)[:10], str(newest)[:10])
