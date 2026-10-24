"""Record pages as URL tabs (Navigation v2, docs/30 §11.5 and §11.10; lane V3): what a project, a plant,
pipeline or line, and a grid connection point show on each tab, which tabs a record has, and the URL rules
every record route shares.

- **T-1** One header on every tab (`partials/_record_header.html`): breadcrumb, name, status chip or type
  badge, the company line, a line of key figures whose terms link to `/glossary`, the one primary action
  and the tier notice, then the tab bar (`partials/_record_tabs.html`).
- **T-3** A tab is linked only when it has content, and a tab bar only with two or more tabs. A tab URL
  reached while its tab is empty answers 404 with the header, the tab bar and one line
  (`ABSENT_LINES`); an unknown segment is the plain not-found page.
- **T-4** Every tab ends with its own credits line (`credits`) and the record's attribution line; the
  Sources tab holds the full panel.
- **T-6** Tabs are path segments under the record URL. `?tab=` answers 301 to the segment
  (`query_tab_redirect`) with every other parameter kept, `{record}/overview` answers 301 to the record
  (`overview_redirect`), and a merged project's old slug answers 301 to the survivor with the segment
  kept (`web/app.py`).
- **T-7** Only the Overview is indexable, in the sitemap and carries JSON-LD; tab pages are
  `noindex, follow` and canonical to themselves.

The routes live with their record type (`web/app.py` for projects, `web/assets_pages.py`,
`web/interconnection_points.py`); this module holds only what they share, so the company tabs of lane V4
can use it too. It reads nothing private: Track is offered by `web/my_projects.py::track_offer`, which
sees only the request and the public record.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, urlencode

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse

from web.formatting import display_date, mw
from web.labels import LIFECYCLE_LABELS, REUSE_CLASS_LABELS, SECTION_LABELS, lifecycle_label
from web.my_projects import track_offer
from web.page import get_api, get_free_alerts, get_lag_days, is_preview_active, paid_tiers_offered, templates
from web.viewmodels import source_freshness, source_label

#: The tab names readers see (T-2: one or two words). The keys are the path segments; the Overview is the
#: record's own URL and has none.
TAB_LABELS: dict[str, str] = {
    "overview": "Overview",
    "timeline": "Timeline",
    "sources": "Sources",
    "matches": "Matches",
    "retirement": "Retirement",
    "grid": "Grid",
    "ownership": "Ownership",
    "nearby": "Nearby",
    "projects": "Projects",
    "changes": "Changes",
}

#: Each record type's tabs, in order (docs/30 §11.5). A tab missing from a record is left out of its bar.
#: Matches stays in the project's list so its URL is a tab (404 with the header) rather than an unknown
#: address: matches are withheld from readers until a rule set clears US-401 AC3 (IA-9).
PROPOSAL_TABS: tuple[str, ...] = ("overview", "timeline", "sources", "matches")
ASSET_TABS: tuple[str, ...] = ("overview", "retirement", "grid", "ownership", "nearby", "sources")
POINT_TABS: tuple[str, ...] = ("overview", "projects", "changes", "sources")

#: What a tab URL says when the record has nothing for it (T-3): one line under the header and tab bar.
ABSENT_LINES: dict[tuple[str, str], str] = {
    ("proposal", "matches"): "No matches are published for this project.",
    ("asset", "retirement"): "No retired or scheduled generators are recorded for this asset.",
    ("asset", "grid"): "No grid connection is recorded for this asset.",
    ("asset", "ownership"): "Every ownership record is on the line under the name.",
    ("asset", "nearby"): "No exact-grade projects are recorded within 25 km.",
    ("interconnection_point", "projects"): "No projects to show at this point.",
    ("interconnection_point", "changes"): "No change events are recorded for the projects here yet.",
}

#: The plain-language help (owner, 2026-10-23: readable by anyone). A status chip links to its lifecycle
#: state key (`web/labels.py` `LIFECYCLE_LABELS`); a technical term in a record header links to one of
#: these anchors. Lane search-about builds `/glossary` with exactly these ids.
GLOSSARY_PATH = "/glossary"
GLOSSARY_TERMS: frozenset[str] = frozenset(
    {
        "interconnection-queue",
        "queue-position",
        "capacity",
        "in-service-date",
        "large-load",
        "rfp",
        "funding-call",
    }
)

#: How many rows of another tab the Overview shows before "All N in {tab}" (T-5).
OVERVIEW_ROWS = 5


def glossary_href(key: str | None) -> str | None:
    """`/glossary#<key>` for a glossary term or a lifecycle state key, else `None` (no link)."""
    if not key or (key not in GLOSSARY_TERMS and key not in LIFECYCLE_LABELS):
        return None
    return f"{GLOSSARY_PATH}#{key}"


def tab_href(base_path: str, key: str) -> str:
    """The URL of tab `key` under a record whose Overview is `base_path`."""
    return base_path if key == "overview" else f"{base_path}/{key}"


@dataclass(frozen=True)
class Tabs:
    """One record's tabs for one request: which exist, their counts, and which one is open."""

    record_type: str
    base_path: str
    order: tuple[str, ...]
    present: Mapping[str, bool]
    current: str
    counts: Mapping[str, int | None] = field(default_factory=dict)
    #: The record's own words for an absent tab, where the line in `ABSENT_LINES` would not be true of it.
    absent_lines: Mapping[str, str] = field(default_factory=dict)

    def has(self, key: str) -> bool:
        return key == "overview" or bool(self.present.get(key))

    def href(self, key: str) -> str:
        return tab_href(self.base_path, key)

    def label(self, key: str) -> str:
        return TAB_LABELS[key]

    @property
    def shown(self) -> list[dict[str, Any]]:
        """The tab bar's links, in order (T-3: an absent tab is not linked)."""
        return [
            {
                "key": key,
                "label": TAB_LABELS[key],
                "href": self.href(key),
                "count": self.counts.get(key),
                "current": key == self.current,
            }
            for key in self.order
            if self.has(key)
        ]

    @property
    def bar(self) -> bool:
        """Whether the tab bar is drawn (T-3: only with two or more tabs)."""
        return len(self.shown) >= 2

    @property
    def is_overview(self) -> bool:
        return self.current == "overview"

    @property
    def absent(self) -> bool:
        return not self.has(self.current)

    @property
    def absent_line(self) -> str:
        return self.absent_lines.get(self.current) or ABSENT_LINES.get(
            (self.record_type, self.current), "Nothing is recorded here for this record."
        )

    @property
    def sources_href(self) -> str:
        return self.href("sources")


