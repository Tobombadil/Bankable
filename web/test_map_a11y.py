"""Map accessibility and correctness fixes from the 2026-09-30 audit, held at source level (the
behaviour itself is driven in `web/test_e2e.py`):

* F3: only the newest proposals response is applied;
* F4: "in view" is counted from what is drawn, lines are named by type, a capped line set says so;
* F5 / D-13: the closed drawer is `hidden` and not `aria-modal`; a skip link reaches the results;
  proposal rows open the drawer from the keyboard; the drawer names the licence;
* F11 / D-2: markers are chip pairs (no white text on a pale fill), measured here in both themes;
  lifecycle is carried by glyph as well as hue, and the legend is exposed and names states;
* D-12: the basemap flavour follows the theme and land cover is muted to the tokens;
* docs/60 §11 item 10 (axe on `/`, 2026-09-27): the status chip's words clear 4.5:1 on its fill in
  every theme (`color-contrast` on `.chip--neutral .chip__label` was 4.47:1), and `#in-view-items`
  only ever holds plain list items (`list`: the group names were `role="presentation"` items).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parent
MAP_JS = (WEB / "static" / "js" / "map.js").read_text(encoding="utf-8")
BASEMAP_JS = (WEB / "static" / "js" / "basemap.js").read_text(encoding="utf-8")
CSS = (WEB / "static" / "css" / "styles.css").read_text(encoding="utf-8")
HOME = (WEB / "templates" / "home_map.html").read_text(encoding="utf-8")
FAMILIES = ("neutral", "progress", "committed", "success", "danger")


def _block(start: str) -> str:
    """The custom properties of the rule that begins at `start` (up to its closing brace)."""
    i = CSS.index(start)
    return CSS[i : CSS.index("}", i)]


def _tokens(block: str) -> dict[str, str]:
    return dict(re.findall(r"(--[a-z0-9-]+):\s*(#[0-9a-fA-F]{6})", block))


def _luminance(hex_colour: str) -> float:
    channels = [int(hex_colour[i : i + 2], 16) / 255 for i in (1, 3, 5)]
    linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast(a: str, b: str) -> float:
    la, lb = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


THEMES = {
    "light": _tokens(_block(":root {")),
    "dark (media)": {**_tokens(_block(":root {")), **_tokens(_block(':root:not([data-theme="light"]) {'))},
    "dark (toggle)": {**_tokens(_block(":root {")), **_tokens(_block(':root[data-theme="dark"] {'))},
}


@pytest.mark.parametrize("theme", sorted(THEMES))
@pytest.mark.parametrize("family", FAMILIES)
def test_marker_glyph_and_ring_meet_aa_on_their_fill_and_the_land(theme: str, family: str) -> None:
    """A proposal marker is the status chip in miniature: the family's fill with the family's text
    colour for ring and glyph (map.js `addFamilyImages`). Measured, not assumed: the glyph is a
    graphical object (SC 1.4.11, 3:1) and the chip pair also carries chip text (SC 1.4.3, 4.5:1);
    the ring must separate the marker from the land (3:1)."""
    tokens = THEMES[theme]
    text, fill, land = (
        tokens[f"--family-{family}-text"],
        tokens[f"--family-{family}-fill"],
        tokens["--map-land"],
    )
    assert _contrast(text, fill) >= 4.5, (theme, family, round(_contrast(text, fill), 2))
    assert _contrast(text, land) >= 3.0, (theme, family, round(_contrast(text, land), 2))


def test_markers_draw_no_white_text_or_stroke() -> None:
    """F11: badge text and stroke were `#ffffff` on every theme, 1.5-2.2:1 on the dark-mode fills."""
    assert '"#ffffff"' not in MAP_JS
    assert '"text-color": cssVar("--text")' in MAP_JS
    assert 'cssVar("--family-" + family + "-fill")' in MAP_JS


def test_points_and_clusters_carry_the_family_glyph() -> None:
    """D-2: shape as well as hue. Points are family markers, clusters show the dominant family's
    glyph above the count, and the tech badge waits for z9."""
    assert '"icon-image": ["concat", "family-marker-", ["get", "family"]]' in MAP_JS
    assert '"icon-image": ["concat", "family-glyph-", ["get", "family"]]' in MAP_JS
    assert 'id: "point-labels", type: "symbol", source: "proposals", minzoom: 9' in MAP_JS
    for family in FAMILIES:
        assert f'family === "{family}"' in MAP_JS or family == "danger"


def test_the_legend_is_exposed_names_states_and_tracks_what_is_drawn() -> None:
    legend = HOME.split('id="lifecycle-legend"')[1].split("</div>")[0]
    assert 'aria-hidden="true">' not in legend.split(">")[0]
    assert 'role="group" aria-label="Proposal status key"' in legend
    for words in (
        "Announced",
        "In process: filed, studied, permitted, under construction",
        "Contracted",
        "Built",
    ):
        assert words in legend
    for family in FAMILIES:
        assert f'data-legend-family="{family}"' in legend
    assert "Neutral<" not in legend and "Progress<" not in legend and "Danger<" not in legend
    assert "updateLifecycleLegend((fc.totals || {}).lifecycle_state_counts || {});" in MAP_JS


def test_a_stale_proposals_response_is_dropped() -> None:
    """F3: the same sequence guard `refetchAssets` already had, plus an abort of the superseded call."""
    body = MAP_JS.split("function refetch() {")[1].split("\n  }\n")[0]
    assert "var seq = ++proposalsFetchSeq;" in body
    assert "if (seq !== proposalsFetchSeq) return;" in body
    assert "proposalsAbort.abort()" in body


def test_in_view_counts_what_is_drawn_and_names_line_types() -> None:
    """F4: `totals.records` is dataset-wide; the live region counts clusters and points in the
    response, names each line type, and says when the line set was capped."""
    render = MAP_JS.split("function render() {")[1]
    assert 'var liveText = (totals.records || 0) + " proposals in view."' not in MAP_JS
    assert 'if (kind === "cluster") proposalsInView += Number(f.properties.count) || 0;' in render
    assert 'pipeline" + (lines === 1' not in MAP_JS
    assert "ASSET_TYPES[type].plural" in render
    assert "lt.lines_shown < lt.line_count" in render
    assert 'id="lines-note"' in HOME


def test_the_closed_drawer_is_out_of_the_page() -> None:
    """F5: hidden (no tab stop, no accessibility node) and `aria-modal` only while open."""
    drawer = MAP_JS.split("function buildDrawer() {")[1]
    assert 'el.setAttribute("aria-modal", "true");\n    el.setAttribute("aria-label"' not in drawer
    assert "el.hidden = true;" in drawer
    open_fn = drawer.split("function open() {")[1].split("\n    }\n")[0]
    close_fn = drawer.split("function close() {")[1].split("\n    }\n")[0]
    assert "el.hidden = false;" in open_fn and 'el.setAttribute("aria-modal", "true");' in open_fn
    assert 'el.removeAttribute("aria-modal");' in close_fn and 'el.setAttribute("inert", "");' in close_fn


def test_keyboard_path_to_results_and_the_drawer() -> None:
    """D-13: a skip link to the in-view list, a Details button on every proposal row, and the
    drawer's source line names the licence class."""
    assert '<a class="skip-link" href="#in-view-list">Skip to results in view</a>' in HOME
    assert (
        '<button type="button" class="details-btn">Details</button>'
        in HOME.split('id="in-view-item-template"')[1]
    )
    assert 'details.addEventListener("click", function () { openDrawer(p); });' in MAP_JS
    assert "reuseClassName(source.reuse_class)" in MAP_JS


