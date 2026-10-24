"""Builds the brand's drawn files: the outlined wordmark, the mark (favicon set), and the share card.

Run `python -m web.og_card` after changing anything below and commit what it writes: the share card
(Open Graph / Twitter, `web/static/img/og-card-2.png`); with `--icons` the two raster icons (`favicon.ico`,
`apple-touch-icon.png`) that draw `favicon.svg`'s mark; with `--wordmark` the outlined wordmark
(`web/static/img/wordmark.svg`), which also prints the inline copy the top bar carries. The files are
self-made: the brand system of docs/31-brand-identity.md (§5.1 the wordmark and the mark, §5.4 the card), the
self-hosted Geist file in `web/static/fonts/` (read as shipped; Pillow's FreeType opens WOFF2 and sets its
weight axis), the Census state outlines in `data/vendored/regions/`, and the county-level project points in
`web/og_card_map.json`. No third-party imagery, map tiles or other organisations' marks; the Compass logo is
never drawn. Nothing at runtime imports this module; `base.html` and the top bar carry the committed files.

The wordmark (§5.1) is "infra" in Geist 700 and "mation" in Geist 400, one word at -0.03em tracking, its
first i dotless and carrying a round orange dot 1.25 times the bold stem's width, centred on the stem as
measured from the outlined glyph's bounds. It ships as outlined paths, so the dot cannot drift with font
loading. `build_wordmark` makes those paths from the Geist file (it needs fontTools with brotli,
skia-pathops and uharfbuzz, which the site does not: a person runs it); `read_wordmark` reads them back,
and the card, the tests and the top bar all take the one committed `wordmark.svg`.

The mark (§5.1) is the information sign on a 16-unit grid: a circle of radius 8, the i's stem 2.8 wide
from y 7.35 to 13.25 with corner radius 0.5, and the orange dot at (8, 4.55), radius 1.75. On light the
circle is navy and the stem white; on dark the circle is `#eef1f5` and the stem `#0e0f11`.

The card is a navy field (a read surface, where Compass's navy may be a ground): the wordmark alone (the mark
is never beside it), the endorsement, one line saying what the site is, where its records come from, and the
national map of planned projects in the evergreen tint over faint state lines. It carries no live figure,
because a cached card would show a stale number; the page's description carries them. Every text colour is at least
4.5:1 on the navy (`web/test_og_card.py` recomputes it); the orange is the dot over the i only.

The map's points (`--rebuild-map`) are the located active proposals of the committed fixture
`data/eval/normalized.parquet`, counted per county centre (Census outlines), from sources whose register
entry (`data/sources.yaml`) is `reuse: open` and published (`raw_ok` or `derived_only`) only: a card is
copied off the site without its attribution strip, so it draws only sources whose terms ask no credit, and
it names them on the card anyway. No restricted, unknown or non-commercial row reaches it.

A card that changes in a way a cached copy must not keep showing gets a new file name (platforms cache by
URL), as the fonts directory does (`web/static/fonts/README.md`); this is the second card.
"""

from __future__ import annotations

import argparse
import html
import importlib
import io
import json
import math
import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops, ImageDraw, ImageFont

WEB_ROOT = Path(__file__).resolve().parent
REPO_ROOT = WEB_ROOT.parent
FONTS = WEB_ROOT / "static" / "fonts"
IMG = WEB_ROOT / "static" / "img"
#: Where the card is written, and the path `base.html` links under the site's base URL.
OG_CARD_PATH = IMG / "og-card-2.png"
OG_CARD_URL_PATH = "static/img/og-card-2.png"
FAVICON_ICO_PATH = IMG / "favicon.ico"
TOUCH_ICON_PATH = IMG / "apple-touch-icon.png"
#: The outlined wordmark: the one source of its paths for the top bar, the card and the tests.
WORDMARK_PATH = IMG / "wordmark.svg"
MAP_DATA_PATH = WEB_ROOT / "og_card_map.json"
REGIONS = REPO_ROOT / "data" / "vendored" / "regions"
FIXTURE = REPO_ROOT / "data" / "eval" / "normalized.parquet"
REGISTER = REPO_ROOT / "data" / "sources.yaml"
WIDTH, HEIGHT = 1200, 630
#: Mirrors `og:image:alt` / `twitter:image:alt` in `base.html` (pinned by `web/test_og_card.py`).
OG_CARD_ALT = (
    "Inframation, a Compass company: planned US energy projects, each linked to its source, "
    "drawn at their counties on a map of the United States."
)
#: Budget for the committed file. Platforms accept far more; a small card loads fast in a feed.
MAX_BYTES = 150_000

