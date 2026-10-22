"""Static assets are referenced with a content-hash query string (web/assets.py)."""

from __future__ import annotations

import re
from pathlib import Path

from fastapi.testclient import TestClient

from web.app import app as web_app
from web.assets import ASSET_VERSION, VERSIONED_ASSETS, compute_asset_version

VERSION_RE = re.compile(r"^[0-9a-f]{12}$")


def test_version_is_twelve_hex_and_changes_with_content(tmp_path: Path) -> None:
    assert VERSION_RE.match(ASSET_VERSION)
    for rel in VERSIONED_ASSETS:
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text("a")
    first = compute_asset_version(tmp_path)
    assert first == compute_asset_version(tmp_path), "same bytes, same version"
    (tmp_path / VERSIONED_ASSETS[-1]).write_text("b")
    assert compute_asset_version(tmp_path) != first, "a changed file must flip the version"


def test_rendered_pages_reference_versioned_assets() -> None:
    """The privacy page needs no API; it extends base.html, which links the stylesheet."""
    with TestClient(web_app) as client:
        html = client.get("/privacy").text
    assert f'/static/css/styles.css?v={ASSET_VERSION}"' in html
    assert '/static/css/styles.css"' not in html, "an unversioned reference would defeat the point"


def test_every_file_a_template_versions_is_in_the_version_hash() -> None:
    """A template that links `static/<file>?v={{ asset_version }}` relies on the hash covering that
    file; basemap.js and asset_map.js were left out (mobile-behaviour review, 2026-10-22)."""
    templates = Path(__file__).resolve().parent / "templates"
    linked: set[str] = set()
    for path in templates.rglob("*.html"):
        linked |= set(
            re.findall(r"/static/([\w/.-]+)\?v=\{\{ asset_version \}\}", path.read_text(encoding="utf-8"))
        )
    assert linked and linked <= set(VERSIONED_ASSETS), sorted(linked - set(VERSIONED_ASSETS))
