"""Builds the site's Open Graph / Twitter card image, `web/static/img/og-card.png`.

Run `python -m web.og_card` after changing anything below and commit the PNG it writes. The image
is self-made: the docs/31 §1.1–§1.2 palette and the self-hosted IBM Plex files in
`web/static/fonts/` (read as shipped; Pillow's FreeType opens WOFF2 directly), no third-party
imagery, map tiles or other organisations' marks. Nothing at runtime imports this module; the page
template (`web/templates/base.html`) links the committed file.

The card is ink, so it reads in light and dark feeds alike. Copper is never drawn on ink (D-20);
the accent is copper-tint. Every text colour is at least 4.5:1 on ink (`web/test_og_card.py`
recomputes it). The lifecycle strip names each state in words beside its icon, never by colour
alone (D-5), with the same icon shapes as `base.html`'s status symbols.

The URL has no version query, so the link platforms cache stays valid across deploys. If the
picture changes in a way a cached card must not keep showing, give the file a new name, as the
fonts directory does (`web/static/fonts/README.md`).
"""

from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

WEB_ROOT = Path(__file__).resolve().parent
FONTS = WEB_ROOT / "static" / "fonts"
#: Where the card is written, and the path `base.html` links under the site's base URL.
OG_CARD_PATH = WEB_ROOT / "static" / "img" / "og-card.png"
OG_CARD_URL_PATH = "static/img/og-card.png"
WIDTH, HEIGHT = 1200, 630
#: Mirrors `og:image:alt` / `twitter:image:alt` in `base.html` (pinned by `web/test_og_card.py`).
OG_CARD_ALT = (
    "Infraque: energy and infrastructure proposals and opportunities, one record per project, "
    "traced from announced to built."
)
#: Budget for the committed file. Platforms accept far more; a small card loads fast in a feed.
MAX_BYTES = 150_000

# docs/31 §1.1 core and §1.2 dark tints (the on-ink column).
INK = "#16324f"
PAPER = "#f6f4ef"
COPPER_TINT = "#e0b58a"
NEUTRAL_TINT = "#c9d2da"
PROGRESS_TINT = "#8fc4dd"
SUCCESS_TINT = "#8fd6ac"
#: The hairline rules: paper over ink at about 22%, a border, not text.
RULE = "#46607a"
#: Every colour any text on the card is drawn in.
TEXT_COLOURS = (PAPER, COPPER_TINT, NEUTRAL_TINT)

#: Drawn at this multiple and reduced once, so shapes are anti-aliased like the text.
_SCALE = 2
_MARGIN = 80

#: Proposal lifecycle, left to right: (label, icon, colour). A subset of docs/31 §1.2's states,
#: one per family the forward path passes through.
LIFECYCLE: tuple[tuple[str, str, str], ...] = (
    ("Announced", "neutral", NEUTRAL_TINT),
    ("Filed", "progress", PROGRESS_TINT),
    ("Permitted", "progress", PROGRESS_TINT),
    ("Contracted", "committed", COPPER_TINT),
    ("Built", "success", SUCCESS_TINT),
)


def _font(name: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONTS / name), size * _SCALE)


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
    """Draw `text` letter by letter with `tracking` px added after each (the masthead's
    `letter-spacing`, which Pillow has no setting for). Returns the x it ended at."""
    x, y = xy
    for char in text:
        draw.text((_s(x), _s(y)), char, font=font, fill=fill)
        x += font.getlength(char) / _SCALE + tracking
    return x


def _wrap(text: str, font: ImageFont.FreeTypeFont, width: float) -> list[str]:
    lines: list[str] = []
    line = ""
    for word in text.split():
        candidate = f"{line} {word}".strip()
        if line and font.getlength(candidate) / _SCALE > width:
            lines.append(line)
            line = word
        else:
            line = candidate
    if line:
        lines.append(line)
    return lines