# docs/31-brand-identity.md §4, §5.1 and §5.4: the navy field and the colours drawn on it. Ratios on NAVY:
# text 17.01:1, the wordmark 16.66:1, the endorsement and credits 9.17:1, the evergreen tint 9.81:1, the dot
# over the i 6.72:1.
NAVY = "#0b1023"
INK = NAVY  # the field every text colour is checked against
TEXT = "#f2f3f6"
MUTED = "#b0b5bf"
DOT = "#5fcf9f"  # --accent-tint: projects in process
COMMITTED = "#cdd5ea"  # the dark committed family: contracted projects, as squares
NEUTRAL = "#9aa3c0"  # announced projects, as rings
#: Compass orange (styles.css `--brand-dot`): the dot over the i, in the wordmark and in the mark; never
#: text or data. The one place it is set for the drawn files; `favicon.svg` and `wordmark.svg` write it out.
BRAND_DOT = "#e68023"
#: The mark and the wordmark on dark grounds (§5.1): a light circle (and light letters), a near-black stem.
MARK_LIGHT = "#eef1f5"
MARK_DARK_STEM = "#0e0f11"
WHITE = "#ffffff"
LAND = "#161c36"
STATE_LINE = "#2c3459"
#: Every colour any text on the card is drawn in (the wordmark's letters among them).
TEXT_COLOURS = (TEXT, MARK_LIGHT, MUTED)
#: The one font file: Geist's latin subset, weight set per use on its `wght` axis.
GEIST = "geist-latin.woff2"

#: Drawn at this multiple and reduced once, so shapes are anti-aliased like the text.
_SCALE = 2
_LEFT = 72

LINE = "Planned US energy projects, each linked to its source."
FOOT = "From grid queues, permits, dockets and RFPs"

# ------------------------------------------------------------------------------------------ type


def _font(size: float, weight: int) -> ImageFont.FreeTypeFont:
    """Geist at `size` px (card pixels) and `weight`. The basic layout engine, so the card draws the same
    with or without libraqm (glyph advances only; Geist's spacing needs no kerning at these sizes)."""
    font = ImageFont.truetype(str(FONTS / GEIST), round(size * _SCALE), layout_engine=ImageFont.Layout.BASIC)
    font.set_variation_by_axes([weight])
    return font


def _s(value: float) -> int:
    return round(value * _SCALE)


def _tracked(
    draw: ImageDraw.ImageDraw,
    xy: tuple[float, float],
    text: str,
    font: ImageFont.FreeTypeFont,
    fill: str,
    tracking: float,
) -> float:
    """Draw `text` with `tracking` card px added after each character (CSS `letter-spacing`, which Pillow
    has no setting for), baseline at `xy[1]`. Returns the x it ended at."""
    x, baseline = xy
    for char in text:
        draw.text((_s(x), _s(baseline)), char, font=font, fill=fill, anchor="ls")
        x += font.getlength(char) / _SCALE + tracking
    return x


def _wrap(text: str, font: ImageFont.FreeTypeFont, width: float, tracking: float) -> list[str]:
    def length(line: str) -> float:
        return font.getlength(line) / _SCALE + tracking * len(line)

    lines: list[str] = []
    line = ""
    for word in text.split():
        candidate = f"{line} {word}".strip()
        if line and length(candidate) > width:
            lines.append(line)
            line = word
        else:
            line = candidate
    if line:
        lines.append(line)
    return lines


# -------------------------------------------------------------------------------------- wordmark

#: §5.1 in Geist's units (1000 to the em): the two parts and their weights, the tracking (CSS
#: `letter-spacing`, after every glyph), the dot's diameter over the bold stem's width, and the gap between
#: the stem's top and the dot's foot (0.09em, as in the owner's chosen variant A, 2026-10-23).
WORDMARK_PARTS = (("\u0131nfra", 700), ("mation", 400))
WORDMARK_TRACKING = -30
DOT_TO_STEM = 1.25
DOT_GAP = 90
UNITS_PER_EM = 1000

_PATH_TOKEN = re.compile(r"[MLHVQCZ]|-?(?:\d+\.?\d*|\.\d+)")


@dataclass(frozen=True)
class Glyph:
    """One outlined glyph of the wordmark: Geist's glyph name, the weight it was cut at, and its outline as
    SVG path data (absolute M, L, H, V, Q, C and Z; y down; the baseline at y 0; placed in the word)."""

    name: str
    weight: int
    d: str


@dataclass(frozen=True)
class Wordmark:
    """The outlined wordmark: its glyphs in reading order (the first is the dotless i) and its dot."""

    glyphs: tuple[Glyph, ...]
    dot: tuple[float, float, float]  # centre x, centre y, radius

    @property
    def stem(self) -> tuple[float, float, float, float]:
        """The bold dotless i's bounds, measured from its outline: (left, top, right, bottom)."""
        return path_bounds(self.glyphs[0].d)

    def bounds(self) -> tuple[float, float, float, float]:
        """The ink's bounds, the dot included: (left, top, right, bottom)."""
        boxes = [path_bounds(glyph.d) for glyph in self.glyphs]
        cx, cy, r = self.dot
        boxes.append((cx - r, cy - r, cx + r, cy + r))
        return (
            min(b[0] for b in boxes),
            min(b[1] for b in boxes),
            max(b[2] for b in boxes),
            max(b[3] for b in boxes),
        )