def test_the_basemap_follows_the_theme() -> None:
    """D-12: the dark theme starts from the dark flavour and sprite; land cover and area fills are
    the land token in both themes, so national zoom is not pale land beside dark water."""
    assert "basemaps.namedFlavor(colors.flavor)" in BASEMAP_JS
    assert 'PROTOMAPS_SPRITE_BASE + (colors.flavor || "light")' in BASEMAP_JS
    assert "Object.keys(flavor.landcover).forEach" in BASEMAP_JS
    assert 'namedFlavor("light")' not in BASEMAP_JS
    assert CSS.count("--map-flavor: dark;") == 2 and CSS.count("--map-flavor: light;") == 1


# ---- audit 2026-10-07 UX-5: the region fills are explained, and a state is never a solid fill ----


def test_region_fills_are_stepped_by_their_own_count_and_states_are_outlines() -> None:
    """A lone state-placed proposal used to paint all of Indiana slate grey (opacity scaled against
    the busiest region in view, 0.15..0.7). Counties now step by their own count up to 0.4; a state
    or country is a near-transparent wash under a dashed outline."""
    assert "0.15 + ratio * 0.55" not in MAP_JS
    body = MAP_JS.split("function regionOpacity(level, count) {")[1].split("\n  }\n")[0]
    assert 'if (level !== "county") return REGION_BROAD_OPACITY;' in body
    assert "n >= 50 ? 0.4" in body
    assert "var REGION_BROAD_OPACITY = 0.04;" in MAP_JS
    assert 'id: "region-outline-broad"' in MAP_JS and '"line-dasharray": [3, 2]' in MAP_JS
    assert 'region_grade: f.properties.region_level === "county" ? "area" : "broad"' in MAP_JS