def _icon(draw: ImageDraw.ImageDraw, kind: str, cx: float, cy: float, colour: str) -> None:
    """The `base.html` status symbols (a 12-unit viewBox) at 2.4x, on an ink disc so the strip's
    line stops at the icon's edge."""
    unit = 2.4
    r = 4.5 * unit
    stroke = _s(1.8 * unit)
    draw.ellipse([_s(cx - r - 8), _s(cy - r - 8), _s(cx + r + 8), _s(cy + r + 8)], fill=INK)
    box = [_s(cx - r), _s(cy - r), _s(cx + r), _s(cy + r)]
    if kind == "neutral":
        draw.ellipse(box, outline=colour, width=stroke)
    elif kind == "progress":
        draw.ellipse(box, outline=RULE, width=stroke)
        draw.arc(box, start=-90, end=90, fill=colour, width=stroke)
    elif kind == "committed":
        half = 4 * unit
        draw.rectangle([_s(cx - half), _s(cy - half), _s(cx + half), _s(cy + half)], fill=colour)
    elif kind == "success":
        points = [(2.2, 6.3), (4.6, 8.7), (9.8, 3.3)]
        draw.line(
            [(_s(cx + (px - 6) * unit), _s(cy + (py - 6) * unit)) for px, py in points],
            fill=colour,
            width=stroke,
            joint="curve",
        )
    else:  # pragma: no cover - a typo in LIFECYCLE, caught the first time the script runs
        raise ValueError(f"unknown icon {kind!r}")


def render() -> Image.Image:
    """The card as an RGB image, `WIDTH` x `HEIGHT`."""
    image = Image.new("RGB", (WIDTH * _SCALE, HEIGHT * _SCALE), INK)
    draw = ImageDraw.Draw(image)
    right = WIDTH - _MARGIN

    # Masthead line, as the site header sets it: mono, upper case, tracked.
    mono_medium = _font("IBMPlexMono-Medium-Latin1.woff2", 21)
    _tracked(draw, (_MARGIN, 66), "INFRAQUE REGISTER · PUBLIC EDITION", mono_medium, COPPER_TINT, 2.2)
    draw.line([(_s(_MARGIN), _s(108)), (_s(right), _s(108))], fill=RULE, width=_s(1))

    # Wordmark and the one sentence the site's default meta description leads with.
    draw.text(
        (_s(_MARGIN - 6), _s(132)),
        "Infraque",
        font=_font("IBMPlexSans-SemiBold-Latin1.woff2", 128),
        fill=PAPER,
    )
    tagline = _font("IBMPlexSans-Regular-Latin1.woff2", 40)
    sentence = "Energy and infrastructure proposals and opportunities, fused into one record per project."
    for row, line in enumerate(_wrap(sentence, tagline, right - _MARGIN)):
        draw.text((_s(_MARGIN), _s(306 + row * 54)), line, font=tagline, fill=NEUTRAL_TINT)

    # Lifecycle strip: one line, one icon and label per state.
    strip_y = 470
    draw.line([(_s(_MARGIN + 14), _s(strip_y)), (_s(right - 14), _s(strip_y))], fill=RULE, width=_s(2))
    label_font = _font("IBMPlexMono-Regular-Latin1.woff2", 20)
    step = (right - _MARGIN - 28) / (len(LIFECYCLE) - 1)
    for index, (label, kind, colour) in enumerate(LIFECYCLE):
        cx = _MARGIN + 14 + index * step
        _icon(draw, kind, cx, strip_y, colour)
        width = label_font.getlength(label) / _SCALE
        if index == 0:
            x = cx - 14
        elif index == len(LIFECYCLE) - 1:
            x = cx + 14 - width
        else:
            x = cx - width / 2
        draw.text((_s(x), _s(strip_y + 26)), label, font=label_font, fill=NEUTRAL_TINT)

    # Footer: the provenance promise every page keeps.
    draw.line([(_s(_MARGIN), _s(556)), (_s(right), _s(556))], fill=RULE, width=_s(1))
    footer = _font("IBMPlexSans-Medium-Latin1.woff2", 22)
    draw.text(
        (_s(_MARGIN), _s(572)), "Every record attributed to its source register", font=footer, fill=PAPER
    )

    return image.resize((WIDTH, HEIGHT), Image.Resampling.LANCZOS)


def write(path: Path = OG_CARD_PATH) -> int:
    """Render and save the card as an optimised 256-colour PNG; returns its size in bytes. The card
    is flat colour and anti-aliased text, which a palette holds without visible banding."""
    path.parent.mkdir(parents=True, exist_ok=True)
    card = render().quantize(colors=256, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)
    card.save(path, format="PNG", optimize=True)
    return path.stat().st_size


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--out", type=Path, default=OG_CARD_PATH, help="where to write the PNG")
    args = parser.parse_args(argv)
    size = write(args.out)
    print(f"wrote {args.out} ({WIDTH}x{HEIGHT}, {size:,} bytes)")  # noqa: T201 — CLI summary line
    return 0 if size <= MAX_BYTES else 1


if __name__ == "__main__":
    raise SystemExit(main())
