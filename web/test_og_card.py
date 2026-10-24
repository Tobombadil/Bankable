"""The Open Graph / Twitter card image (`web/og_card.py`, `web/static/img/og-card-2.png`), the favicon set
it also draws, and the meta that links the card (`web/templates/base.html`).

Held in place:
  - the committed PNG is 1200x630, under 150 KB, served by the site, and is what the script draws
    (so nobody can swap in an outside image or let the file drift from its source);
  - it is the brand system's navy card (docs/31-brand-identity.md §5.4): its text colours keep 4.5:1 on
    the navy, the orange is the dot over the i only, and copper is gone;
  - its lockup is the information-sign mark beside the outlined wordmark (§5.1): both dots round and
    centred on their stems, measured in the committed pixels;
  - its map draws only sources whose register entry is reuse-open and published, and credits them;
  - `favicon.svg`, `favicon.ico` and `apple-touch-icon.png` are the §5.1 mark (a circle, the i's stem and
    the orange dot on a 16-unit grid), drawn by the script;
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
    # docs/31 §1.1 (until 2026-10-23): copper on ink was 2.5:1, a banned pairing; the copper is gone.
    source = Path(og_card.__file__).read_text(encoding="utf-8").lower()
    assert "#a8571c" not in source and "#8a4614" not in source


def test_the_card_is_the_brand_systems_navy_card() -> None:
    """docs/31-brand-identity.md §5.4: Compass navy, near-white text, the evergreen tint for the projects,
    and Compass orange only as the dot over the i: never a text colour. Ratios on the navy as the spec
    states them."""
    assert og_card.INK == og_card.NAVY == "#0b1023"
    assert og_card.BRAND_DOT == "#e68023" and og_card.BRAND_DOT not in og_card.TEXT_COLOURS
    expected = {
        og_card.TEXT: 17.01,
        og_card.MARK_LIGHT: 16.66,
        og_card.MUTED: 9.17,
        og_card.DOT: 9.81,
        og_card.BRAND_DOT: 6.72,
    }
    for colour, ratio in expected.items():
        assert round(_contrast(colour, og_card.NAVY), 2) == ratio, colour
    css = (Path(og_card.__file__).resolve().parent / "static" / "css" / "styles.css").read_text(
        encoding="utf-8"
    )
    assert f"--brand-dot: {og_card.BRAND_DOT};" in css and f"--color-ink: {og_card.NAVY};" in css
    assert f"--color-accent-tint: {og_card.DOT};" in css
    # The dot keeps its exact orange through the 256-colour palette (median cut had merged it into a
    # duller orange); the evergreen tint lands within two levels a channel.
    with Image.open(og_card.OG_CARD_PATH) as card:
        colours = [rgb for _, rgb in card.convert("RGB").getcolors(1 << 16) or []]

    def rgb(hex_colour: str) -> tuple[int, ...]:
        return tuple(int(hex_colour[i : i + 2], 16) for i in (1, 3, 5))

    assert rgb(og_card.BRAND_DOT) in colours
    assert any(max(abs(a - b) for a, b in zip(c, rgb(og_card.DOT), strict=True)) <= 2 for c in colours)


ORANGE = (230, 128, 35)


def _near(pixel: tuple[int, ...], colour: tuple[int, ...], tolerance: int) -> bool:
    return max(abs(a - b) for a, b in zip(pixel[:3], colour, strict=True)) <= tolerance


def _blobs(
    image: Image.Image, box: tuple[int, int, int, int], colour: tuple[int, ...]
) -> list[list[tuple[int, int]]]:
    """The pixels of `colour` inside `box`, in clusters split where a column holds none, left to right."""
    x0, y0, x1, y1 = box
    columns: dict[int, list[tuple[int, int]]] = {}
    for x in range(x0, x1):
        for y in range(y0, y1):
            if _near(image.getpixel((x, y)), colour, 24):
                columns.setdefault(x, []).append((x, y))
    clusters: list[list[tuple[int, int]]] = []
    for x in sorted(columns):
        if clusters and x - clusters[-1][-1][0] <= 1:
            clusters[-1].extend(columns[x])
        else:
            clusters.append(list(columns[x]))
    return clusters


def _round_dot(points: list[tuple[int, int]]) -> tuple[float, int]:
    """Assert the cluster is a disc (as wide as tall, about pi/4 of its box filled; a triangle fills half)
    and return its centre x and its lowest row."""
    xs, ys = [x for x, _ in points], [y for _, y in points]
    width, height = max(xs) - min(xs) + 1, max(ys) - min(ys) + 1
    assert abs(width - height) <= 1, (width, height)
    assert 0.7 <= len(points) / (width * height) <= 0.86, len(points) / (width * height)
    return sum(xs) / len(xs), max(ys)


def _run_centre(image: Image.Image, y: int, near_x: float, colour: tuple[int, ...]) -> float:
    """The middle of the run of `colour` in row `y` that contains the column nearest `near_x`."""
    x = round(near_x)
    assert _near(image.getpixel((x, y)), colour, 40), (x, y, image.getpixel((x, y)))
    left = right = x
    while _near(image.getpixel((left - 1, y)), colour, 40):
        left -= 1
    while _near(image.getpixel((right + 1, y)), colour, 40):
        right += 1
    return (left + right) / 2


def test_the_cards_dots_are_round_and_centred_on_their_stems() -> None:
    """§5.1 and §5.4, in the committed pixels: the mark and the wordmark each carry one round orange dot
    (no triangle), centred on the stem below it to within a pixel: the mark's stem is knocked out of its
    light circle in near-black, the wordmark's is a light letter on the navy."""
    with Image.open(og_card.OG_CARD_PATH) as card:
        rgb = card.convert("RGB")
    mark_dot, word_dot = _blobs(rgb, (40, 30, 520, 200), ORANGE)
    for points, stem in ((mark_dot, (14, 15, 17)), (word_dot, (238, 241, 245))):
        centre, foot = _round_dot(points)
        assert abs(_run_centre(rgb, foot + 12, centre, stem) - centre) <= 0.75