def path_contours(d: str, steps: int = 12) -> list[list[tuple[float, float]]]:
    """SVG path data as `build_wordmark` writes it, flattened into closed polylines (curves in `steps`)."""
    tokens = _PATH_TOKEN.findall(d)
    contours: list[list[tuple[float, float]]] = []
    points: list[tuple[float, float]] = []
    x = y = 0.0
    i = 0

    def take(n: int) -> list[float]:
        nonlocal i
        values = [float(t) for t in tokens[i : i + n]]
        i += n
        return values

    while i < len(tokens):
        command = tokens[i]
        i += 1
        if command == "M":
            if points:
                contours.append(points)
            x, y = take(2)
            points = [(x, y)]
        elif command == "L":
            x, y = take(2)
            points.append((x, y))
        elif command == "H":
            (x,) = take(1)
            points.append((x, y))
        elif command == "V":
            (y,) = take(1)
            points.append((x, y))
        elif command == "Q":
            cx, cy, ex, ey = take(4)
            for k in range(1, steps + 1):
                t = k / steps
                points.append(
                    (
                        (1 - t) ** 2 * x + 2 * (1 - t) * t * cx + t**2 * ex,
                        (1 - t) ** 2 * y + 2 * (1 - t) * t * cy + t**2 * ey,
                    )
                )
            x, y = ex, ey
        elif command == "C":
            c1x, c1y, c2x, c2y, ex, ey = take(6)
            for k in range(1, steps + 1):
                t = k / steps
                u = 1 - t
                points.append(
                    (
                        u**3 * x + 3 * u * u * t * c1x + 3 * u * t * t * c2x + t**3 * ex,
                        u**3 * y + 3 * u * u * t * c1y + 3 * u * t * t * c2y + t**3 * ey,
                    )
                )
            x, y = ex, ey
        elif command == "Z":
            if points:
                contours.append(points)
            points = []
        else:  # pragma: no cover - the tokenizer yields only the commands above
            raise ValueError(f"path command {command!r}")
    if points:
        contours.append(points)
    return contours


def path_bounds(d: str) -> tuple[float, float, float, float]:
    """The bounds of an outline (left, top, right, bottom), from its points with the curves flattened: exact
    for straight edges, like the stem's."""
    points = [point for contour in path_contours(d) for point in contour]
    xs, ys = [px for px, _ in points], [py for _, py in points]
    return min(xs), min(ys), max(xs), max(ys)


def _num(value: float) -> str:
    """A coordinate to a tenth of a font unit (a ten-thousandth of an em), without trailing zeros."""
    rounded = round(value, 1) or 0.0
    return f"{rounded:.1f}".rstrip("0").rstrip(".")


def build_wordmark(font_path: Path = FONTS / GEIST) -> Wordmark:  # pragma: no cover - a person runs it
    """Cut the wordmark from Geist: each part instanced at its weight with overlaps removed, shaped by
    HarfBuzz (kerning on, ligatures off, as CSS sets letter-spaced text), tracked -0.03em, and drawn as
    absolute path data. The dot's centre is the bold stem's centre as measured from that outline, its
    diameter 1.25 times the stem's width, its foot 0.09em above the stem's top.

    Needs `pip install fonttools brotli skia-pathops uharfbuzz` (none of them are site dependencies)."""
    ttlib = importlib.import_module("fontTools.ttLib")
    instancer = importlib.import_module("fontTools.varLib.instancer")
    overlaps = importlib.import_module("fontTools.ttLib.removeOverlaps")
    svg_path_pen = importlib.import_module("fontTools.pens.svgPathPen")
    transform_pen = importlib.import_module("fontTools.pens.transformPen")
    harfbuzz = importlib.import_module("uharfbuzz")

    glyphs: list[Glyph] = []
    pen_x = 0.0
    for text, weight in WORDMARK_PARTS:
        font = instancer.instantiateVariableFont(ttlib.TTFont(str(font_path)), {"wght": weight})
        overlaps.removeOverlaps(font)
        font.flavor = None
        data = io.BytesIO()
        font.save(data)
        shaper = harfbuzz.Font(harfbuzz.Face(data.getvalue()))
        shaper.scale = (UNITS_PER_EM, UNITS_PER_EM)
        buffer = harfbuzz.Buffer()
        buffer.add_str(text)
        buffer.guess_segment_properties()
        harfbuzz.shape(shaper, buffer, {"liga": False, "kern": True})
        names = font.getGlyphOrder()
        glyph_set = font.getGlyphSet()
        for info, position in zip(buffer.glyph_infos, buffer.glyph_positions, strict=True):
            name = names[info.codepoint]
            pen = svg_path_pen.SVGPathPen(glyph_set, ntos=_num)
            placed = (1, 0, 0, -1, pen_x + position.x_offset, -position.y_offset)
            glyph_set[name].draw(transform_pen.TransformPen(pen, placed))
            glyphs.append(Glyph(name, weight, pen.getCommands()))
            pen_x += position.x_advance + WORDMARK_TRACKING
    left, top, right, _ = path_bounds(glyphs[0].d)
    radius = DOT_TO_STEM * (right - left) / 2
    dot = (round((left + right) / 2, 2), round(top - DOT_GAP - radius, 2), round(radius, 2))
    return Wordmark(tuple(glyphs), dot)


