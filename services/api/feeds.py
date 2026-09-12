"""RSS 2.0 and JSON Feed 1.1 rendering (docs/23 §9.2; api/openapi.yaml `RssFeed`/`JsonFeed`).

Both are event feeds: one item per visible published event on a matching record, `pubDate` /
`date_published` = `public_at`, never earlier (US-503 AC1/AC3).
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from xml.sax.saxutils import escape

from services.api.common import WEB_HOST, iso
from services.ingest.lag import LAG_DAYS_BY_KIND, Kind


def feed_title(resource: str, kind: Kind) -> str:
    lag = LAG_DAYS_BY_KIND[kind]
    return f"{resource} — Public feed, {lag} days delayed — live in Pro"


def render_rss(*, resource: str, kind: Kind, self_url: str, items: list[dict[str, Any]]) -> str:
    title = feed_title(resource, kind)
    channel_link = f"{WEB_HOST}/{resource.lower()}"
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom" xmlns:dc="http://purl.org/dc/elements/1.1/">',
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
        parts.append(f"<description>{escape(it['description'])}</description>")
        parts.append("</item>")
    parts.append("</channel>")
    parts.append("</rss>")
    return "".join(parts)


def _rfc822(value: dt.datetime) -> str:
    return value.astimezone(dt.UTC).strftime("%a, %d %b %Y %H:%M:%S GMT")


def render_json_feed(
    *, resource: str, kind: Kind, self_url: str, items: list[dict[str, Any]]
) -> dict[str, Any]:
    return {
        "version": "https://jsonfeed.org/version/1.1",
        "title": feed_title(resource, kind),
        "home_page_url": f"{WEB_HOST}/{resource.lower()}",
        "feed_url": self_url,
        "description": "Energy and infrastructure change events, with provenance on every item.",
        "items": [
            {
                "id": it["guid"],
                "url": it["url"],
                "title": it["title"],
                "content_text": it["description"],
                "date_published": iso(it["pub_date"]),
                "tags": it.get("categories", []),
                "_platform": it["platform_ext"],
            }
            for it in items
        ],
    }