def test_the_card_uses_only_the_self_hosted_fonts() -> None:
    source = Path(og_card.__file__).read_text(encoding="utf-8")
    names = set(re.findall(r'"([A-Za-z0-9-]+\.woff2)"', source))
    assert names, "the script names its fonts"
    assert all((og_card.FONTS / name).is_file() for name in names)
    assert names == {"geist-latin.woff2"}  # the brand system's one face (T-3)


# ------------------------------------------------------------------------------- the map
REGISTER = [
    {"id": "us.eia.860m", "reuse": "open", "publication": "raw_ok"},
    {"id": "us.iso.ercot.gen_queue", "reuse": "open", "publication": "raw_ok"},
    {"id": "us.iso.caiso.gen_queue", "reuse": "attribution", "publication": "derived_only"},
    {"id": "us.iso.spp.gen_queue", "reuse": "restricted", "publication": "none"},
    {"id": "us.iso.nyiso.gen_queue", "reuse": "open", "publication": "none"},
]


def test_only_open_published_sources_reach_the_card() -> None:
    """A card travels without the site's attribution strip, so it draws only sources whose terms ask no
    credit and that are published: no restricted, unknown, non-commercial or unpublished row."""
    assert og_card.drawable_sources(REGISTER) == {"us.eia.860m", "us.iso.ercot.gen_queue"}


def test_map_points_count_active_projects_per_county_from_drawable_sources_only() -> None:
    centres = {
        ("US-TX", "HARRIS"): (-95.4, 29.8),
        ("US-KS", "BARBER"): (-98.7, 37.2),
        ("US-AK", "NOME"): (-165.0, 64.5),
    }
    rows = [
        {"source_id": "ercot", "state": "TX", "county": "Harris County", "lifecycle_state": "studied"},
        {"source_id": "ercot", "state": "TX", "county": "Harris", "lifecycle_state": "contracted"},
        {"source_id": "eia860m", "state": "TX", "county": "HARRIS", "lifecycle_state": "under_construction"},
        {
            "source_id": "ercot",
            "state": "TX",
            "county": "Harris",
            "lifecycle_state": "withdrawn",
        },  # not active
        {
            "source_id": "spp",
            "state": "KS",
            "county": "Barber County",
            "lifecycle_state": "studied",
        },  # restricted
        {
            "source_id": "eia860m",
            "state": "AK",
            "county": "Nome",
            "lifecycle_state": "studied",
        },  # not the lower 48
        {"source_id": "eia860m", "state": "TX", "county": None, "lifecycle_state": "studied"},  # no county
        {
            "source_id": "eia860m",
            "state": "TX",
            "county": "Nowhere",
            "lifecycle_state": "studied",
        },  # no centre
    ]
    drawable = og_card.drawable_sources(REGISTER)
    assert og_card.map_points(rows, drawable, centres) == [[-95.4, 29.8, 3, "progress"]]
    assert og_card.county_key("TX", "Kit Carson County") == ("US-TX", "KIT CARSON")
    assert og_card.county_key("NY", "Wyoming/Livingston") == ("US-NY", "WYOMING")


def test_county_centres_are_the_largest_rings_area_centroid() -> None:
    square = [[0.0, 0.0], [2.0, 0.0], [2.0, 2.0], [0.0, 2.0], [0.0, 0.0]]
    islet = [[10.0, 10.0], [10.5, 10.0], [10.5, 10.5], [10.0, 10.5], [10.0, 10.0]]
    feature = {
        "properties": {"state_code": "US-TX", "name": "Harris"},
        "geometry": {"type": "MultiPolygon", "coordinates": [[islet], [square]]},
    }
    assert og_card.county_centres([feature]) == {("US-TX", "HARRIS"): (1.0, 1.0)}