def _view_box(word: Wordmark, *, to_baseline: bool) -> tuple[int, int, int, int]:
    """Whole font units around the ink; with `to_baseline` the box ends at the baseline (y 0) so its
    bottom edge is the baseline a flex row aligns on, and the round letters' overshoot paints below it."""
    left, top, right, bottom = word.bounds()
    x, y = math.floor(left), math.floor(top)
    end = 0 if to_baseline else math.ceil(bottom)
    return x, y, math.ceil(right) - x, end - y


def _glyph_paths(word: Wordmark, separator: str) -> str:
    return separator.join(
        f'<path data-glyph="{g.name}" data-weight="{g.weight}" d="{g.d}"/>' for g in word.glyphs
    )


def _em(units: int) -> str:
    return f"{units / UNITS_PER_EM:.3f}em"


def wordmark_svg(word: Wordmark) -> str:
    """The standalone `wordmark.svg`: navy letters (`#eef1f5` under a dark colour scheme) and the orange dot,
    titled "Inframation", sized in em so it scales with the text around it."""
    x, y, w, h = _view_box(word, to_baseline=False)
    cx, cy, r = word.dot
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" '
        f'viewBox="{x} {y} {w} {h}" width="{_em(w)}" height="{_em(h)}">\n'
        "  <title>Inframation</title>\n"
        '  <!-- Inframation\'s wordmark (docs/31-brand-identity.md §5.1): "infra" in Geist 700,\n'
        '       "mation" in Geist 400, tracking -0.03em, outlined; the first i is dotless and\n'
        "       carries the orange dot, centred on the bold stem as measured from its outline.\n"
        "       Cut from Geist by web/og_card.py `build_wordmark`; the top bar inlines the same\n"
        "       paths (web/templates/partials/_top_bar.html). -->\n"
        "  <style>\n"
        f"    .ink {{ fill: {NAVY}; }}\n"
        f"    @media (prefers-color-scheme: dark) {{ .ink {{ fill: {MARK_LIGHT}; }} }}\n"
        "  </style>\n"
        f'  <g class="ink">\n    {_glyph_paths(word, chr(10) + "    ")}\n  </g>\n'
        f'  <circle fill="{BRAND_DOT}" cx="{cx:g}" cy="{cy:g}" r="{r:g}"/>\n'
        "</svg>\n"
    )


def inline_wordmark(word: Wordmark) -> str:
    """The top bar's copy: the same paths in `currentColor` and the dot in `--brand-dot` (styles.css
    `.wordmark__dot`), hidden from assistive technology (the link carries the name), its box ending at the
    baseline so the endorsement sits on it."""
    x, y, w, h = _view_box(word, to_baseline=True)
    cx, cy, r = word.dot
    return (
        f'<svg class="wordmark__svg" viewBox="{x} {y} {w} {h}" width="{_em(w)}" height="{_em(h)}" '
        'overflow="visible" aria-hidden="true" focusable="false">'
        f'<g fill="currentColor">{_glyph_paths(word, "")}</g>'
        f'<circle class="wordmark__dot" cx="{cx:g}" cy="{cy:g}" r="{r:g}"/></svg>'
    )


_GLYPH = re.compile(r'<path data-glyph="([^"]+)" data-weight="(\d+)" d="([^"]+)"/>')
_DOT = re.compile(
    r'<circle (?:fill="[^"]+"|class="wordmark__dot") cx="([-\d.]+)" cy="([-\d.]+)" r="([\d.]+)"/>'
)


def read_wordmark(svg: str) -> Wordmark:
    """The glyphs and the dot of a drawn wordmark (`wordmark.svg`, or the top bar's inline copy)."""
    glyphs = tuple(Glyph(html.unescape(n), int(w), d) for n, w, d in _GLYPH.findall(svg))
    dot = _DOT.search(svg)
    if not glyphs or dot is None:
        raise ValueError("not a drawn wordmark")
    return Wordmark(glyphs, (float(dot.group(1)), float(dot.group(2)), float(dot.group(3))))


def _fill_paths(
    image: Image.Image, paths: Sequence[str], origin: tuple[float, float], scale: float, colour: str
) -> None:
    """Fill outlines (even-odd, which for overlap-free glyphs is their own fill) in `colour` onto `image`,
    `scale` image px per font unit, the outlines' (0, 0) at `origin`. Drawn at 4x and reduced, so the edges
    are anti-aliased like the text."""
    up = 4
    contours = [c for d in paths for c in path_contours(d)]
    xs = [x for c in contours for x, _ in c]
    ys = [y for c in contours for _, y in c]
    left, top = math.floor(origin[0] + min(xs) * scale) - 1, math.floor(origin[1] + min(ys) * scale) - 1
    right, bottom = math.ceil(origin[0] + max(xs) * scale) + 1, math.ceil(origin[1] + max(ys) * scale) + 1
    size = ((right - left) * up, (bottom - top) * up)
    mask = Image.new("1", size, 0)
    for contour in contours:
        shape = Image.new("1", size, 0)
        points = [
            ((origin[0] + x * scale - left) * up, (origin[1] + y * scale - top) * up) for x, y in contour
        ]
        ImageDraw.Draw(shape).polygon(points, fill=1)
        mask = ImageChops.logical_xor(mask, shape)
    alpha = mask.convert("L").resize((right - left, bottom - top), Image.Resampling.BOX)
    image.paste(Image.new("RGBA", alpha.size, colour), (left, top), alpha)