def test_the_legend_names_the_region_areas_and_map_js_shows_it_only_when_drawn() -> None:
    legend = HOME.split('id="lifecycle-legend"')[1].split("</div>")[0]
    row = legend.split('id="region-legend"')[1].split("</span>")[0]
    assert "shaded county" in row and "dashed state or country outline" in row and "how many" in row
    assert "legendRow.hidden = !regionFeatures.length;" in MAP_JS
    tokens = THEMES["light"]
    assert _contrast(tokens["--region-line"], tokens["--color-paper"]) >= 4.5  # the row's words


def test_regions_are_listed_busiest_first_and_states_by_name() -> None:
    from web.labels import map_labels

    assert map_labels()["region"]["US-IN"] == "Indiana"
    assert "return (Number(b.properties.count) || 0) - (Number(a.properties.count) || 0);" in MAP_JS
    assert "var regionNames = SERVER_LABELS.region || {};" in MAP_JS


# ------------------------------------------- docs/60 §11 item 10: the two serious axe findings on `/`
@pytest.mark.parametrize("theme", sorted(THEMES))
@pytest.mark.parametrize("family", FAMILIES)
def test_status_chip_words_meet_aa_on_the_chip_fill(theme: str, family: str) -> None:
    """The chip's label is the family text token on the family fill (styles.css `.chip--{family}`),
    12.8px at weight 600: normal-size text, so 4.5:1 (SC 1.4.3). Neutral measured 4.47:1 under axe
    with docs/31 §1.2's `#5b6b7c`; the token is `#4f5e6e` since, 5.44:1 (dark: 7.88:1)."""
    rule = CSS.split(f".chip--{family} ")[1].split("}")[0]
    assert f"background: var(--family-{family}-fill)" in rule
    assert f"color: var(--family-{family}-text)" in rule
    tokens = THEMES[theme]
    text, fill = tokens[f"--family-{family}-text"], tokens[f"--family-{family}-fill"]
    assert _contrast(text, fill) >= 4.5, (theme, family, round(_contrast(text, fill), 2))


def test_the_neutral_chip_holds_the_measured_fix() -> None:
    light = THEMES["light"]
    assert round(_contrast("#5b6b7c", light["--family-neutral-fill"]), 2) == 4.47  # what axe flagged
    assert round(_contrast(light["--family-neutral-text"], light["--family-neutral-fill"]), 2) >= 5.4
    # The map key prints the same token on the page ground (home_map.html, legend row "Announced").
    for theme in THEMES.values():
        ground = theme.get("--bg", theme["--color-paper"])
        assert _contrast(theme["--family-neutral-text"], ground) >= 4.5


def _nearest_declaration(name: str, before: int) -> str:
    """The right-hand side of the last `var <name> = ...;` above `before` in map.js."""
    found = list(re.finditer(rf"var {name} = ([^;]+);", MAP_JS[:before]))
    assert found, name
    return found[-1].group(1)


def test_the_in_view_list_only_ever_holds_plain_list_items() -> None:
    """axe `list` (serious): a <ul> may hold only <li> (or script/template) children, and an <li>
    given another role, as the group names once were (`role="presentation"`), no longer counts as
    one. Every node map.js puts in `#in-view-items` is created as an <li> or is the row template,
    and nothing in map.js sets a role other than the drawer's."""
    assert '<ul class="in-view-list" id="in-view-items"></ul>' in HOME  # empty until map.js fills it
    assert 'var listEl = document.getElementById("in-view-items");' in MAP_JS
    appended = list(re.finditer(r"listEl\.appendChild\((\w+)\)", MAP_JS))
    assert len(appended) >= 8
    for match in appended:
        source = _nearest_declaration(match.group(1), match.start())
        assert source in ('document.createElement("li")', "template.content.cloneNode(true)"), (
            match.group(1),
            source,
        )
    template = HOME.split('<template id="in-view-item-template">')[1].split("</template>")[0]
    top_level = re.findall(r"^  <(/?)(\w+)([^>]*)>", template, re.M)
    assert [(close, tag) for close, tag, _ in top_level] == [("", "li"), ("/", "li")]
    assert "role=" not in top_level[0][2]
    assert set(re.findall(r'setAttribute\("role", "(\w+)"\)', MAP_JS)) == {"dialog"}
    assert ".role =" not in MAP_JS
