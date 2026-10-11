"""The private beta's feedback link (2026-10-11; docs/64, docs/65): whether the site is in beta mode,
and the header link "Tell us what you were trying to do" that every public page shows while it is.

**The signal is the existing gate, not a new setting.** `SITE_ACCESS=basic` is what puts the beta's
one shared login in front of every host (`infra/compose/Caddyfile` `gate_basic`, docs/64 §1), and
opening the site to the public is `SITE_ACCESS=open` (docs/64 §6). The `web` service reads the same
secrets file through `env_file` (`infra/compose/compose.prod.yml`), so the link appears exactly while
the gate is on and disappears with it, with nothing else to remember to switch off. Read on every
call, like `WEB_DEV_PREVIEW`, so a test or a redeploy needs no restart of this module.

**Where the reader came from is a path, never a query string.** A filter or search a reader typed
lives in the query (`/proposals?q=...`, `/search?q=...`), and the site's tokens do too
(`/verify?token=`, `/reset-password?token=`). `site_path` keeps only a bare site path: cut at the
first `?` or `#`, one leading slash (never `//host`, never a backslash, which browsers read as a
slash), no whitespace or control character, at most `PATH_MAX` characters. The API applies the same
rule and refuses anything else (`services/api/admin_posts.py`, beta feedback note).

This module imports nothing from the rest of `web/`, so the four Jinja environments that render
`base.html` (`web/page.py`, `web/auth.py`, `web/legal.py`, `web/pricing.py`) can each register
`feedback_href` without an import cycle; the routes are in `web/feedback.py`.
"""

from __future__ import annotations

import os
import re
from urllib.parse import urlencode, urlsplit

from starlette.requests import Request

FEEDBACK_PATH = "/feedback"
#: `SITE_ACCESS`'s value while the beta's shared login is on (docs/64 §2 step 6).
BETA_ACCESS = "basic"
#: The API's `ReportRequest.page_path` maxLength (api/openapi.yaml).
PATH_MAX = 512
#: The API's `ReportRequest.page_path` pattern (api/openapi.yaml), as `services/api/admin_posts.py`
#: holds it: a bare site path.
_SITE_PATH = re.compile(r"^/(?:[^/?#\s\\\x00-\x1f\x7f][^?#\s\\\x00-\x1f\x7f]*)?$")


def beta_mode() -> bool:
    """True while the beta's shared login is on (`SITE_ACCESS=basic`)."""
    return os.environ.get("SITE_ACCESS", "").strip().lower() == BETA_ACCESS


def site_path(value: str | None) -> str | None:
    """`value` as a bare site path without its query string or fragment, or `None` when what is
    left is not one (an absolute URL, `//host`, a backslash, whitespace, too long)."""
    if not value:
        return None
    path = value.split("#", 1)[0].split("?", 1)[0]
    if len(path) > PATH_MAX or not _SITE_PATH.match(path):
        return None
    return path


def came_from(request: Request) -> str | None:
    """The page a reader opened the form from: the header link's `from`, or else a same-origin
    `Referer`, as a path only. Never the form itself."""
    path = site_path(request.query_params.get("from"))
    if path is None:
        referer = urlsplit(request.headers.get("referer") or "")
        if (referer.scheme, referer.netloc) == (request.url.scheme, request.url.netloc):
            path = site_path(referer.path)
    return None if path == FEEDBACK_PATH else path


def feedback_href(request: Request) -> str | None:
    """The header link's address on this page, or `None` outside beta mode (no link at all)."""
    if not beta_mode():
        return None
    here = site_path(request.url.path)
    if here is None or here == FEEDBACK_PATH:
        return FEEDBACK_PATH
    return f"{FEEDBACK_PATH}?{urlencode({'from': here})}"


__all__ = ["FEEDBACK_PATH", "PATH_MAX", "beta_mode", "came_from", "feedback_href", "site_path"]