def is_tab(order: Sequence[str], tab: str) -> bool:
    """Whether `tab` names a tab of this record type (`overview` is the record URL, never a segment)."""
    return tab != "overview" and tab in order


def _kept_query(request: Request, *, drop: Iterable[str] = ("tab",)) -> str:
    dropped = set(drop)
    kept = [(k, v) for k, v in request.query_params.multi_items() if k not in dropped]
    return ("?" + urlencode(kept)) if kept else ""


def query_tab_redirect(
    request: Request, base_path: str, order: Sequence[str], current: str = "overview"
) -> RedirectResponse | None:
    """`{record}?tab={name}` (docs/16 U-4's form) answers 301 to `{record}/{name}` with every other
    parameter kept (T-6). An empty `tab=`, or a name that is no tab of this record type, keeps the tab
    the path asked for (`current`: the Overview on the record's own URL). `None` when the request
    carries no `tab`."""
    if "tab" not in request.query_params:
        return None
    name = (request.query_params.get("tab") or "").strip().lower()
    if not is_tab(order, name):
        name = current if is_tab(order, current) else "overview"
    return RedirectResponse(tab_href(base_path, name) + _kept_query(request), status_code=301)


def overview_redirect(request: Request, base_path: str) -> RedirectResponse:
    """`{record}/overview` answers 301 to the record's own URL, query kept: the Overview has no segment
    (T-6), and a second address for it would be a copy of the indexed page."""
    return RedirectResponse(base_path + _kept_query(request, drop=()), status_code=301)


def moved_redirect(request: Request, collection: str, slug: str, tab: str) -> RedirectResponse:
    """A merged record's old slug answers 301 to the survivor on the same tab, query kept (T-6)."""
    target = tab_href(f"/{collection}/{quote(slug, safe='')}", tab)
    return RedirectResponse(target + _kept_query(request, drop=()), status_code=301)


