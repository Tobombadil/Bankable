"""Shared plumbing for the ISO queue connectors (CAISO, ERCOT, NYISO). The workbook parsers are
gridstatus 0.36.0's, vendored in `pipeline/vendor/gridstatus` (BSD 3-Clause) so the library and the
dependency pins it carried are gone; each connector fetches the bytes politely and `parse` runs the
vendored parser over them, so it is pure and runs unchanged on recorded fixtures.

`fetch_workbook` (2026-10-23, lane national-queues) is the CAISO and NYISO fetch: one GET, made
conditional on the validators the previous snapshot recorded, so a daily poll of a report that has
not changed costs a 304 and reuses the stored bytes. A refusal names the host's own answer (status,
`Server`, `Via`, `X-Cache`, the CDN's request id and the first words of the body) in the error the
run records, because production showed both reports with no successful run and nothing to say why:
`source.last_error` read only "GET ... -> HTTP 403" at best. A 401, 403 or 451 is recorded as
`blocked` (`BlockedError`), the class a refusal by the host belongs to; any other status is a
failure. A 5xx or 429 that outlasts `PoliteSession`'s retries, and a challenge page, are described
the same way (the last answer is kept by a `requests` response hook), instead of the session's bare
"HTTP 503". Nothing here retries a refusal or changes how the request looks to get past one."""

from __future__ import annotations

import datetime as dt
import json
import re
import time
from collections.abc import Callable, Mapping
from typing import Any

import pandas as pd

from pipeline.connectors.base import BlockedError, Connector, ConnectorError, ParseError, RawSnapshot
from pipeline.connectors.canonical import harmonise_iso_frame, normalize_iso
from pipeline.connectors.http import HttpBlocked, HttpFailed
from pipeline.vendor.gridstatus import queues

XLSX_MAGIC = b"PK"
XLSX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

#: Connector-facing ISO name -> vendored parser (the names gridstatus's classes had).
PARSERS: dict[str, Callable[[bytes], pd.DataFrame]] = {
    "CAISO": queues.caiso_queue,
    "Ercot": queues.ercot_queue,
    "NYISO": queues.nyiso_queue,
}

#: Statuses that mean the host refused us, as opposed to failing: recorded `blocked`.
REFUSAL_STATUSES = frozenset({401, 403, 451})
#: Response headers that say which edge answered and why (CloudFront, Cloudflare, Akamai, Imperva).
_DIAGNOSTIC_HEADERS = (
    "server",
    "via",
    "x-cache",
    "x-amz-cf-pop",
    "x-amz-cf-id",
    "cf-ray",
    "cf-mitigated",
    "x-iinfo",
    "akamai-grn",
    "content-type",
)
_WS = re.compile(r"\s+")
_TAG = re.compile(r"<[^>]+>")


def queue_rows(iso_name: str, raw: RawSnapshot) -> list[dict[str, Any]]:
    """The vendored gridstatus queue parser for `iso_name` over `raw.content`, as row dicts."""
    if not raw.content.startswith(XLSX_MAGIC):
        raise ParseError(f"{raw.url} is not an xlsx (first bytes {raw.content[:16]!r})")
    try:
        df = PARSERS[iso_name](raw.content)
    except queues.QueueLayoutError as e:
        raise ParseError(f"{raw.url}: {e}") from e
    df.columns = [str(c) for c in df.columns]
    return [{str(k): v for k, v in r.items()} for r in df.to_dict("records")]


def normalize_iso_rows(
    connector: Connector, key: str, rows: list[dict[str, Any]], raw: RawSnapshot
) -> pd.DataFrame:
    df = normalize_iso(pd.DataFrame(rows), key, connector.status_map, raw.retrieved_at_iso)
    df = df.drop(columns=["source_url"])  # finalize stamps the fetched file URL
    return connector.finalize(df, rows, raw)


def restate_iso_status(connector: Connector, key: str, df: pd.DataFrame) -> pd.DataFrame | None:
    """`Connector.restate_status` for an ISO queue: re-harmonise each stored row's
    own `raw` payload (the parser's row, kept by `finalize`) under the connector's current status
    map. None when the frame carries no `raw` column, so the caller diffs it as stored."""
    if "raw" not in df.columns:
        return None
    rows = [json.loads(r) if isinstance(r, str) and r else {} for r in df["raw"]]
    harmonised = harmonise_iso_frame(pd.DataFrame(rows, index=df.index), key, connector.status_map)
    return pd.DataFrame(
        {"lifecycle_state": [s for s, _ in harmonised], "status_rule": [r for _, r in harmonised]},
        index=df.index,
    )


# ---------------------------------------------------------------------------------- fetching
def describe_response(status: int, headers: Mapping[str, Any], content: bytes) -> str:
    """One line naming who answered and what they said: the status, the edge headers in
    `_DIAGNOSTIC_HEADERS` that are present, and the first 160 characters of a text body with its
    markup stripped. The whole of what `source.last_error` will say about a refusal."""
    lower = {str(k).lower(): str(v) for k, v in dict(headers).items()}
    parts = [f"HTTP {status}"]
    parts += [f"{name}={lower[name]}" for name in _DIAGNOSTIC_HEADERS if lower.get(name)]
    ctype = lower.get("content-type", "").lower()
    if content and ("text" in ctype or "html" in ctype or "xml" in ctype or "json" in ctype):
        text = _WS.sub(" ", _TAG.sub(" ", content[:4000].decode("utf-8", "replace"))).strip()
        if text:
            parts.append(f"body={text[:160]!r}")
    return "; ".join(parts)


