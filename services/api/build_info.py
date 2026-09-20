"""Which build is running, and how fresh its data is.

Two different questions get confused as "am I seeing the latest version?", and the answer needs
both halves:

* **Which code** is serving the request — the commit. In a container this is stamped at build time
  into `GIT_SHA` (`.github/workflows/release.yml` passes the tag it pushes); on a developer's
  machine there is no such variable, so it is read from the working tree once per process. A
  developer who has edited files since that commit is told so (`dirty`), because "the site shows
  commit X" is misleading when the tree no longer matches X.
* **Which data** it is serving — the newest `retrieved_at` across the sources actually loaded,
  which moves independently: the dev runner loads a snapshot and then serves it for days, so a
  perfectly current build can be showing week-old rows.

Everything here is best-effort and never raises: a missing `.git`, no `git` on the PATH, a
read-only checkout and an empty database all resolve to `None`, and the caller renders what it
has. This is diagnostic information, not a contract.
"""

from __future__ import annotations

import datetime as dt
import functools
import logging
import os
import pathlib
import shutil
import subprocess
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

#: Environment variables an image build or deploy may stamp the commit into, in the order tried.
#: `GIT_SHA` is what this repository's release workflow sets; the others are conventions from
#: common build platforms, accepted so a deploy that already sets one needs no extra wiring.
COMMIT_ENV_VARS = ("GIT_SHA", "SOURCE_COMMIT", "GIT_COMMIT", "VERCEL_GIT_COMMIT_SHA")

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
log = logging.getLogger(__name__)


def _git(*args: str) -> str | None:
    """`git` output, or `None` for any reason at all -- no git binary, no repository, a permission
    error, a timeout. Never raises: this is a footer line, not a health check."""
    git = shutil.which("git")
    if git is None or not (_REPO_ROOT / ".git").exists():
        return None
    try:
        result = subprocess.run(  # noqa: S603 -- fixed argv, no shell, repository root only
            [git, "-C", str(_REPO_ROOT), *args],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


@functools.lru_cache(maxsize=1)
def build_info() -> dict[str, Any]:
    """`{"commit": str | None, "commit_source": str, "dirty": bool | None}`, resolved once per
    process. The commit is short (the first 12 characters), long enough to be unambiguous in a
    repository this size and short enough to read from a footer."""
    for name in COMMIT_ENV_VARS:
        value = (os.environ.get(name) or "").strip()
        if value:
            return {"commit": value[:12], "commit_source": name, "dirty": None}
    commit = _git("rev-parse", "HEAD")
    if commit is None:
        return {"commit": None, "commit_source": "unavailable", "dirty": None}
    status = _git("status", "--porcelain")
    return {
        "commit": commit[:12],
        "commit_source": "working-tree",
        # `status` is "" for a clean tree and None when the call failed; only the first is a
        # trustworthy "clean".
        "dirty": bool(status) if status is not None else None,
    }


def data_as_of(db: Session) -> str | None:
    """The newest `retrieved_at` across loaded source records, as an ISO instant, or `None` when
    nothing is loaded. This is the data's own vintage -- how recently the platform *fetched* from
    a source -- and is deliberately not the publish lag, which `/v1/health` reports separately."""
    from services.db.models import OpportunitySource, ProposalSource

    newest: dt.datetime | None = None
    for model in (ProposalSource, OpportunitySource):
        try:
            value = db.scalar(select(func.max(model.retrieved_at)))
        except Exception:
            log.debug("data_as_of: %s unreadable, skipping", model.__name__, exc_info=True)
            continue
        if value is not None and (newest is None or value > newest):
            newest = value
    if newest is None:
        return None
    if newest.tzinfo is None:
        newest = newest.replace(tzinfo=dt.UTC)
    return newest.isoformat().replace("+00:00", "Z")
