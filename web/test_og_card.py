"""The Open Graph / Twitter card image (`web/og_card.py`, `web/static/img/og-card.png`) and the
meta that links it (`web/templates/base.html`).

Held in place:
  - the committed PNG is 1200x630, under 150 KB, served by the site, and is what the script draws
    (so nobody can swap in an outside image or let the file drift from its source);
  - its text colours keep 4.5:1 on ink and copper is never drawn on ink (docs/31 D-20);
  - every page links it with an absolute URL under the request's base URL, the one the canonical
    link uses, so no domain is written into the page, with width, height, alt and
    `twitter:card=summary_large_image`;
  - per-page `og:title` / `og:description` are the page's own `<title>` and meta description, on a
    record page and on pages rendered by each of the other Jinja environments.
"""

from __future__ import annotations

import html
import re
import struct
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageChops, ImageStat

from web import og_card
from web.api_client import ApiClient
from web.app import app as web_app

BASE_TEMPLATE = Path(og_card.__file__).resolve().parent / "templates" / "base.html"


def _png_size(data: bytes) -> tuple[int, int]:
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    assert data[12:16] == b"IHDR"
    width, height = struct.unpack(">II", data[16:24])
    return width, height


def _contrast(a: str, b: str) -> float:
    def luminance(hex_colour: str) -> float:
        channels = [int(hex_colour[i : i + 2], 16) / 255 for i in (1, 3, 5)]
        linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
        return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

    high, low = sorted((luminance(a), luminance(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


# ------------------------------------------------------------------------------- the image
def test_the_committed_card_is_a_1200_by_630_png_under_the_budget() -> None:
    data = og_card.OG_CARD_PATH.read_bytes()

    assert _png_size(data) == (og_card.WIDTH, og_card.HEIGHT) == (1200, 630)
    assert len(data) <= og_card.MAX_BYTES == 150_000


def test_the_script_draws_the_committed_card(tmp_path: Path) -> None:
    """Regenerated here and compared by pixels, not bytes: a different Pillow or FreeType moves
    anti-aliasing a little, while a hand-edited or stale file differs by far more."""
    out = tmp_path / "card.png"

    assert og_card.main(["--out", str(out)]) == 0
    assert _png_size(out.read_bytes()) == (1200, 630)
    with Image.open(out) as fresh, Image.open(og_card.OG_CARD_PATH) as committed:
        diff = ImageChops.difference(fresh.convert("RGB"), committed.convert("RGB"))
    assert max(ImageStat.Stat(diff).mean) < 2.0


def test_every_text_colour_reads_on_ink_and_copper_is_not_used() -> None:
    for colour in og_card.TEXT_COLOURS:
        assert _contrast(colour, og_card.INK) >= 4.5, colour
    # docs/31 §1.1: copper on ink is 2.5:1, a banned pairing; the accent on ink is copper-tint.
    source = Path(og_card.__file__).read_text(encoding="utf-8").lower()
    assert "#a8571c" not in source and "#8a4614" not in source


def test_the_card_uses_only_the_self_hosted_fonts() -> None:
    source = Path(og_card.__file__).read_text(encoding="utf-8")
    names = set(re.findall(r'"([A-Za-z0-9-]+\.woff2)"', source))
    assert names, "the script names its fonts"
    assert all((og_card.FONTS / name).is_file() for name in names)
    assert all(name.startswith("IBMPlex") for name in names)


# --------------------------------------------------------------------------- the meta tags
DEFAULT_HEALTH: dict[str, Any] = {
    "status": "ok",
    "data_as_of": "2026-09-01",
    "live_as_of": "2026-09-15T00:00:00Z",
    "lag_days_default": {"supply": 0, "opportunities": 0},
}
DEFAULT_VOCAB: dict[str, Any] = {
    "data": {
        "technology": [{"value": "solar"}],
        "proposal_kind": [{"value": "generation"}],
        "opportunity_kind": [{"value": "rfp"}],
        "opportunity_status": [{"value": "open"}],
    }
}
PROPOSAL: dict[str, Any] = {
    "public_id": "PROP_A",
    "slug": "prop-a",
    "name_canonical": 'Acme "Big" Solar',
    "kind": "generation",
    "technology": "solar",
    "capacity_mw": 120.0,
    "jurisdiction": "US-TX",
    "lifecycle_state": "under_construction",
    "status_raw": "Under Construction",
    "identifiers": {},
    "proposed_online_date": None,
    "schedule_slip": None,
    "source_count": 1,
    "provenance": [
        {
            "source_id": "us.iso.ercot.gen_queue",
            "source_name": "ERCOT Queue",
            "source_url": "https://example.org/q",
            "retrieved_at": "2026-09-13T00:00:00Z",
            "reuse_class": "open",
            "attribution_text": "ERCOT",
            "source_record_id": "23INR0001",
        }
    ],
}
LIST_ENV: dict[str, Any] = {
    "data": [PROPOSAL],
    "meta": {"total": 1, "total_is_estimate": False},
    "page": {"next_cursor": None, "prev_cursor": None, "has_more": False},
    "licence_summary": [],
}


class FakeTransport:
    def __init__(self, responses: Mapping[str, tuple[int, Any]]) -> None:
        self.responses = dict(responses)

    def _respond(self, url: str) -> httpx.Response:
        if url in self.responses:
            status, body = self.responses[url]
            return httpx.Response(status, json=body)
        return httpx.Response(404, json={"title": "not_found", "detail": url})

    def get(
        self, url: str, *, params: Mapping[str, Any] | None = None, cookies: dict[str, str] | None = None
    ) -> httpx.Response:
        return self._respond(url)

    def post(
        self, url: str, *, json: Mapping[str, Any] | None = None, cookies: dict[str, str] | None = None
    ) -> httpx.Response:
        return self._respond(url)

    def request(
        self,
        method: str,
        url: str,
        *,
        json: Mapping[str, Any] | None = None,
        params: Mapping[str, Any] | None = None,
        cookies: dict[str, str] | None = None,
    ) -> httpx.Response:
        return self._respond(url)

    def close(self) -> None:
        pass


#: A base URL that is not TestClient's default, so a hard-coded host could not pass.
PUBLIC_BASE = "https://register.example.net"


@pytest.fixture()
def web_client() -> Iterator[TestClient]:
    with TestClient(web_app, base_url=PUBLIC_BASE) as client:
        web_app.state.api_client = ApiClient(
            FakeTransport(
                {
                    "/v1/health": (200, DEFAULT_HEALTH),
                    "/v1/meta/vocabularies": (200, DEFAULT_VOCAB),
                    "/v1/sources": (200, {"data": []}),
                    "/v1/proposals": (200, LIST_ENV),
                    "/v1/proposals/geo": (200, {"data": {"totals": {"lifecycle_state_counts": {}}}}),
                }
            )
        )
        web_app.state.lag_days_default = None
        yield client
    for key in ("api_client", "lag_days_default", "sitemap_cache", "asset_type_counts_cache"):
        web_app.state.__dict__.pop(key, None)


def _meta(body: str, attr: str, value: str) -> str | None:
    match = re.search(rf'<meta {attr}="{re.escape(value)}" content="([^"]*)">', body)
    return html.unescape(match.group(1)) if match else None


#: A record page, and one page from each Jinja environment that renders `base.html` (web/app.py,
#: web/legal.py, web/pricing.py, web/auth.py).
PAGES = ("/proposals/prop-a", "/proposals", "/privacy", "/pricing", "/login")


@pytest.mark.parametrize("path", PAGES)
def test_every_page_links_the_card_under_its_own_base_url(web_client: TestClient, path: str) -> None:
    resp = web_client.get(path)

    assert resp.status_code == 200
    body = resp.text
    image = f"{PUBLIC_BASE}/{og_card.OG_CARD_URL_PATH}"
    assert _meta(body, "property", "og:image") == image
    assert _meta(body, "name", "twitter:image") == image
    assert _meta(body, "property", "og:image:type") == "image/png"
    assert _meta(body, "property", "og:image:width") == str(og_card.WIDTH)
    assert _meta(body, "property", "og:image:height") == str(og_card.HEIGHT)
    assert _meta(body, "property", "og:image:alt") == og_card.OG_CARD_ALT
    assert _meta(body, "name", "twitter:image:alt") == og_card.OG_CARD_ALT
    assert _meta(body, "name", "twitter:card") == "summary_large_image"
    # Same base as the canonical link.
    canonical = re.search(r'<link rel="canonical" href="([^"]+)">', body)
    assert canonical is not None and canonical.group(1).startswith(PUBLIC_BASE + "/")


@pytest.mark.parametrize("path", PAGES)
def test_og_title_and_description_are_the_pages_own(web_client: TestClient, path: str) -> None:
    body = web_client.get(path).text

    title = re.search(r"<title>(.*?)</title>", body, re.S)
    assert title is not None
    page_title = html.unescape(title.group(1))
    description = _meta(body, "name", "description")
    assert description
    assert _meta(body, "property", "og:title") == page_title
    assert _meta(body, "name", "twitter:title") == page_title
    assert _meta(body, "property", "og:description") == description
    assert _meta(body, "name", "twitter:description") == description


def test_a_record_page_titles_its_card_with_the_record(web_client: TestClient) -> None:
    body = web_client.get("/proposals/prop-a").text

    og_title = _meta(body, "property", "og:title")
    assert og_title is not None and og_title.startswith('Acme "Big" Solar')
    og_description = _meta(body, "property", "og:description")
    assert og_description is not None and "120.0 MW" in og_description


def test_the_card_url_the_page_names_is_served(web_client: TestClient) -> None:
    resp = web_client.get(f"/{og_card.OG_CARD_URL_PATH}")

    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"
    assert resp.content == og_card.OG_CARD_PATH.read_bytes()


def test_the_template_links_the_scripts_output_path() -> None:
    template = BASE_TEMPLATE.read_text(encoding="utf-8")
    assert f"'{og_card.OG_CARD_URL_PATH}'" in template
    assert og_card.OG_CARD_PATH.as_posix().endswith("web/" + og_card.OG_CARD_URL_PATH)
