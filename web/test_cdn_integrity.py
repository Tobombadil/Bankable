"""Subresource Integrity on the third-party map files (audit 2026-10-07 UX-19).

MapLibre, PMTiles and the Protomaps basemaps load from cdn.jsdelivr.net on the map, asset and
company pages; before this none carried `integrity`, so a changed or compromised CDN copy would have
run on the site. The URLs and hashes now live in one table (`web/assets.py::CDN_ASSETS`) that the
templates render through two macros.
"""

from __future__ import annotations

import base64
import hashlib
import re
import urllib.request
from pathlib import Path

import pytest

from web.assets import CDN_ASSETS

WEB = Path(__file__).resolve().parent


def test_no_template_names_a_cdn_file_except_through_the_table() -> None:
    for path in (WEB / "templates").rglob("*.html"):
        text = path.read_text(encoding="utf-8")
        assert "cdn.jsdelivr.net" not in text, path.name


def test_every_entry_is_pinned_and_hashed() -> None:
    for name, entry in CDN_ASSETS.items():
        pinned = r"https://cdn\.jsdelivr\.net/npm/(@[\w-]+/)?[\w.-]+@\d+\.\d+\.\d+/"
        assert re.match(pinned, entry["url"]), name
        assert re.fullmatch(r"sha384-[A-Za-z0-9+/]{64}", entry["integrity"]), name


def test_the_macros_render_integrity_and_cors() -> None:
    macros = (WEB / "templates" / "_macros.html").read_text(encoding="utf-8")
    for macro in ("cdn_script", "cdn_style"):
        body = macros.split(f"{{% macro {macro}(name) -%}}")[1].split("{%- endmacro %}")[0]
        assert 'integrity="{{ cdn[name].integrity }}"' in body and 'crossorigin="anonymous"' in body
    for page in ("home_map.html", "asset_detail.html", "organization_detail.html"):
        text = (WEB / "templates" / page).read_text(encoding="utf-8")
        assert 'm.cdn_script("maplibre_js")' in text and 'm.cdn_style("maplibre_css")' in text, page


def test_the_e2e_stand_ins_serve_the_same_versions() -> None:
    e2e = (WEB / "test_e2e.py").read_text(encoding="utf-8")
    for constant, name in (("MAPLIBRE_VERSION", "maplibre_js"), ("PMTILES_VERSION", "pmtiles_js")):
        version = re.search(rf'^{constant} = "([\d.]+)"', e2e, re.M)
        assert version and f"@{version.group(1)}/" in CDN_ASSETS[name]["url"]


@pytest.mark.parametrize("name", sorted(CDN_ASSETS))
def test_the_recorded_hash_is_the_files_hash(name: str) -> None:
    """Needs the network; skipped without it. Run when a version changes."""
    try:
        with urllib.request.urlopen(CDN_ASSETS[name]["url"], timeout=20) as response:  # noqa: S310
            body = response.read()
    except Exception as exc:  # pragma: no cover - offline runners
        pytest.skip(f"CDN unreachable: {exc}")
    digest = base64.b64encode(hashlib.sha384(body).digest()).decode()
    assert CDN_ASSETS[name]["integrity"] == f"sha384-{digest}"