def _circle_path(cx: float, cy: float, r: float) -> str:
    """A circle as path data (four cubic quarters), so the dot fills like the letters."""
    k = 0.5522847498 * r
    return (
        f"M{cx + r} {cy}C{cx + r} {cy + k} {cx + k} {cy + r} {cx} {cy + r}"
        f"C{cx - k} {cy + r} {cx - r} {cy + k} {cx - r} {cy}"
        f"C{cx - r} {cy - k} {cx - k} {cy - r} {cx} {cy - r}"
        f"C{cx + k} {cy - r} {cx + r} {cy - k} {cx + r} {cy}Z"
    )


def _lockup(image: Image.Image, x: float, baseline: float, size: float) -> float:
    """§5.1 and §5.4 on the navy card: the wordmark from `wordmark.svg` in `MARK_LIGHT` with its orange dot,
    its ink starting at `x`. The mark is never drawn beside it (owner, 2026-10-24: the circled i is not
    next to the spelled-out logo); the wordmark's own dot carries the i. `size` is the wordmark's em in
    card px. Returns the x where the wordmark's ink starts."""
    word = read_wordmark(WORDMARK_PATH.read_text(encoding="utf-8"))
    left, _, _, _ = word.bounds()
    scale = size * _SCALE / UNITS_PER_EM
    word_x = x
    origin = (_s(word_x) - left * scale, _s(baseline))
    _fill_paths(image, [g.d for g in word.glyphs], origin, scale, MARK_LIGHT)
    _fill_paths(image, [_circle_path(*word.dot)], origin, scale, BRAND_DOT)
    return word_x


# ------------------------------------------------------------------------------------------- map

# Albers equal-area conic for the lower 48 (standard parallels 29.5 and 45.5, origin 37.5N 96W), the
# projection national US maps use.
_P1, _P2, _P0, _L0 = (math.radians(v) for v in (29.5, 45.5, 37.5, -96.0))
_N = (math.sin(_P1) + math.sin(_P2)) / 2
_C = math.cos(_P1) ** 2 + 2 * _N * math.sin(_P1)
_R0 = math.sqrt(_C - 2 * _N * math.sin(_P0)) / _N
#: Outside the lower 48.
_NOT_CONUS = {"US-AK", "US-HI", "US-PR", "US-VI", "US-GU", "US-AS", "US-MP"}
#: Where the map sits on the card (card px): it runs off the right edge, as on the site's front page.
_MAP_BOX = (516.0, 36.0, 1276.0, 556.0)


def _albers(lon: float, lat: float) -> tuple[float, float]:
    rho = math.sqrt(max(_C - 2 * _N * math.sin(math.radians(lat)), 0.0)) / _N
    theta = _N * (math.radians(lon) - _L0)
    return rho * math.sin(theta), -(_R0 - rho * math.cos(theta))


def _rings(geometry: Mapping[str, Any]) -> list[list[list[float]]]:
    if geometry["type"] == "Polygon":
        return list(geometry["coordinates"])
    return [ring for polygon in geometry["coordinates"] for ring in polygon]


def _states() -> list[list[tuple[float, float]]]:
    """The lower 48's state outlines, projected (outer and inner rings alike)."""
    features = json.loads((REGIONS / "us_states.geojson").read_text(encoding="utf-8"))["features"]
    return [
        [_albers(lon, lat) for lon, lat in ring]
        for feature in features
        if feature["properties"]["region_id"] not in _NOT_CONUS
        for ring in _rings(feature["geometry"])
    ]


def _fit(rings: Sequence[Sequence[tuple[float, float]]]) -> tuple[float, float, float]:
    """Scale and offset that fit the outlines into `_MAP_BOX`, centred."""
    xs = [x for ring in rings for x, _ in ring]
    ys = [y for ring in rings for _, y in ring]
    x0, y0, x1, y1 = _MAP_BOX
    scale = min((x1 - x0) / (max(xs) - min(xs)), (y1 - y0) / (max(ys) - min(ys)))
    dx = x0 + ((x1 - x0) - (max(xs) - min(xs)) * scale) / 2 - min(xs) * scale
    dy = y0 + ((y1 - y0) - (max(ys) - min(ys)) * scale) / 2 - min(ys) * scale
    return scale, dx, dy


