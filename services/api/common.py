"""Small shared helpers: the `infraque.com` placeholder, request ids, ISO timestamps.

`api/openapi.yaml` keeps the literal `infraque.com` token everywhere a hostname is unavoidable —
even inside the `Problem.type` regex — because the product name is not yet chosen
(`docs/00-PLAN.md`; `docs/04-standards.md` §0.6). This service does the same rather than inventing
a placeholder of its own, so responses validate against the committed contract byte-for-byte.
"""

from __future__ import annotations

import datetime as dt
import secrets
from urllib.parse import urlparse

DOMAIN = "infraque.com"
API_HOST = f"https://api.{DOMAIN}"
WEB_HOST = f"https://{DOMAIN}"
TERMS_URL = f"{WEB_HOST}/legal/api-licence"

_REQUEST_ID_ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyz"


def new_request_id() -> str:
    """`^req_[0-9a-z]{6,32}$` (api/openapi.yaml `X-Request-Id`)."""
    body = "".join(secrets.choice(_REQUEST_ID_ALPHABET) for _ in range(16))
    return f"req_{body}"


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def ensure_aware(value: dt.datetime) -> dt.datetime:
    """SQLite has no real `timestamptz`: a `DateTime(timezone=True)` column round-trips as a
    naive `datetime` there (Postgres would keep it aware). Treat a naive value as UTC — every
    write in this service already stores UTC-aware datetimes (docs/04 E-4)."""
    return value if value.tzinfo is not None else value.replace(tzinfo=dt.UTC)


def iso(value: dt.datetime | dt.date | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=dt.UTC)
        return value.astimezone(dt.UTC).isoformat().replace("+00:00", "Z")
    return value.isoformat()


def normalise_domain(website: str | None) -> str | None:
    """Strip scheme, `www.`, path and port; lowercase; `None` if unusable (docs/34 §5 "upsert by
    primary domain"). Shared by `services/crm/router.py` and `services/api/admin_people.py` /
    `services/api/admin_intake.py`, which previously each carried their own identical copy
    (docs/42-backend-review-2026-09-26.md §2 item 3)."""
    if not website:
        return None
    candidate = website.strip()
    if not candidate:
        return None
    if "://" not in candidate:
        candidate = f"//{candidate}"
    host = urlparse(candidate).netloc
    host = host.rsplit("@", 1)[-1]
    host = host.split(":", 1)[0].lower()
    if host.startswith("www."):
        host = host[4:]
    return host or None