def stored_validators(connector: Connector, url: str) -> dict[str, str]:
    """Conditional-request headers from the previous snapshot of this source, when it was fetched
    from `url` and recorded a validator; empty otherwise (ask unconditionally)."""
    previous = connector.previous
    if previous is None:
        return {}
    snapshot = previous.record.get("snapshot") or {}
    meta = previous.meta
    if str(meta.get("requested_url") or snapshot.get("fetched_url") or "") != url:
        return {}
    headers: dict[str, str] = {}
    if meta.get("etag"):
        headers["If-None-Match"] = str(meta["etag"])
    if meta.get("last_modified"):
        headers["If-Modified-Since"] = str(meta["last_modified"])
    return headers


def _reusable(connector: Connector) -> bytes | None:
    """The stored workbook a 304 points at, or None when it cannot be read, fails its SHA-256 or is
    not an xlsx (the caller then downloads the file: one full fetch, no more)."""
    if connector.previous is None:
        return None
    try:
        body = connector.previous.content()
    except Exception:  # an unusable stored object costs one download, no more
        return None
    return body if body.startswith(XLSX_MAGIC) else None


def _get(connector: Connector, url: str, *, timeout: float, headers: dict[str, str] | None = None) -> Any:
    """One `PoliteSession.get`, keeping every answer it receives (a `requests` response hook), so a
    failure the session raises after its retries, or a challenge it refuses, is described by the
    host's last answer (`describe_response`) rather than by the session's one-line summary."""
    seen: list[Any] = []

    def keep(response: Any, *args: Any, **kwargs: Any) -> Any:
        seen.append(response)
        return response

    kwargs: dict[str, Any] = {"timeout": timeout, "hooks": {"response": [keep]}}
    if headers:
        kwargs["headers"] = headers
    try:
        return connector.http.get(url, **kwargs)
    except HttpBlocked as exc:
        last = seen[-1] if seen else None
        detail = (
            f"; {describe_response(last.status_code, last.headers, last.content)}" if last is not None else ""
        )
        raise BlockedError(f"GET {url} refused: {exc}{detail}") from exc
    except HttpFailed as exc:
        if not seen:
            raise ConnectorError(f"GET {url} failed: {exc}") from exc
        last = seen[-1]
        raise ConnectorError(
            f"GET {url} failed after {len(seen)} answers: "
            + describe_response(last.status_code, last.headers, last.content)
        ) from exc


def fetch_workbook(connector: Connector, url: str, *, timeout: float = 180) -> RawSnapshot:
    """GET a queue workbook (module docstring): conditional when the previous snapshot recorded
    validators for `url`; a 304 reuses the stored bytes, which the runner then records
    `unchanged`. The snapshot's `meta` keeps the validators for the next run."""
    t0 = time.monotonic()
    conditional = stored_validators(connector, url)
    r = _get(connector, url, timeout=timeout, headers=conditional)
    requests_made = 1
    content: bytes | None = None
    not_modified = False
    if conditional and r.status_code == 304:
        content = _reusable(connector)
        not_modified = content is not None
        if content is None:
            r = _get(connector, url, timeout=timeout)
            requests_made += 1
    if content is None:
        if r.status_code != 200:
            detail = describe_response(r.status_code, r.headers, r.content)
            if r.status_code in REFUSAL_STATUSES:
                raise BlockedError(f"GET {url} refused: {detail}")
            raise ConnectorError(f"GET {url} failed: {detail}")
        content = r.content
        if not content.startswith(XLSX_MAGIC):
            raise ParseError(
                f"GET {url} answered 200 with something other than an xlsx: "
                + describe_response(r.status_code, r.headers, content)
            )
    previous_meta = connector.previous.meta if connector.previous is not None else {}
    etag = r.headers.get("ETag") or (previous_meta.get("etag") if not_modified else None)
    last_modified = r.headers.get("Last-Modified") or (
        previous_meta.get("last_modified") if not_modified else None
    )
    meta: dict[str, Any] = {"requested_url": url}
    if etag:
        meta["etag"] = str(etag)
    if last_modified:
        meta["last_modified"] = str(last_modified)
    if not_modified:
        meta["not_modified"] = True
    return RawSnapshot(
        content=content,
        content_type=XLSX_CONTENT_TYPE if not_modified else r.headers.get("Content-Type", ""),
        url=url,
        retrieved_at=dt.datetime.now(dt.UTC),
        http_status=r.status_code,
        ext="xlsx",
        headers=dict(r.headers),
        elapsed_s=round(time.monotonic() - t0, 2),
        requests_made=requests_made,
        meta=meta,
    )