def tier_notice(request: Request) -> dict[str, Any]:
    """The tier notice (docs/04 D-3, D-28) for a supply-side record: the same values `web/app.py`
    `delayed_notice(request, "proposal")` builds, for the routes outside `web/app.py`, which cannot import
    it back (`web/app.py` includes their routers). `web/test_record_tabs.py` pins the two to one answer."""
    lag_days = get_lag_days(request)["supply"]
    now = dt.datetime.now(dt.UTC)
    return {
        "lag_days": lag_days,
        "data_as_of": (now - dt.timedelta(days=lag_days)).strftime("%Y-%m-%d"),
        "preview_active": is_preview_active(request),
        "free_alerts": get_free_alerts(request),
        "paid_tiers_offered": paid_tiers_offered(request),
        "freshness": source_freshness(request, get_api(request)),
    }


def credits(*groups: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The registers behind a tab's rows (T-4), once each in first-seen order: the short name, the
    licence's words, the latest retrieval date and the licence's own credit text where it sets one (NESO's
    must appear exactly as written, L-2). Each group is a list of provenance rows (`source_id`,
    `source_name`, `reuse_class`, `retrieved_at`, `attribution_text`); gated sources never reach a page, so
    a gated register is never named (D-34)."""
    out: dict[str, dict[str, Any]] = {}
    for group in groups:
        for row in group or []:
            if not isinstance(row, Mapping):
                continue
            name = str(
                source_label(row.get("source_id")) or row.get("source_name") or row.get("source_id") or ""
            )
            if not name:
                continue
            retrieved = row.get("retrieved_at")
            reuse = row.get("reuse_class")
            credit = _credit_text(row.get("attribution_text"), name)
            seen = out.get(name)
            if seen is None:
                out[name] = {
                    "name": name,
                    "licence": REUSE_CLASS_LABELS.get(str(reuse)) if reuse else None,
                    "retrieved_at": retrieved,
                    "credit": credit,
                }
                continue
            # A row that names its licence or credit completes one that only named the register.
            seen["licence"] = seen["licence"] or (REUSE_CLASS_LABELS.get(str(reuse)) if reuse else None)
            seen["credit"] = seen["credit"] or credit
            if retrieved and str(retrieved) > str(seen.get("retrieved_at") or ""):
                seen["retrieved_at"] = retrieved
    return list(out.values())


def _credit_text(text: Any, name: str) -> str | None:
    """A licence's own credit text, or `None` when it only repeats the register's name ("EIA-860M (open
    licence; EIA-860M, …)" says the name twice). Any other text is kept exactly as written (L-2)."""
    if not text:
        return None
    words = str(text).strip()
    return None if words.rstrip(".").strip().casefold() == name.strip().casefold() else words


def summary_rows(envelope: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """An API envelope's `licence_summary.sources` (the registers behind exactly the rows it carries) as
    the provenance rows `credits` reads."""
    summary = (envelope or {}).get("licence_summary")
    sources = summary.get("sources") if isinstance(summary, Mapping) else None
    return [
        {
            "source_id": src.get("source_id"),
            "source_name": src.get("name"),
            "reuse_class": src.get("reuse_class"),
            "attribution_text": src.get("attribution_text"),
            "retrieved_at": src.get("retrieved_at_max") or src.get("retrieved_at"),
        }
        for src in sources or []
        if isinstance(src, Mapping)
    ]


def row_provenance(rows: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """Every provenance row carried by a list of flattened records (each row's own `provenance`)."""
    out: list[Mapping[str, Any]] = []
    for row in rows:
        for prov in row.get("provenance") or []:
            if isinstance(prov, Mapping):
                out.append(prov)
    return out


def _coords(value: Any) -> list[tuple[float, float]]:
    if (
        isinstance(value, (list, tuple))
        and len(value) >= 2
        and all(isinstance(v, (int, float)) for v in value[:2])
    ):
        return [(float(value[0]), float(value[1]))]
    out: list[tuple[float, float]] = []
    if isinstance(value, (list, tuple)):
        for part in value:
            out.extend(_coords(part))
    return out


def map_href(geometry: Mapping[str, Any] | None) -> str | None:
    """The map centred on a plant, pipeline or line (`map.js` reads `center` and `zoom`): a point at a
    town's zoom, a route at its middle vertex a little further out. `None` without a geometry."""
    if not isinstance(geometry, Mapping):
        return None
    points = _coords(geometry.get("coordinates"))
    if not points:
        return None
    lon, lat = points[len(points) // 2]
    zoom = 9 if str(geometry.get("type")) == "Point" else 7
    return "/?" + urlencode({"center": f"{lon:.4f},{lat:.4f}", "zoom": f"{zoom:.2f}"})


def source_count(rows: Iterable[Mapping[str, Any]]) -> int:
    """How many registers a Sources tab lists (its count in the tab bar, T-2)."""
    return len(credits(rows))


def primary_action(
    request: Request,
    tabs: Tabs,
    track_type: str,
    record: Mapping[str, Any],
    *fallbacks: tuple[str, str | None],
) -> dict[str, Any] | None:
    """The header's one primary action (docs/30 §11.4): "Track" wherever the Track block is offered (it
    lives on the Overview, after the facts: `docs/30` §11.5), else the first fallback `(label, href)` that
    has an href. On the Overview the Track link moves down the page to the block; on another tab it opens
    the Overview there."""
    if track_offer(request, track_type, record):
        track = "#track-heading" if tabs.is_overview else f"{tabs.base_path}#track-heading"
        return {"label": "Track", "href": track, "track": True}
    for label, href in fallbacks:
        if href:
            return {"label": label, "href": href, "track": False}
    return None


# ------------------------------------------------------------------------------------- the header
#: The trail every record breadcrumb starts with (docs/30 §11.3: "Market / Companies / {name}").
MARKET_CRUMB: tuple[str, str] = (SECTION_LABELS["market"], "/")


def crumbs(tabs: Tabs, trail: Sequence[tuple[str, str]], name: str) -> list[tuple[str, str | None]]:
    """The visible breadcrumb: Market, the record's list (and its parent tab), then the record, which
    links to its Overview from every other tab and is the current page on the Overview."""
    return [MARKET_CRUMB, *trail, (name, None if tabs.is_overview else tabs.base_path)]


def jsonld_trail(trail: Sequence[tuple[str, str]], name: str, path: str) -> list[tuple[str, str]]:
    """The `BreadcrumbList` of the Overview (T-7: it stops at the record; tab pages carry none)."""
    return [MARKET_CRUMB, *trail, (name, path)]


def fact(
    label: str,
    value: str,
    *,
    term: str | None = None,
    value_term: str | None = None,
    datetime: str | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    """One key figure in the header: a label (linked to the glossary when it is a technical term) and a
    value (linked instead when the value is the term, as "Large load" is). A date keeps its ISO form in
    a `<time>` element, with a short note after it ("stated")."""
    return {
        "label": label,
        "value": value,
        "label_href": glossary_href(term),
        "value_href": glossary_href(value_term),
        "datetime": datetime,
        "note": note,
    }


def _ids_text(ids: Sequence[str]) -> str:
    shown = ", ".join(ids[:2])
    return shown + (f" and {len(ids) - 2} more" if len(ids) > 2 else "")


def proposal_facts(record: Mapping[str, Any], fields: Iterable[str]) -> list[dict[str, Any]]:
    """A project's key figures (docs/16 §4.2's header line): what kind of load, its capacity, the queue it
    waits in and its place there, and the in-service date its source states. A figure the record lacks is
    left out here (the Overview's field grid prints its dash)."""
    rows = set(fields)
    out: list[dict[str, Any]] = []
    if record.get("kind") == "load":
        out.append(fact("Kind", "Large load", value_term="large-load"))
    if "capacity_mw" in rows and record.get("capacity_mw") is not None:
        out.append(fact("Capacity", f"{mw(record['capacity_mw'])} MW", term="capacity"))
    queued = str(record.get("queue_id_label") or "").startswith("Queue")
    if queued and record.get("iso"):
        queue = str(record.get("iso_label") or record["iso"])
        out.append(fact("Interconnection queue", queue, term="interconnection-queue"))
    if queued and record.get("queue_ids"):
        out.append(fact("Queue position", _ids_text(list(record["queue_ids"])), term="queue-position"))
    if record.get("proposed_online_date"):
        date = str(record["proposed_online_date"])
        out.append(
            fact(
                "In-service date", display_date(date), term="in-service-date", datetime=date, note="(stated)"
            )
        )
    return out


def asset_facts(record: Mapping[str, Any]) -> list[dict[str, Any]]:
    """A plant's, pipeline's or line's key figures: capacity, voltage or length, the year it was
    commissioned, and where it is. "Commissioned", the Overview's own word, holds for a plant that has
    since retired; "in service since" would not."""
    out: list[dict[str, Any]] = []
    if record.get("capacity_mw") is not None and not record.get("is_fuel"):
        out.append(fact("Capacity", f"{mw(record['capacity_mw'])} MW", term="capacity"))
    if record.get("voltage"):
        out.append(fact("Voltage", str(record["voltage"])))
    if record.get("length_miles") is not None:
        out.append(fact("Mapped length", f"{float(record['length_miles']):,.0f} miles"))
    if record.get("commissioned_year") and not record.get("is_fuel"):
        out.append(fact("Commissioned", str(record["commissioned_year"])))
    state = str(record.get("state") or "").rsplit("-", 1)[-1] or None
    place = record.get("states") or ", ".join(str(p) for p in (record.get("county"), state) if p)
    if place:
        out.append(fact("Location", str(place)))
    return out


def point_facts(record: Mapping[str, Any]) -> list[dict[str, Any]]:
    """A grid connection point's key figures: the queue that names it, its voltage and the capacity
    waiting there (the API's own total at the reader's tier)."""
    out: list[dict[str, Any]] = []
    if record.get("iso_label"):
        out.append(fact("Interconnection queue", str(record["iso_label"]), term="interconnection-queue"))
    if record.get("voltage_label"):
        out.append(fact("Voltage", str(record["voltage_label"])))
    totals = record.get("totals") or {}
    active = int(totals.get("active_count") or 0)
    waiting = f"{record.get('active_mw_label')} MW, {active} active project{'s' if active != 1 else ''}"
    out.append(fact("Capacity queued", waiting, term="capacity"))
    return out


# ----------------------------------------------------------------------------- the project timeline
#: `web/viewmodels.py::proposal_history` words a merge as "Merged into this record: …". The Timeline tab
#: folds those rows into "Record maintenance" (docs/16 §4.2: our bookkeeping, not the project's history);
#: `web/test_record_tabs.py` pins the wording to this prefix.
MERGE_PREFIX = "Merged into this record"


def proposal_timeline(record: Mapping[str, Any], history: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """One dated list, newest first (docs/30 §11.5; docs/16 §4.2): the in-service date the source
    states (marked as stated, never a commitment), the status as last observed, and every recorded
    change. The merges that built the record sit apart as record maintenance. `changes` counts the
    recorded changes alone, for the Overview's "All N in Timeline"."""
    status = record.get("status_source") or {}
    cod = record.get("cod_source") or {}
    state = str(lifecycle_label(record.get("lifecycle_state")) or record.get("lifecycle_state") or "").lower()
    entries: list[dict[str, Any]] = []
    if record.get("proposed_online_date"):
        slip = record.get("slip") or {}
        text = "In-service date " + (
            f"stated by {cod['source_name']}" if cod.get("source_name") else "as stated"
        )
        text += " (not a commitment)."
        if slip:
            text += (
                f" The date passed {slip.get('amount')} ago and {status.get('source_name') or 'its source'}"
                f" still reports the project as {state}."
            )
        entries.append({"date": record["proposed_online_date"], "text": text, "stated": True})
    observed = f"Observed as {state}" + (f" in {status['source_name']}" if status.get("source_name") else "")
    if record.get("status_raw"):
        observed += f" (source status: “{record['status_raw']}”)"
    entries.append({"date": status.get("retrieved_at"), "text": observed + ".", "stated": False})
    changes: list[dict[str, Any]] = []
    maintenance: list[dict[str, Any]] = []
    for item in history:
        row = {"date": item.get("date"), "text": item.get("text"), "stated": False}
        (maintenance if str(item.get("text") or "").startswith(MERGE_PREFIX) else changes).append(row)
    entries.extend(changes)
    entries.sort(key=lambda e: str(e.get("date") or ""), reverse=True)
    return {"entries": entries, "changes": changes, "maintenance": maintenance}


def render(
    request: Request,
    template: str,
    context: dict[str, Any],
    tabs: Tabs,
) -> HTMLResponse:
    """A tab page: 200, or 404 when its tab is absent (T-3), with the tab set in the context."""
    context = {**context, "tabs": tabs}
    if tabs.absent:
        return templates.TemplateResponse(request, "record_tab_absent.html", context, status_code=404)
    return templates.TemplateResponse(request, template, context)


templates.env.globals["glossary_href"] = glossary_href
