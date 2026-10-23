"""Load a private checklist content pack at deploy (docs/19 §2.3; decision row 2026-10-22).

    python -m services.projects.load_checklist --pack-url "$CHECKLIST_PACK_URL"

Detailed checklist content (guidance per item, per-sector variants, examples) stays out of this
public repository. It is a pack in the public schema (`data/checklist/pack.schema.json`), kept in a
private object or the owner's private repository; `CHECKLIST_PACK_URL` is a SOPS-managed secret
(docs/60). This command fetches it, validates it against the schema and the generic pack (every key
must exist in `generic-v1` and keep its folder; an unmatched key is refused) and writes its rows to
`checklist_template_item` under its `pack_version`, in one transaction.

**A no-op without a URL.** With neither `--pack-url` nor `CHECKLIST_PACK_URL`, it logs that the
generic names stay in use and exits 0 without touching the database.

**Nothing private is logged.** The URL may carry a signature, and the pack is private content, so
logs name only the pack version, the item count and, on failure, where the pack failed (a JSON
path and the rule broken), never a value. httpx logs every request's full URL at `INFO` (its
`httpx` logger), so the fetch raises the `httpx` and `httpcore` loggers to `WARNING` while it runs.
An unexpected exception is logged by type only. `https://` and local files (`file://` or a path)
are accepted; plain `http://` is refused.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import logging
import os
import pathlib
import sys
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Final
from urllib.parse import urlsplit

import yaml
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from services.projects.checklist import ChecklistPack, PackError, generic_pack, validate_pack
from services.projects.models import ChecklistTemplateItem, utcnow

log = logging.getLogger("services.projects.load_checklist")

ENV_PACK_URL: Final = "CHECKLIST_PACK_URL"
MAX_PACK_BYTES: Final = 5_000_000
FETCH_TIMEOUT_SECONDS: Final = 30.0
#: The HTTP client's loggers: `httpx` logs "HTTP Request: GET <full URL>" at INFO, and `httpcore`
#: traces connections at DEBUG.
HTTP_CLIENT_LOGGERS: Final = ("httpx", "httpcore")


@contextlib.contextmanager
def _http_client_logs_quiet() -> Iterator[None]:
    """Raises the HTTP client's loggers to at least `WARNING` for the fetch and restores them after,
    so the signed pack URL never reaches a log, whatever the process configured."""
    loggers = [logging.getLogger(name) for name in HTTP_CLIENT_LOGGERS]
    saved = [logger.level for logger in loggers]
    for logger in loggers:
        logger.setLevel(max(logger.level, logging.WARNING))
    try:
        yield
    finally:
        for logger, level in zip(loggers, saved, strict=True):
            logger.setLevel(level)


@dataclass(frozen=True)
class LoadResult:
    pack_version: str
    items: int
    removed: int


def fetch_pack(url: str, *, max_bytes: int = MAX_PACK_BYTES) -> bytes:
    """The pack's bytes from `https://…`, `file://…` or a local path. Errors name the failure, never
    the URL."""
    try:
        parts = urlsplit(url)
    except ValueError:
        raise PackError("the pack URL is not a valid URL") from None
    if parts.scheme in ("", "file"):
        path = pathlib.Path(parts.path if parts.scheme == "file" else url)
        if not path.is_file():
            raise PackError("the pack file does not exist")
        if path.stat().st_size > max_bytes:
            raise PackError(f"the pack is larger than {max_bytes} bytes")
        return path.read_bytes()
    if parts.scheme != "https":
        raise PackError("the pack URL must be https:// or a local file")
    import httpx

    try:
        with (
            _http_client_logs_quiet(),
            httpx.Client(timeout=FETCH_TIMEOUT_SECONDS, follow_redirects=False) as client,
            client.stream("GET", url) as response,
        ):
            if response.status_code != 200:
                raise PackError(f"fetching the pack answered HTTP {response.status_code}")
            body = bytearray()
            for chunk in response.iter_bytes():
                body.extend(chunk)
                if len(body) > max_bytes:
                    raise PackError(f"the pack is larger than {max_bytes} bytes")
            return bytes(body)
    # `InvalidURL` and `StreamError` are not `HTTPError`s; their messages may quote the URL.
    except (httpx.HTTPError, httpx.InvalidURL, httpx.StreamError) as exc:
        raise PackError(f"fetching the pack failed ({type(exc).__name__})") from None


def parse_pack(raw: bytes) -> object:
    """YAML or JSON (JSON is YAML). Parse errors are reported without the offending text."""
    try:
        return yaml.safe_load(raw.decode("utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError):
        raise PackError("the pack is not UTF-8 YAML or JSON") from None


def load_pack(db: Session, document: object, *, now: dt.datetime | None = None) -> LoadResult:
    """Validates `document` and writes its rows under its `pack_version`, replacing any rows that
    version already had. The newest loaded pack becomes the active one (`checklist.active_pack`).
    Projects keep the items they were created with; guidance is read live, so it reaches them."""
    pack: ChecklistPack = validate_pack(document, generic=generic_pack())
    now = now or utcnow()
    existing = {
        row.key: row
        for row in db.execute(
            select(ChecklistTemplateItem).where(ChecklistTemplateItem.pack_version == pack.pack_version)
        ).scalars()
    }
    stale = sorted(set(existing) - pack.keys())
    if stale:
        db.execute(
            delete(ChecklistTemplateItem).where(
                ChecklistTemplateItem.pack_version == pack.pack_version, ChecklistTemplateItem.key.in_(stale)
            )
        )
    for item in pack.items:
        row = existing.get(item.key) or ChecklistTemplateItem(pack_version=pack.pack_version, key=item.key)
        row.folder = item.folder
        row.pillar = item.pillar
        row.name = item.name
        row.guidance = item.guidance
        row.sort_order = item.sort_order
        row.technologies = list(item.technologies)
        row.loaded_at = now
        db.add(row)
    db.flush()
    return LoadResult(pack_version=pack.pack_version, items=len(pack.items), removed=len(stale))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m services.projects.load_checklist", description=__doc__)
    parser.add_argument(
        "--pack-url",
        default=os.environ.get(ENV_PACK_URL) or None,
        help=f"https:// URL or local path of the private pack (default: ${ENV_PACK_URL})",
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="SQLAlchemy URL (default: $DATABASE_URL, as every other job reads it)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    args = _parser().parse_args(argv)
    if not args.pack_url:
        log.info("no %s configured; the generic checklist names stay in use", ENV_PACK_URL)
        return 0
    database_url = args.database_url or os.environ.get("DATABASE_URL")
    if not database_url:
        log.error("checklist pack not loaded: no --database-url and no DATABASE_URL")
        return 2
    from sqlalchemy.exc import SQLAlchemyError

    from services.db.session import get_engine, get_sessionmaker, session_scope

    try:
        document = parse_pack(fetch_pack(args.pack_url))
        engine = get_engine(database_url)
        try:
            with session_scope(get_sessionmaker(engine)) as db:
                result = load_pack(db, document)
        finally:
            engine.dispose()
    except PackError as exc:
        log.error("checklist pack refused: %s", exc)
        return 1
    except SQLAlchemyError as exc:
        # The statement's parameters would carry the pack's text: name the failure only.
        log.error("checklist pack not loaded: database error (%s)", type(exc).__name__)
        return 1
    except Exception as exc:
        log.error("checklist pack not loaded: unexpected error (%s)", type(exc).__name__)
        return 1
    log.info(
        "checklist pack %s loaded: %d items, %d removed", result.pack_version, result.items, result.removed
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through main() in the tests
    sys.exit(main())