def test_the_committed_map_data_names_its_sources_and_credits_them() -> None:
    import json

    data = json.loads(og_card.MAP_DATA_PATH.read_text(encoding="utf-8"))
    assert data["sources"] == ["us.eia.860m", "us.iso.ercot.gen_queue"]
    assert data["credit"] == ["EIA-860M", "ERCOT", "Census outlines"]
    families = {point[3] for point in data["points"]}
    assert families <= {"neutral", "progress", "committed"} and "progress" in families
    assert all(point[2] >= 1 for point in data["points"])
    assert all(-125 < point[0] < -66 and 24 < point[1] < 50 for point in data["points"])  # the lower 48


# ----------------------------------------------------------------------------------- the icons
def test_the_script_draws_the_committed_icons(tmp_path: Path) -> None:
    """favicon.ico and apple-touch-icon.png are `favicon.svg`'s mark (docs/31-brand-identity.md §5.1),
    drawn by the script, so neither can drift from it."""
    ico, touch = tmp_path / "favicon.ico", tmp_path / "apple-touch-icon.png"
    og_card.write_icons(ico, touch)
    for fresh_path, committed_path in ((ico, og_card.FAVICON_ICO_PATH), (touch, og_card.TOUCH_ICON_PATH)):
        with Image.open(fresh_path) as fresh, Image.open(committed_path) as committed:
            assert fresh.size == committed.size
            diff = ImageChops.difference(fresh.convert("RGBA"), committed.convert("RGBA"))
        assert max(ImageStat.Stat(diff).mean) < 2.0
    # The 180px home-screen icon: the mark inset a tenth on the light ground (9px a unit from 18px in): the
    # navy circle around (90, 90), radius 72; the white stem from x 77.4 to 102.6 and y 84.15 to 137.25;
    # the orange dot at (90, 58.95), radius 15.75.
    with Image.open(og_card.TOUCH_ICON_PATH) as icon:
        rgb = icon.convert("RGB")
    assert rgb.getpixel((2, 2)) == rgb.getpixel((177, 177)) == (238, 241, 245)  # the ledger ground
    assert rgb.getpixel((90, 59)) == ORANGE  # the dot
    assert rgb.getpixel((90, 110)) == (255, 255, 255)  # the stem
    assert rgb.getpixel((60, 110)) == rgb.getpixel((90, 79)) == (11, 16, 35)  # the circle, and dot to stem
    (dot,) = _blobs(rgb, (0, 0, 180, 180), ORANGE)
    centre, _ = _round_dot(dot)
    assert abs(centre - 89.5) <= 0.5  # pixel 89 is x 89-90: the grid's centre line, x 90
    assert abs(_run_centre(rgb, 110, centre, (255, 255, 255)) - centre) <= 0.5
    # The ICO's 48px frame: transparent outside the circle, the dot, the stem and the navy circle.
    with Image.open(og_card.FAVICON_ICO_PATH) as ico_image:
        frame = ico_image.convert("RGBA")
    assert frame.size == (48, 48) and frame.getpixel((1, 1))[3] == 0
    assert _near(frame.getpixel((24, 13)), ORANGE, 24)  # the dot
    assert _near(frame.getpixel((24, 31)), (255, 255, 255), 24)  # the stem
    assert _near(frame.getpixel((10, 24)), (11, 16, 35), 8)


def test_favicon_svg_is_the_information_sign_on_its_grid() -> None:
    """§5.1's geometry exactly, as the script writes it: circle radius 8; stem 2.8 wide from y 7.35 to 13.25,
    corner radius 0.5; the dot at (8, 4.55), radius 1.75. Light: navy circle, white stem; dark: `#eef1f5`
    circle, `#0e0f11` stem; the orange in both. No triangle, no path."""
    svg = (og_card.IMG / "favicon.svg").read_text(encoding="utf-8")
    assert svg == og_card.favicon_svg()
    assert '<circle class="disc" cx="8" cy="8" r="8"/>' in svg
    assert '<rect class="stem" x="6.6" y="7.35" width="2.8" height="5.9" rx="0.5"/>' in svg
    assert f'<circle fill="{og_card.BRAND_DOT}" cx="8" cy="4.55" r="1.75"/>' in svg
    light, dark = svg.split("@media (prefers-color-scheme: dark)", 1)
    assert ".disc { fill: #0b1023; }" in light and ".stem { fill: #ffffff; }" in light
    assert ".disc { fill: #eef1f5; }" in dark and ".stem { fill: #0e0f11; }" in dark
    assert "<path" not in svg and "<polygon" not in svg


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
    # Capacity through the `mw` formatter (docs/31 §4), not the raw float ("120.0 MW").
    assert og_description is not None and "120 MW" in og_description and "120.0" not in og_description


def test_the_card_url_the_page_names_is_served(web_client: TestClient) -> None:
    resp = web_client.get(f"/{og_card.OG_CARD_URL_PATH}")

    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"
    assert resp.content == og_card.OG_CARD_PATH.read_bytes()


def test_the_template_links_the_scripts_output_path() -> None:
    template = BASE_TEMPLATE.read_text(encoding="utf-8")
    assert f"'{og_card.OG_CARD_URL_PATH}'" in template
    assert og_card.OG_CARD_PATH.as_posix().endswith("web/" + og_card.OG_CARD_URL_PATH)