def _draw_map(image: Image.Image) -> None:
    layer = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    rings = _states()
    scale, dx, dy = _fit(rings)

    def at(x: float, y: float) -> tuple[int, int]:
        return _s(x * scale + dx), _s(y * scale + dy)

    for ring in rings:
        draw.polygon([at(x, y) for x, y in ring], fill=LAND)
    for ring in rings:
        draw.line([at(x, y) for x, y in ring], fill=STATE_LINE, width=_s(1), joint="curve")

    points = json.loads(MAP_DATA_PATH.read_text(encoding="utf-8"))["points"]
    # Rings first, then discs, then squares; the largest first in each, so small marks stay on top.
    for family in ("neutral", "progress", "committed"):
        marks = sorted((p for p in points if p[3] == family), key=lambda p: -int(p[2]))
        for lon, lat, count, _ in marks:
            cx, cy = at(*_albers(float(lon), float(lat)))
            r = (1.6 + math.sqrt(int(count)) * 1.05) * _SCALE
            box = [cx - r, cy - r, cx + r, cy + r]
            if family == "progress":
                draw.ellipse(box, fill=DOT)
            elif family == "committed":
                k = r * 0.89
                draw.rectangle([cx - k, cy - k, cx + k, cy + k], fill=COMMITTED)
            else:
                draw.ellipse(box, outline=NEUTRAL, width=max(2, _s(1.2)))
    image.alpha_composite(layer)


def render() -> Image.Image:
    """The card as an RGB image, `WIDTH` x `HEIGHT`."""
    image = Image.new("RGBA", (WIDTH * _SCALE, HEIGHT * _SCALE), NAVY)
    _draw_map(image)
    # The lockup: the wordmark alone (never the mark beside it), and under it the endorsement (text; the
    # Compass logo is never drawn).
    word_x = _lockup(image, _LEFT, 128, 72)
    draw = ImageDraw.Draw(image)
    draw.text((_s(word_x), _s(176)), "a Compass company", font=_font(26, 400), fill=MUTED, anchor="ls")

    # The one line, and where the records come from.
    line_font = _font(40, 500)
    tracking = -0.02 * 40
    lines = _wrap(LINE, line_font, 520, tracking)
    for row, text in enumerate(lines):
        _tracked(draw, (_LEFT, 452 + row * 46), text, line_font, TEXT, tracking)
    draw.text((_s(_LEFT), _s(570)), FOOT, font=_font(22, 400), fill=MUTED, anchor="ls")

    # The map's own credit, as every map on the site carries one.
    credit = "Map: " + ", ".join(json.loads(MAP_DATA_PATH.read_text(encoding="utf-8"))["credit"])
    credit_font = _font(16, 400)
    width = credit_font.getlength(credit) / _SCALE
    draw.text((_s(WIDTH - 40 - width), _s(604)), credit, font=credit_font, fill=MUTED, anchor="ls")

    return image.convert("RGB").resize((WIDTH, HEIGHT), Image.Resampling.LANCZOS)


def write(path: Path = OG_CARD_PATH) -> int:
    """Render and save the card as an optimised 256-colour PNG; returns its size in bytes. The card is
    flat colour, small marks and anti-aliased text, which a palette holds without visible banding. The
    octree keeps the few dot pixels at Compass orange exactly; median cut merged them into a duller
    orange (210, 138, 73) and wrote a larger file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    card = render().quantize(colors=256, method=Image.Quantize.FASTOCTREE, dither=Image.Dither.NONE)
    card.save(path, format="PNG", optimize=True)
    return path.stat().st_size


# ----------------------------------------------------------------------------------------- icons


#: The mark on its 16-unit grid (§5.1): the circle's radius, the stem's box (left, top, right, bottom) and
#: corner radius, and the dot's centre and radius. `favicon.svg` draws the same numbers.
MARK_RADIUS = 8.0
MARK_STEM = (6.6, 7.35, 9.4, 13.25)
MARK_STEM_CORNER = 0.5
MARK_DOT = (8.0, 4.55, 1.75)


def mark(
    size: int,
    circle: str = NAVY,
    stem: str = WHITE,
    ground: str | None = None,
    inset: float = 0.0,
) -> Image.Image:
    """`favicon.svg`'s mark, the information sign, at `size` px, drawn at 8x and reduced: a `circle` of
    radius 8 on the 16-unit grid, the i's `stem` (2.8 wide, y 7.35 to 13.25, corner radius 0.5) and the
    orange dot at (8, 4.55), radius 1.75. Light grounds take the defaults (navy circle, white stem); dark
    grounds `MARK_LIGHT` and `MARK_DARK_STEM`. `inset` is the share of the box left clear on each side, for
    a home-screen icon that sits on a `ground`."""
    big = size * 8
    image = Image.new("RGBA", (big, big), ground or (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    unit = big * (1 - 2 * inset) / 16
    off = big * inset

    def at(x: float, y: float) -> tuple[float, float]:
        return off + x * unit, off + y * unit

    def disc(cx: float, cy: float, r: float, fill: str) -> None:
        (x0, y0), (x1, y1) = at(cx - r, cy - r), at(cx + r, cy + r)
        draw.ellipse([x0, y0, x1 - 1, y1 - 1], fill=fill)  # Pillow's box includes its far edge

    disc(8, 8, MARK_RADIUS, circle)
    (x0, y0), (x1, y1) = at(*MARK_STEM[:2]), at(*MARK_STEM[2:])
    draw.rounded_rectangle([x0, y0, x1 - 1, y1 - 1], radius=MARK_STEM_CORNER * unit, fill=stem)
    disc(*MARK_DOT, BRAND_DOT)
    return image.resize((size, size), Image.Resampling.LANCZOS)


def write_icons(ico: Path = FAVICON_ICO_PATH, touch: Path = TOUCH_ICON_PATH) -> None:
    """The ICO (16, 32 and 48px frames: the light-ground mark, navy circle and white stem, on a transparent
    ground) for clients that take no SVG, and the 180px home-screen icon: the same mark on the light ledger
    ground, inset a tenth on each side, because iOS fills a transparent icon's corners with black."""
    frames = [mark(px) for px in (48, 32, 16)]
    frames[0].save(ico, format="ICO", sizes=[(48, 48), (32, 32), (16, 16)], append_images=frames[1:])
    mark(180, ground=MARK_LIGHT, inset=0.1).convert("RGB").save(touch, format="PNG", optimize=True)


