"""Content-hash version string for the site's own static assets.

`base.html` and `home_map.html` reference `/static/css/styles.css` and `/static/js/map.js` with
`?v={{ asset_version }}` so that a browser which cached the previous build never keeps running it
against new markup. Without this, `StaticFiles` sends no `Cache-Control`, browsers apply heuristic
freshness (a tenth of the file's age since `Last-Modified`), and a checkout that sat unchanged for
days handed out a stale `map.js` for hours after `git pull` (owner's machine, 2026-09-15: the new
plants checkbox rendered from the new template while the old script, which knew nothing about it,
kept running). The digest is computed once at import from the files' bytes, so every process built
from the same tree agrees on it and every content change flips it.

Each web module keeps its own `Jinja2Templates` (see `web/auth.py`'s docstring), so each registers
`asset_version` from here rather than sharing an environment.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

STATIC_ROOT = Path(__file__).resolve().parent / "static"
VERSIONED_ASSETS: tuple[str, ...] = ("css/styles.css", "js/map.js")


def compute_asset_version(static_root: Path = STATIC_ROOT) -> str:
    """Twelve hex characters of a SHA-256 over the versioned assets' bytes, in a fixed order."""
    digest = hashlib.sha256()
    for rel in VERSIONED_ASSETS:
        digest.update(rel.encode())
        digest.update((static_root / rel).read_bytes())
    return digest.hexdigest()[:12]


ASSET_VERSION = compute_asset_version()
