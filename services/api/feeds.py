"""RSS 2.0 and JSON Feed 1.1 rendering (docs/23 §9.2; api/openapi.yaml `RssFeed`/`JsonFeed`).

Both are event feeds: one item per visible published event on a matching record, `pubDate` /
`date_published` = `public_at`, never earlier (US-503 AC1/AC3).

Attribution on every item (CLAUDE.md: every stored record carries `source_id`, `source_url`,
`retrieved_at`, `licence`, and attribution renders automatically; docs/23 §10). Each item's
`platform_ext["provenance"]` is the list of provenance quartets for the item's visible source rows
(`link_provenance` / `event_provenance` below build it; `services/api/serialize.py`
`provenance_quartet` is the shape every other surface uses). The JSON Feed carries it as
`_platform.provenance`; before 2026-09-27 that list was always empty. The RSS item carries it as:

  * one `<infraque:provenance>` element per source, in the namespace `PROVENANCE_NS`, with
    `source_id`, `source_name`, `source_url`, `retrieved_at`, `licence` (the licence id),
    `reuse_class` and `attribution` children. RSS 2.0 allows any element "defined in a namespace"
    (RSS 2.0 specification, "Extending RSS"). RSS's own `<source url="...">` is not used: it names
    "the RSS channel that the item came from" and its `url` must point at that channel's XML, which
    a queue spreadsheet or a docket page is not;
  * one Dublin Core `<dc:source>` per source URL (DCMI: "a related resource from which the
    described resource is derived"), so a generic reader that knows only `dc:` still shows where
    the item came from; `dc:creator` already carries the credit line;
  * the credit line appended to `<description>` (docs/23 §9.2: "description = derived fields + the
    credit line", US-503 AC3), and to the JSON Feed's `content_text`.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable
from typing import Any
from xml.sax.saxutils import escape

from services.api.common import WEB_HOST, iso
from services.api.serialize import provenance_quartet
from services.db.models import Event, OpportunitySource, ProposalSource
from services.ingest.lag import Kind

#: XML namespace of the per-item provenance element in RSS. A fixed identifier, not a URL anyone
#: needs to fetch (XML namespaces are names); versioned so a later shape change can take `/2`.
PROVENANCE_NS = "https://infraque.com/ns/feed-provenance/1"
_PROVENANCE_FIELDS = (
    ("source_id", "source_id"),
    ("source_name", "source_name"),
    ("source_url", "source_url"),
    ("retrieved_at", "retrieved_at"),
    ("licence", "licence_id"),
    ("reuse_class", "reuse_class"),
    ("attribution", "attribution_text"),
)


def link_provenance(links: Iterable[ProposalSource | OpportunitySource]) -> list[dict[str, Any]]:
    """The provenance quartet of each active source row of a proposal or opportunity."""
    return [
        provenance_quartet(
            link.source, link.source.licence, source_url=link.source_url, retrieved_at=link.retrieved_at
        )
        for link in links
        if link.active
    ]


def event_provenance(event: Event) -> list[dict[str, Any]]:
    """The event's own quartet, or nothing for a user-actor event (whose source and licence are
    null by design, docs/21 §3.10). An event row without its own `source_url` credits the source's
    landing page rather than printing no link."""
    if event.source is None or event.licence is None or event.retrieved_at is None:
        return []
    url = event.source_url or event.source.url
    return [provenance_quartet(event.source, event.licence, source_url=url, retrieved_at=event.retrieved_at)]


def credit_line(item: dict[str, Any]) -> str:
    """The item's credit line: each distinct attribution text of its sources, in order, or the
    `creator` the item already names when it has no provenance rows."""
    texts: list[str] = []
    for row in item.get("platform_ext", {}).get("provenance") or []:
        text = row.get("attribution_text") or row.get("source_name")
        if text and text not in texts:
            texts.append(str(text))
    return "; ".join(texts) or str(item.get("creator") or "")


def _described(item: dict[str, Any]) -> str:
    credit = credit_line(item)
    description = str(item["description"])
    return f"{description} — {credit}" if credit and credit not in description else description


def feed_title(resource: str, kind: Kind, *, live: bool = False) -> str:
    """`live=True` is this sprint's addition (Pro tier and alerts): a private saved-search feed
    (`services/alerts/feed.py`) is zero-lag for its owner, so the public "N days delayed" framing
    (US-604) would be actively wrong there — `docs/23` §9.2's `feedSavedSearch` calls it out as
    "items are live (`lag_days = 0`) for the owner's tier"."""
    if live:
        return f"{resource} — Live feed (Pro)"
    # Nothing is delayed any more: records since 2026-09-19 and change events since 2026-09-21,
    # when the ISO change-event delay went along with its per-source knob (`services/ingest/lag.py`).
    # US-604's disclosure rule is "say what is actually true of this feed", so the title says live.
    # What Pro adds to the same items is delivery -- alerts and webhooks -- not freshness.
    del kind  # nothing about the title depends on the subject's kind any more
    return f"{resource} — Public feed, live — alerts and API in Pro"


def render_rss(
    *, resource: str, kind: Kind, self_url: str, items: list[dict[str, Any]], live: bool = False
) -> str:
    title = feed_title(resource, kind, live=live)
    channel_link = f"{WEB_HOST}/{resource.lower()}"
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom" xmlns:dc="http://purl.org/dc/elements/1.1/"'
        f' xmlns:infraque="{PROVENANCE_NS}">',
        "<channel>",
        f"<title>{escape(title)}</title>",
        f"<link>{escape(channel_link)}</link>",
        f'<atom:link href="{escape(self_url)}" rel="self" type="application/rss+xml"/>',
        "<description>Energy and infrastructure change events, with provenance on every item.</description>",
    ]
    for it in items:
        parts.append("<item>")
        parts.append(f"<title>{escape(it['title'])}</title>")
        parts.append(f"<link>{escape(it['url'])}</link>")
        parts.append(f'<guid isPermaLink="false">{escape(it["guid"])}</guid>')
        parts.append(f"<pubDate>{_rfc822(it['pub_date'])}</pubDate>")
        parts.append(f"<dc:creator>{escape(it['creator'])}</dc:creator>")
        for cat in it.get("categories", []):
            parts.append(f"<category>{escape(cat)}</category>")
        parts.append(f"<description>{escape(_described(it))}</description>")
        for row in it.get("platform_ext", {}).get("provenance") or []:
            if row.get("source_url"):
                parts.append(f"<dc:source>{escape(str(row['source_url']))}</dc:source>")
            parts.append("<infraque:provenance>")
            for element, key in _PROVENANCE_FIELDS:
                if row.get(key) is not None:
                    parts.append(f"<infraque:{element}>{escape(str(row[key]))}</infraque:{element}>")
            parts.append("</infraque:provenance>")
        parts.append("</item>")
    parts.append("</channel>")
    parts.append("</rss>")
    return "".join(parts)


def _rfc822(value: dt.datetime) -> str:
    return value.astimezone(dt.UTC).strftime("%a, %d %b %Y %H:%M:%S GMT")


def render_json_feed(
    *, resource: str, kind: Kind, self_url: str, items: list[dict[str, Any]], live: bool = False
) -> dict[str, Any]:
    return {
        "version": "https://jsonfeed.org/version/1.1",
        "title": feed_title(resource, kind, live=live),
        "home_page_url": f"{WEB_HOST}/{resource.lower()}",
        "feed_url": self_url,
        "description": "Energy and infrastructure change events, with provenance on every item.",
        "items": [
            {
                "id": it["guid"],
                "url": it["url"],
                "title": it["title"],
                "content_text": _described(it),
                "date_published": iso(it["pub_date"]),
                "tags": it.get("categories", []),
                "_platform": it["platform_ext"],
            }
            for it in items
        ],
    }