def favicon_svg() -> str:
    """`favicon.svg`: the mark of `MARK_*` in the light-ground colours, switched to the dark-ground ones
    under a dark browser theme (a tab strip may be either); the dot keeps its orange in both."""
    sx0, sy0, sx1, sy1 = MARK_STEM
    cx, cy, r = MARK_DOT
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16">\n'
        "  <!-- Inframation's mark (docs/31-brand-identity.md §5.1, B-3): the information sign, a\n"
        "       circle with the i knocked out and the orange dot. Light: navy circle, white stem;\n"
        "       dark: a #eef1f5 circle, a #0e0f11 stem. favicon.ico and apple-touch-icon.png draw\n"
        "       the same shapes; web/og_card.py `favicon_svg` writes this file. -->\n"
        "  <style>\n"
        f"    .disc {{ fill: {NAVY}; }}\n"
        f"    .stem {{ fill: {WHITE}; }}\n"
        "    @media (prefers-color-scheme: dark) {\n"
        f"      .disc {{ fill: {MARK_LIGHT}; }}\n"
        f"      .stem {{ fill: {MARK_DARK_STEM}; }}\n"
        "    }\n"
        "  </style>\n"
        f'  <circle class="disc" cx="8" cy="8" r="{MARK_RADIUS:g}"/>\n'
        f'  <rect class="stem" x="{sx0:g}" y="{sy0:g}" width="{sx1 - sx0:.1f}" height="{sy1 - sy0:.1f}" '
        f'rx="{MARK_STEM_CORNER:g}"/>\n'
        f'  <circle fill="{BRAND_DOT}" cx="{cx:g}" cy="{cy:g}" r="{r:g}"/>\n'
        "</svg>\n"
    )


# ------------------------------------------------------------------------------- the map's points

#: The fixture's short source names and their register ids.
FIXTURE_SOURCES = {
    "caiso": "us.iso.caiso.gen_queue",
    "ercot": "us.iso.ercot.gen_queue",
    "nyiso": "us.iso.nyiso.gen_queue",
    "spp": "us.iso.spp.gen_queue",
    "isone": "us.iso.isone.gen_queue",
    "eia860m": "us.eia.860m",
}
#: What each drawable source is called in the card's credit line.
CREDIT_NAMES = {"us.eia.860m": "EIA-860M", "us.iso.ercot.gen_queue": "ERCOT"}
#: The active lifecycle states the front page counts, and the mark each family draws.
ACTIVE_FAMILY = {
    "announced": "neutral",
    "filed": "progress",
    "studied": "progress",
    "permitted": "progress",
    "under_construction": "progress",
    "contracted": "committed",
}
_COUNTY_WORDS = (" COUNTY", " PARISH", " BOROUGH", " CENSUS AREA", " CITY AND BOROUGH", " MUNICIPALITY")


def drawable_sources(register: Iterable[Mapping[str, Any]]) -> set[str]:
    """Register ids a share card may draw: `reuse: open` and published (`raw_ok` or `derived_only`)."""
    return {
        str(entry["id"])
        for entry in register
        if entry.get("reuse") == "open" and entry.get("publication") in {"raw_ok", "derived_only"}
    }


def county_key(state: str, county: str) -> tuple[str, str]:
    """`("US-TX", "HARRIS")` from `("TX", "Harris County")`; the first of a multi-county "A/B"."""
    name = county.split("/")[0].strip().upper().replace(".", "")
    for word in _COUNTY_WORDS:
        name = name.removesuffix(word)
    return f"US-{state.strip().upper()}", " ".join(name.split())


