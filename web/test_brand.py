"""The brand in the build (docs/31-brand-identity.md §4, §5.1, §7): tokens, the outlined wordmark and the
lockup, footer line, labels, shadows, buttons, links and map paint.

Before this lane the site drew in copper and Plex on a warm paper ground, labels were 12px mono capitals
with tracking, floating controls carried resting shadows and the wordmark was a plain word; the lane's first
cut set "ınframation" in live text under an orange triangle (a "needle"), which the owner's later choice
(§5.1: "infra" 700 and "mation" 400, outlined, with a round dot centred on the stem) replaced. Each test
names the §7 row it pins. The rendered checks run against a hand-written fake `Transport`; the fake is
duplicated rather than imported, per this repo's convention that no `web/test_*.py` imports another one.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections.abc import Iterator, Mapping
from html import unescape
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import ImageFont

from web import og_card
from web.api_client import ApiClient
from web.app import app as web_app

WEB = Path(__file__).resolve().parent
CSS = (WEB / "static" / "css" / "styles.css").read_text(encoding="utf-8")
TOP_BAR = (WEB / "templates" / "partials" / "_top_bar.html").read_text(encoding="utf-8")
WORDMARK_SVG = og_card.WORDMARK_PATH.read_text(encoding="utf-8")
MAP_JS = (WEB / "static" / "js" / "map.js").read_text(encoding="utf-8")
BASEMAP_JS = (WEB / "static" / "js" / "basemap.js").read_text(encoding="utf-8")
_RULES = re.findall(r"([^{}]+)\{([^{}]*)\}", re.sub(r"/\*.*?\*/", "", CSS, flags=re.S))


def _bodies(selector: str) -> list[str]:
    """Every declaration block whose selector list names `selector` exactly, in file order."""
    out = []
    for selectors, body in _RULES:
        if selector in [s.strip() for s in selectors.split(",")]:
            out.append(" ".join(body.split()))
    assert out, selector
    return out


def _tokens(selector: str) -> dict[str, str]:
    """The custom properties the first block for `selector` sets (the :root and dark token blocks)."""
    body = _bodies(selector)[0]
    return dict(re.findall(r"(--[\w-]+):\s*([^;]+);", body))


LIGHT = _tokens(":root")
DARK = _tokens(':root[data-theme="dark"]')
DARK_MEDIA = _tokens(':root:not([data-theme="light"])')


def _luminance(hex_colour: str) -> float:
    channels = [int(hex_colour.lstrip("#")[i : i + 2], 16) / 255 for i in (0, 2, 4)]
    linear = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast(a: str, b: str) -> float:
    hi, lo = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


# ---- T-1, T-2: §4's columns, exactly; the dark block twice and the same both times ----

#: docs/31-brand-identity.md §4, light and dark columns.
BRAND_TOKENS = {
    "--color-ink": ("#0b1023", "#f2f3f6"),
    "--color-paper": ("#eef1f5", "#0e0f11"),
    "--color-muted": ("#4a5063", "#b0b5bf"),
    "--bg": ("#eef1f5", "#0e0f11"),
    "--surface": ("#ffffff", "#17181b"),
    "--raised": ("#f5f7fa", "#1f2024"),
    "--active": ("#e8ebf1", "#26282d"),
    "--text": ("#0b1023", "#f2f3f6"),
    "--text-muted": ("#4a5063", "#b0b5bf"),
    "--border": ("#dde1e8", "#2a2c31"),
    "--rule": ("#eceff3", "#1d1e22"),
    "--control": ("#7d8496", "#7b808b"),
    "--primary": ("#0b1023", "#f2f3f6"),
    "--primary-ink": ("#ffffff", "#0e0f11"),
    "--secondary": ("#e8ebf1", "#26282d"),
    "--secondary-ink": ("#0b1023", "#f2f3f6"),
    "--color-accent": ("#1d7356", "#5fcf9f"),
    "--color-accent-deep": ("#16624a", "#5fcf9f"),
    "--color-accent-tint": ("#5fcf9f", "#5fcf9f"),
    "--brand-dot": ("#e68023", "#e68023"),
    "--wordmark-ink": ("#0b1023", "#eef1f5"),
    "--negative": ("#c4242a", "#f0857a"),
    "--map-land": ("#fbfcfd", "#1a1c20"),
    "--map-water": ("#dfe5ee", "#0b0c0e"),
    "--map-border": ("#c3cad6", "#32353c"),
    "--ctx": ("#5b6275", "#8b909b"),
}


def _resolve(tokens: Mapping[str, str], name: str, fallback: Mapping[str, str] = LIGHT) -> str:
    value = tokens.get(name, fallback.get(name, "")).strip()
    for _ in range(4):
        ref = re.fullmatch(r"var\((--[\w-]+)\)", value)
        if not ref:
            break
        value = tokens.get(ref.group(1), fallback.get(ref.group(1), "")).strip()
    return value.lower()


@pytest.mark.parametrize("name", sorted(BRAND_TOKENS))
def test_tokens_take_the_brand_table_values(name: str) -> None:
    light, dark = BRAND_TOKENS[name]
    assert _resolve(LIGHT, name) == light, name
    assert _resolve(DARK, name, LIGHT) == dark, name
    assert _resolve(DARK_MEDIA, name, LIGHT) == dark, name


def test_the_copper_names_stay_as_aliases_for_one_release() -> None:
    assert LIGHT["--color-copper"] == "var(--color-accent)"
    assert LIGHT["--color-copper-deep"] == "var(--color-accent-deep)"
    assert LIGHT["--color-copper-tint"] == "var(--color-accent-tint)"
    assert LIGHT["--shadow-1"] == "none"
    assert LIGHT["--shadow-float"] == "0 8px 24px rgba(11, 16, 35, 0.14)"


@pytest.mark.parametrize(("tokens", "theme"), [(LIGHT, "light"), (DARK, "dark")])
def test_text_tokens_meet_aa_on_both_grounds(tokens: Mapping[str, str], theme: str) -> None:
    """WCAG 2.2 SC 1.4.3 for text (4.5:1) and SC 1.4.11 for the control edge and focus ring (3:1)."""
    grounds = [_resolve(tokens, "--bg"), _resolve(tokens, "--surface")]
    for name in ("--text", "--text-muted", "--link", "--link-visited", "--accent-deep", "--negative"):
        for ground in grounds:
            assert _contrast(_resolve(tokens, name), ground) >= 4.5, (theme, name, ground)
    for name in ("--control", "--focus-ring"):
        for ground in grounds:
            assert _contrast(_resolve(tokens, name), ground) >= 3.0, (theme, name, ground)
    for fill, ink in (
        ("--primary", "--primary-ink"),
        ("--secondary", "--secondary-ink"),
        ("--accent", "--accent-ink"),
    ):
        assert _contrast(_resolve(tokens, fill), _resolve(tokens, ink)) >= 4.5, (theme, fill)


def test_the_dark_theme_is_a_true_neutral_not_navy() -> None:
    for name in ("--bg", "--surface", "--raised", "--active"):
        r, g, b = (int(_resolve(DARK, name)[i : i + 2], 16) for i in (1, 3, 5))
        assert max(r, g, b) - min(r, g, b) <= 7, name  # navy grounds were 20+ apart


def test_orange_is_the_dot_over_the_i_only() -> None:
    """§4, §5.1: Compass orange is the dot over the i, never text or data. One token, and only the wordmark's
    dot reads it; the drawn files take the same value from og_card.py."""
    assert CSS.lower().count("#e68023") == 1  # set once on :root; the dark blocks inherit it
    assert "--brand-dot" not in DARK and "--brand-dot" not in DARK_MEDIA
    uses = [sel.strip() for sel, body in _RULES if "var(--brand-dot)" in body]
    assert uses == [".wordmark__dot"], uses
    assert og_card.BRAND_DOT == LIGHT["--brand-dot"]


#: The files the wordmark, the mark and the card are drawn from or shipped in.
BRAND_FILES = (
    WEB / "templates" / "partials" / "_top_bar.html",
    WEB / "templates" / "base.html",
    WEB / "static" / "css" / "styles.css",
    WEB / "static" / "img" / "favicon.svg",
    og_card.WORDMARK_PATH,
    WEB / "og_card.py",
)


@pytest.mark.parametrize("path", BRAND_FILES, ids=lambda p: p.name)
def test_no_needle_pointer_or_triangle_is_left_in_the_brand(path: Path) -> None:
    """The owner's §5.1 (2026-10-23): a round dot; no needle, no pointer, no triangle anywhere. The map's LNG
    triangle (map.js, styles.css's palette comment) is a data shape, not the brand, and is not in these."""
    text = path.read_text(encoding="utf-8")
    # Case-blind for the words only: under re.I the dotless \u0131 would fold to every i in the file.
    assert not re.search(r"(?i:needle|north pointer|pointer over)|wordmark__i\b|&#x131;|\u0131", text), path
    if path.suffix == ".svg" or path.name == "_top_bar.html":  # base.html's danger glyph is a triangle (D-5)
        assert "<polygon" not in text and "triangle" not in text.lower(), path


def test_the_drawn_svgs_are_well_formed() -> None:
    """A `--` inside an XML comment makes a browser refuse the whole file (the favicon went blank once)."""
    for path in (og_card.WORDMARK_PATH, WEB / "static" / "img" / "favicon.svg"):
        ET.fromstring(path.read_text(encoding="utf-8"))  # noqa: S314 -- this repo's own committed files


# ---- T-3: Geist families with their metric-matched fallbacks ----


def test_font_tokens_name_geist_then_its_fallback() -> None:
    assert LIGHT["--font-display"].startswith('"Geist", "Geist Fallback"')
    assert LIGHT["--font-body"].startswith('"Geist", "Geist Fallback"')
    assert LIGHT["--font-mono"].startswith('"Geist Mono", "Geist Mono Fallback"')
    rules = " ".join(f"{sel}{{{body}}}" for sel, body in _RULES)
    assert "Plex" not in rules and "Newsreader" not in rules  # comments may say what was replaced


# ---- C-1: the page title at 28px ----


def test_h1_is_28px() -> None:
    assert LIGHT["--text-6"] == "1.75rem"
    assert "font-size: var(--text-6);" in _bodies("h1")[-1]


# ---- C-2, C-3: labels in the text face at 13px/500; mono only on identifiers ----

C2_LABELS = (
    ".stamp-label",
    ".provenance-panel__licence",
    ".filter-field label",
    ".filter-field__label",
    ".filter-field--placement legend",
    ".record-table--compact thead tr::before",
    ".map-panel__heading",
    ".filter-field--asset-types legend",
    ".in-view-list__group-name",
    ".pipeline-table caption",
    ".drawer-source__label",
    ".reuse-badge",
    ".field label",
    ".alert-card__filters dt",
    ".field--choices legend",
    # The map search's group names became each row's kind word (lane stacked-lists): the same label rule.
    ".map-search__kind",
    ".view-switch__label",
)


@pytest.mark.parametrize("selector", C2_LABELS)
def test_c2_labels_are_sentence_case_in_the_text_face(selector: str) -> None:
    bodies = _bodies(selector)
    joined = " ".join(bodies)
    assert "uppercase" not in joined and "small-caps" not in joined, selector
    assert not re.search(r"letter-spacing:\s*(?!0[;\s]|normal)", joined), selector
    assert "var(--font-mono)" not in joined, selector
    assert "font-family: var(--font-body); font-size: var(--text-label); font-weight: 500;" in bodies[0], (
        selector
    )
    assert LIGHT["--text-label"] == "0.8125rem"


@pytest.mark.parametrize("selector", [".section-num", ".timeline time", ".provenance-panel__name::before"])
def test_c3_numbers_and_dates_take_the_text_face_with_tabular_figures(selector: str) -> None:
    body = _bodies(selector)[0]
    assert "font-family: var(--font-body); font-variant-numeric: tabular-nums;" in body, selector


# ---- C-4: no shadow at rest; only floating layers take --shadow-float ----


@pytest.mark.parametrize(
    "selector",
    [".map-panel", ".map-search__input", ".map-search__option.is-active", ".sf-key", ".sf-key__substation"],
)
def test_c4_resting_surfaces_have_no_shadow(selector: str) -> None:
    for body in _bodies(selector):
        shadow = re.search(r"box-shadow:\s*([^;]+);", body)
        assert shadow is None or shadow.group(1) in ("none", "var(--shadow-1)"), (selector, body)
    assert ".map-overlay .map-controls button" not in [s.strip() for sel, _ in _RULES for s in sel.split(",")]


@pytest.mark.parametrize(
    "selector", [".map-drawer", ".map-search__list", ".map-search__empty", ".map-controls--regions.is-open"]
)
def test_c4_floating_layers_take_the_float_shadow(selector: str) -> None:
    assert any("box-shadow: var(--shadow-float);" in body for body in _bodies(selector)), selector


def test_c4_no_other_literal_shadow_at_rest() -> None:
    values = {v.strip() for _, body in _RULES for v in re.findall(r"box-shadow:\s*([^;]+);", body)}
    literal = {
        v
        for v in values
        if v not in ("none", "var(--shadow-1)", "var(--shadow-float)") and not v.startswith("inset")
    }
    assert literal == set(), literal


# ---- C-6, C-7: one filled button in ink, a grey secondary, ink links with a fine underline ----


def test_c6_primary_and_secondary_buttons() -> None:
    primary = _bodies(".button-link")[0]
    assert "border: 0; background: var(--primary); color: var(--primary-ink);" in primary
    assert "border-radius: var(--radius-2);" in primary and LIGHT["--radius-2"] == "6px"
    secondary = _bodies(".button-secondary")[0]
    assert "border: 0; background: var(--secondary); color: var(--secondary-ink);" in secondary
    fills = [
        sel.strip() for sel, body in _RULES if re.search(r"background:\s*var\(--(?:color-)?accent\)", body)
    ]
    assert not [s for s in fills if "button" in s], fills  # the accent never fills a button


def test_c7_links_are_ink_with_a_fine_offset_underline() -> None:
    assert (
        _bodies("a")[0]
        == "color: var(--link); text-decoration-thickness: 0.07em; text-underline-offset: 0.18em;"
    )
    assert _bodies("a:hover")[0] == "text-decoration-color: var(--accent);"
    assert _resolve(LIGHT, "--link") == "#0b1023" and _resolve(DARK, "--link", LIGHT) == "#f2f3f6"


# ---- M-1, M-2: markers by area, held on zoom; context faint and fine ----


def test_m1_markers_scale_by_area_and_hold_their_size_on_zoom() -> None:
    assert 'var dotColor = cssVar("--dot") || colors.progress;' in MAP_JS
    assert '["sqrt", ["+", 196, ["*", 1.66, ["get", "count"]]]]' in MAP_JS
    radius = re.findall(r'"circle-radius": \["interpolate", \["linear"\], \["zoom"\]', MAP_JS)
    assert len(radius) >= 2, "cluster and precision radii interpolate by zoom"
    assert '"icon-size": ["interpolate", ["linear"], ["zoom"], 4, 0.85, 8, 1, 12, 1.2]' in MAP_JS


def test_m2_context_layers_draw_faint_and_fine_under_the_projects() -> None:
    assert 'var CTX_OPACITY = mapColors.flavor === "dark" ? 0.32 : 0.24;' in MAP_JS
    assert '["interpolate", ["linear"], ["zoom"], 3, 0.75, 8, 1, 11, 1.5]' in MAP_JS
    assert MAP_JS.count("CTX_OPACITY") >= 5
    assert "#fbfcfd" in BASEMAP_JS and "#dfe5ee" in BASEMAP_JS and "#c3cad6" in BASEMAP_JS


#: The context layers' words: cluster counts, pipeline and line names, plant and retired-plant names.
CONTEXT_LABEL_LAYERS = (
    "plant-cluster-count",
    "asset-line-labels",
    "plant-labels",
    "retired-cluster-count",
    "retired-labels",
)


def _label_opacity(layer: str, flavor: str) -> float:
    """The `text-opacity` map.js ships for `layer` under the light or dark basemap (1 when unset)."""
    block = MAP_JS.split(f'id: "{layer}"', 1)[1].split('}, "clusters");', 1)[0]
    paint = block.split("paint:", 1)[1]
    found = re.search(r'"text-opacity":\s*([\w.]+)', paint)
    if found is None:
        return 1.0
    value = found.group(1)
    if re.fullmatch(r"[\d.]+", value):
        return float(value)
    declared = re.search(rf"var {value} = ([^;]+);", MAP_JS)
    assert declared is not None, value
    expr = declared.group(1).strip()
    ternary = re.fullmatch(r'mapColors\.flavor === "dark" \? ([\d.]+) : ([\d.]+)', expr)
    if ternary:
        return float(ternary.group(1) if flavor == "dark" else ternary.group(2))
    return float(expr)


def _blend(colour: str, ground: str, alpha: float) -> str:
    """`colour` at `alpha` over `ground`, as the map composites a translucent label on the land."""
    c = [int(colour.lstrip("#")[i : i + 2], 16) for i in (0, 2, 4)]
    g = [int(ground.lstrip("#")[i : i + 2], 16) for i in (0, 2, 4)]
    return "#" + "".join(f"{round(alpha * a + (1 - alpha) * b):02x}" for a, b in zip(c, g, strict=True))


@pytest.mark.parametrize("layer", CONTEXT_LABEL_LAYERS)
@pytest.mark.parametrize(("tokens", "flavor"), [(LIGHT, "light"), (DARK, "dark")])
def test_m2_context_labels_read_at_aa_on_the_land(tokens: Mapping[str, str], flavor: str, layer: str) -> None:
    """M-2 asks for faint marks and lines, not faint words: WCAG 2.2 SC 1.4.3 outranks the spec. Every plant
    and asset colour a context label is drawn in, at the opacity map.js ships, keeps 4.5:1 on --map-land.
    (At the lane's first 0.55 / 0.6 the twenty colours measured 2.18-2.86:1 on light land, 3.16-4.40:1 on
    dark.)"""
    land = _resolve(tokens, "--map-land", LIGHT)
    names = sorted(n for n in LIGHT if n.startswith(("--plant-", "--asset-")))
    assert len(names) == 20  # 11 plant technologies, 6 midstream types, transmission, retired, retiring
    alpha = _label_opacity(layer, flavor)
    for name in names:
        shown = _blend(_resolve(tokens, name, LIGHT), land, alpha)
        assert _contrast(shown, land) >= 4.5, (flavor, layer, name, alpha, round(_contrast(shown, land), 2))


# ---- B-2, B-4: the lockup and the footer line, rendered ----


class FakeTransport:
    """Canned responses keyed by path; anything else is a 404, which the page renders around."""

    def __init__(self, responses: Mapping[str, Any]) -> None:
        self.responses = dict(responses)

    def _respond(self, url: str) -> httpx.Response:
        status, body = self.responses.get(url, (404, {"title": "not_found", "detail": url}))
        return httpx.Response(status, json=body)

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


_CACHES = ("api_client", "lag_days_default", "sitemap_cache", "asset_type_counts_cache")


@pytest.fixture()
def page() -> Iterator[str]:
    for key in _CACHES:
        web_app.state.__dict__.pop(key, None)
    web_app.state.api_client = ApiClient(
        FakeTransport(
            {
                "/v1/health": (200, {"status": "ok", "data_as_of": "2026-10-01"}),
                "/v1/meta/vocabularies": (200, {"data": {}}),
                "/v1/sources": (200, {"data": []}),
            }
        )
    )
    web_app.state.lag_days_default = None
    with TestClient(web_app) as client:
        response = client.get("/privacy")
    assert response.status_code == 200
    yield response.text
    for key in _CACHES:
        web_app.state.__dict__.pop(key, None)


def test_b2_the_lockup_is_the_outlined_wordmark_and_one_accessible_name(page: str) -> None:
    """§5.1: the wordmark ships as outlined paths, not live text, so the dot cannot drift with font loading;
    the endorsement follows it; the link's one name is "Inframation, a Compass company"."""
    lockup = page.split('<a class="wordmark" href="/">', 1)[1].split("</a>", 1)[0]
    word = og_card.read_wordmark(WORDMARK_SVG)
    assert lockup.startswith(og_card.inline_wordmark(word))  # the committed file's paths, exactly
    assert lockup.count("<svg") == 1 and lockup.count("<circle") == 1
    drawn, rest = lockup.split("</svg>", 1)
    assert re.sub(r"<[^>]+>", "", drawn) == ""  # no live text in the wordmark
    assert 'aria-hidden="true" focusable="false"' in drawn
    assert rest == (
        '<span class="wordmark__endorse" aria-hidden="true">a Compass company</span>'
        '<span class="visually-hidden">Inframation, a Compass company</span>'
    )
    assert "<img" not in lockup  # text endorsement: no Compass logo is drawn or traced


def test_the_wordmark_is_infra_bold_and_mation_regular() -> None:
    word = og_card.read_wordmark(WORDMARK_SVG)
    assert [g.name for g in word.glyphs] == ["dotlessi", "n", "f", "r", "a", "m", "a", "t", "i", "o", "n"]
    assert [g.weight for g in word.glyphs] == [700] * 5 + [400] * 6


#: Each glyph name the wordmark uses, as the character Pillow's FreeType measures.
_CHARS = {"dotlessi": "ı", "n": "n", "f": "f", "r": "r", "a": "a", "m": "m", "t": "t", "i": "i", "o": "o"}


def test_the_outlines_are_geists_own_at_their_weights() -> None:
    """Each outline's ink box is Geist's for that glyph at that weight, as FreeType measures the shipped
    variable file at 1000px (a unit a pixel; FreeType's box rounds outward, hence 2 units). The bold and
    regular cuts of these letters differ by far more, so a glyph cut at the wrong weight fails."""
    fonts: dict[int, ImageFont.FreeTypeFont] = {}
    for weight in (400, 700):
        font = ImageFont.truetype(
            str(og_card.FONTS / og_card.GEIST), 1000, layout_engine=ImageFont.Layout.BASIC
        )
        font.set_variation_by_axes([weight])
        fonts[weight] = font

    def ink(weight: int, name: str) -> tuple[int, int, int, int]:
        """The rendered glyph's ink box, baseline at 0 (`getbbox` spans the advance, not the ink)."""
        mask, (ox, oy) = fonts[weight].getmask2(_CHARS[name], anchor="ls")
        box = mask.getbbox()
        assert box is not None, name
        return ox + box[0], oy + box[1], ox + box[2], oy + box[3]

    word = og_card.read_wordmark(WORDMARK_SVG)
    for glyph in word.glyphs:
        left, top, right, bottom = og_card.path_bounds(glyph.d)
        x0, y0, x1, y1 = ink(glyph.weight, glyph.name)
        assert abs((right - left) - (x1 - x0)) <= 2, glyph.name
        assert abs(top - y0) <= 2 and abs(bottom - y1) <= 2, glyph.name
        if glyph.name in ("dotlessi", "n", "m", "a", "o"):
            w0, _, w1, _ = ink(1100 - glyph.weight, glyph.name)
            assert abs((right - left) - (w1 - w0)) > 10, glyph.name  # the other weight would not pass


def test_the_dot_is_round_and_centred_on_the_measured_stem() -> None:
    """§5.1: the dot is centred on the bold stem as measured from the outlined glyph's bounds, 1.25 times
    the stem's width, its foot 0.09em above the stem; the stem is the outline's straight box."""
    word = og_card.read_wordmark(WORDMARK_SVG)
    left, top, right, bottom = word.stem
    assert right - left == pytest.approx(150, abs=1) and top == pytest.approx(-536, abs=1) and bottom == 0
    cx, cy, r = word.dot
    assert abs(cx - (left + right) / 2) <= 0.01  # font units: a hundred-thousandth of an em
    assert 2 * r / (right - left) == pytest.approx(og_card.DOT_TO_STEM, abs=0.001)
    assert og_card.DOT_TO_STEM == 1.25
    assert top - (cy + r) == pytest.approx(og_card.DOT_GAP, abs=0.01)
    assert og_card.DOT_GAP == 90


def test_the_standalone_wordmark_is_navy_then_light_on_dark() -> None:
    assert "<title>Inframation</title>" in WORDMARK_SVG
    light, dark = WORDMARK_SVG.split("@media (prefers-color-scheme: dark)", 1)
    assert f".ink {{ fill: {og_card.NAVY}; }}" in light and f".ink {{ fill: {og_card.MARK_LIGHT}; }}" in dark
    assert f'<circle fill="{og_card.BRAND_DOT}"' in WORDMARK_SVG
    assert og_card.wordmark_svg(og_card.read_wordmark(WORDMARK_SVG)) == WORDMARK_SVG


def test_the_dot_sits_on_the_stem_in_a_browser(page: str) -> None:
    """The top bar in Chromium, light and dark: the dot's centre is the stem's to a hundredth of a pixel and
    1.25 times its width; the letters are navy (`#eef1f5` on dark) and the dot Compass orange; the link's
    accessible name is the lockup's. The page and its stylesheet are served from this process (no port)."""
    sync_api = pytest.importorskip("playwright.sync_api")
    measure = """() => { const w = document.querySelector('.top-bar .wordmark');
      const stem = w.querySelector('path[data-glyph=dotlessi]').getBoundingClientRect();
      const dot = w.querySelector('.wordmark__dot').getBoundingClientRect();
      return {offset: (dot.left + dot.width / 2) - (stem.left + stem.width / 2),
              ratio: dot.width / stem.width,
              letters: getComputedStyle(w.querySelector('svg g')).fill,
              dot: getComputedStyle(w.querySelector('.wordmark__dot')).fill}; }"""

    def serve(route: Any) -> None:
        url = route.request.url
        if url.endswith("/privacy"):
            route.fulfill(status=200, content_type="text/html", body=page)
        elif "/static/css/styles.css" in url:
            route.fulfill(status=200, content_type="text/css", body=CSS)
        else:
            route.fulfill(status=404, body="")

    # The sandbox's preinstalled Chromium where it exists, else Playwright's own (CI), as test_e2e.py does.
    chromium = "/opt/pw-browsers/chromium"
    launch = {"executable_path": chromium} if Path(chromium).exists() else {}
    with sync_api.sync_playwright() as p:
        browser = p.chromium.launch(**launch)
        try:
            for scheme, letters in (("light", "rgb(11, 16, 35)"), ("dark", "rgb(238, 241, 245)")):
                tab = browser.new_page(viewport={"width": 1440, "height": 900}, color_scheme=scheme)
                tab.route("**/*", serve)
                tab.goto("http://brand.test/privacy")
                got = tab.evaluate(measure)
                assert abs(got["offset"]) < 0.01, (scheme, got)
                assert got["ratio"] == pytest.approx(1.25, abs=0.01), (scheme, got)
                assert (got["letters"], got["dot"]) == (letters, "rgb(230, 128, 35)"), (scheme, got)
                name = "Inframation, a Compass company"
                assert tab.get_by_role("link", name=name, exact=True).count() == 1, scheme
                tab.close()
        finally:
            browser.close()


def test_b4_the_footer_opens_with_the_endorsement(page: str) -> None:
    footer = page.split('<footer class="site-footer">', 1)[1]
    first = re.search(r"<p[^>]*>(.*?)</p>", footer, re.S)
    assert first is not None
    assert " ".join(unescape(first.group(1)).split()) == (
        "Inframation is a Compass company. Compass International Trading Group, LLC, Denver, Colorado."
    )
