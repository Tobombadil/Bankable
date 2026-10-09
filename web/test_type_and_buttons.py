"""Type floor, focus outline and one button set (audit 2026-10-07 UX-15, UX-16; docs/31 §1.3, §5.16, §7).

Before: 0.7rem (11.2px) mono labels in eight rules, under docs/31 §1.3's `--text-1` floor; the
filter inputs and the site search dropped the 2px focus outline for a 1px underline change; at least
six button treatments across public and admin pages; admin pages preloaded no font.
"""

from __future__ import annotations

import re
from pathlib import Path

WEB = Path(__file__).resolve().parent
CSS = (WEB / "static" / "css" / "styles.css").read_text(encoding="utf-8")
ADMIN_CSS = (WEB / "static" / "css" / "admin.css").read_text(encoding="utf-8")


def _rule(css: str, selector: str) -> str:
    start = css.index(selector + " {")
    return css[start : css.index("}", start)]


def test_no_type_below_the_text_1_floor() -> None:
    assert "0.7rem" not in CSS
    sizes = set(re.findall(r"font-size:\s*([0-9.]+(?:rem|px))", CSS))
    assert not {s for s in sizes if s.endswith("rem") and float(s[:-3]) < 0.8}, sizes


def test_inputs_keep_the_focus_outline() -> None:
    assert "outline: none" not in CSS and "outline:none" not in CSS
    assert ":focus-visible {\n  outline: 2px solid var(--focus-ring);" in CSS


def test_secondary_buttons_are_sentence_case_outlined() -> None:
    for selector in (
        ".site-search button",
        ".filter-bar__submit",
        ".in-view-list .details-btn",
        ".map-drawer__close",
    ):
        rule = _rule(CSS, selector)
        assert "uppercase" not in rule, selector
    for selector in (".site-search button", ".filter-bar__submit"):
        rule = _rule(CSS, selector)
        assert "border: 1px solid var(--link)" in rule and "background: transparent" in rule


def test_admin_buttons_reuse_the_public_pair() -> None:
    button = ADMIN_CSS.split(".admin-button {")[1].split("}")[0]
    assert "border: 1px solid var(--link)" in button and "text-decoration: none" in button
    primary = ADMIN_CSS.split(".admin-button--primary, .admin-button--primary:visited {")[1].split("}")[0]
    assert "background: var(--link)" in primary
    assert "--color-ink); color: var(--color-paper)" not in ADMIN_CSS


def test_admin_pages_preload_the_first_paint_fonts() -> None:
    admin_base = (WEB / "templates" / "admin" / "base.html").read_text(encoding="utf-8")
    public_base = (WEB / "templates" / "base.html").read_text(encoding="utf-8")
    preload = re.compile(r'<link rel="preload" href="(/static/fonts/[^"]+)" as="font"')
    assert preload.findall(admin_base) == preload.findall(public_base) != []


def test_no_escaped_entity_literals_inside_expressions() -> None:
    """Audit 2026-10-07 UX-22: `{{ x or '&mdash;' }}` is autoescaped and printed "&mdash;" as text
    on 47 admin fields ("Storage MWh &mdash;")."""
    for path in (WEB / "templates").rglob("*.html"):
        text = path.read_text(encoding="utf-8")
        assert "'&mdash;'" not in text and '"&mdash;"' not in text, path.name