def county_centres(features: Iterable[Mapping[str, Any]]) -> dict[tuple[str, str], tuple[float, float]]:
    """The area centroid of each county's largest ring, keyed by `county_key`."""
    centres: dict[tuple[str, str], tuple[float, float]] = {}
    for feature in features:
        props = feature["properties"]
        best: tuple[float, float, float] | None = None
        for ring in _rings(feature["geometry"]):
            area = cx = cy = 0.0
            for (x0, y0), (x1, y1) in pairwise(ring):
                cross = x0 * y1 - x1 * y0
                area += cross
                cx += (x0 + x1) * cross
                cy += (y0 + y1) * cross
            if area and (best is None or abs(area) > best[0]):
                best = (abs(area), cx / (3 * area), cy / (3 * area))
        if best is not None:
            centres[(str(props["state_code"]), county_key("XX", str(props["name"]))[1])] = (best[1], best[2])
    return centres


def map_points(
    rows: Iterable[Mapping[str, Any]],
    drawable: set[str],
    centres: Mapping[tuple[str, str], tuple[float, float]],
) -> list[list[Any]]:
    """One point per county, `[lon, lat, count, family]`: its centre, its projects' count and the family
    most of them are in."""
    counts: dict[tuple[str, str], Counter[str]] = {}
    for row in rows:
        source = FIXTURE_SOURCES.get(str(row.get("source_id")))
        family = ACTIVE_FAMILY.get(str(row.get("lifecycle_state")))
        state, county = row.get("state"), row.get("county")
        if (
            source not in drawable
            or family is None
            or not isinstance(state, str)
            or not isinstance(county, str)
        ):
            continue
        key = county_key(state, county)
        if key[0] in _NOT_CONUS or key not in centres:
            continue
        counts.setdefault(key, Counter())[family] += 1
    points: list[list[Any]] = []
    for key, families in sorted(counts.items()):
        lon, lat = centres[key]
        points.append([round(lon, 3), round(lat, 3), sum(families.values()), families.most_common(1)[0][0]])
    return points


def rebuild_map_data(out: Path = MAP_DATA_PATH) -> dict[str, Any]:  # pragma: no cover - a person runs it
    """Write `og_card_map.json` from the fixture, the register and the Census county outlines."""
    import pandas as pd
    import yaml

    register = yaml.safe_load(REGISTER.read_text(encoding="utf-8"))
    entries = register["sources"] if isinstance(register, dict) else register
    drawable = drawable_sources(entries)
    counties = json.loads((REGIONS / "us_counties.geojson").read_text(encoding="utf-8"))["features"]
    rows = pd.read_parquet(FIXTURE, columns=["source_id", "state", "county", "lifecycle_state"])
    records = [{k: (None if pd.isna(v) else v) for k, v in r.items()} for r in rows.to_dict(orient="records")]
    points = map_points(records, drawable, county_centres(counties))
    used = sorted(
        {
            FIXTURE_SOURCES[str(r["source_id"])]
            for r in records
            if FIXTURE_SOURCES.get(str(r["source_id"])) in drawable
        }
    )
    data = {
        "about": (
            "Located active proposals of data/eval/normalized.parquet counted per county centre, from the "
            "register's reuse-open, published sources only (web/og_card.py `rebuild_map_data`)."
        ),
        "sources": used,
        "credit": [CREDIT_NAMES.get(s, s) for s in used] + ["Census outlines"],
        "points": points,
    }
    head = json.dumps({k: v for k, v in data.items() if k != "points"}, indent=1)[:-2]
    body = ",\n".join(json.dumps(p, separators=(",", ":")) for p in points)
    out.write_text(f'{head},\n "points": [\n{body}\n ]\n}}\n', encoding="utf-8")
    return data


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--out", type=Path, default=OG_CARD_PATH, help="where to write the PNG")
    parser.add_argument(
        "--icons", action="store_true", help="also redraw favicon.svg, favicon.ico and apple-touch-icon.png"
    )
    parser.add_argument(
        "--wordmark",
        action="store_true",
        help="cut wordmark.svg from Geist first and print the top bar's inline copy (needs fontTools)",
    )
    parser.add_argument(
        "--rebuild-map", action="store_true", help="rebuild og_card_map.json from the fixture first"
    )
    args = parser.parse_args(argv)
    if args.rebuild_map:  # pragma: no cover - a person runs it
        built = rebuild_map_data()
        sources = ", ".join(built["sources"])
        print(f"wrote {MAP_DATA_PATH} ({len(built['points'])} county points from {sources})")  # noqa: T201
    if args.wordmark:  # pragma: no cover - a person runs it
        word = build_wordmark()
        WORDMARK_PATH.write_text(wordmark_svg(word), encoding="utf-8")
        print(f"wrote {WORDMARK_PATH}; paste into _top_bar.html:\n{inline_wordmark(word)}")  # noqa: T201
    if args.icons:
        (IMG / "favicon.svg").write_text(favicon_svg(), encoding="utf-8")
        write_icons()
        print(f"wrote favicon.svg, {FAVICON_ICO_PATH} and {TOUCH_ICON_PATH}")  # noqa: T201 — CLI summary line
    size = write(args.out)
    print(f"wrote {args.out} ({WIDTH}x{HEIGHT}, {size:,} bytes)")  # noqa: T201 — CLI summary line
    return 0 if size <= MAX_BYTES else 1


if __name__ == "__main__":
    raise SystemExit(main())
