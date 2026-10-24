"""Test support: a fake HTTP session that answers Ember's API paths from the fixtures, offline.

The fixtures are **synthetic**: Ember's published answer shapes (OpenAPI 1.2.3, the July 2026 CSV layout its
yearly data page describes, and the earlier long layout) with invented round values, because no real file
could be recorded on 2026-10-23: the API needs the owner's key, and files.ember-energy.org forbids robots
(`docs/26-global-figures.md` §7). `python -m pipeline.connectors.global_ember_electricity fixture` trims a
real answer into a fixture once one is held.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import json
import pathlib
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urlsplit

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"
YEARLY_JSON = "api_yearly_generation_synthetic.json"
MONTHLY_JSON = "api_monthly_generation_synthetic.json"
CAPACITY_JSON = "api_monthly_capacity_synthetic.json"
WIDE_CSV = "yearly_wide_synthetic.csv"
LONG_CSV = "yearly_long_synthetic.csv"
PATH_FIXTURES = {
    "/v1/electricity-generation/yearly": YEARLY_JSON,
    "/v1/electricity-generation/monthly": MONTHLY_JSON,
    "/v1/installed-capacity/monthly": CAPACITY_JSON,
}
TEST_KEY = "test-key-7f3a9c2e"


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def fixture_json(name: str) -> dict[str, Any]:
    payload: dict[str, Any] = json.loads(fixture_bytes(name))
    return payload


class FakeResponse:
    def __init__(self, status: int, content: bytes, url: str) -> None:
        self.status_code = status
        self.content = content
        self.url = url
        self.headers = {"Content-Type": "application/json"}


class FakeSession:
    """Stands in for `PoliteSession`: `get` answers from `bodies` (path -> bytes) or `statuses` (path ->
    HTTP status), or raises `errors[path]`; every call is recorded with its host, so a test can prove a
    host was never contacted. The answers echo the key in `stats.query_parameters_used`, as an API might."""

    def __init__(
        self,
        bodies: Mapping[str, bytes] | None = None,
        *,
        statuses: Mapping[str, int] | None = None,
        errors: Mapping[str, Exception] | None = None,
    ) -> None:
        self.bodies = (
            dict(bodies) if bodies is not None else {p: fixture_bytes(n) for p, n in PATH_FIXTURES.items()}
        )
        self.statuses = dict(statuses or {})
        self.errors = dict(errors or {})
        self.calls: list[dict[str, Any]] = []
        self.requests_made = 0
        self.max_attempts = 4

    def get(self, url: str, *, params: Mapping[str, Any] | None = None, **kwargs: Any) -> FakeResponse:
        parts = urlsplit(url)
        self.calls.append({"host": parts.netloc, "path": parts.path, "params": dict(params or {}), **kwargs})
        self.requests_made += 1
        if parts.path in self.errors:
            raise self.errors[parts.path]
        status = self.statuses.get(parts.path, 200)
        body = self.bodies.get(parts.path, b"") if status == 200 else b'{"detail":"error"}'
        if status == 200 and body and params and params.get("api_key"):
            payload = json.loads(body)
            stats = payload.setdefault("stats", {})
            stats["query_parameters_used"] = {
                **stats.get("query_parameters_used", {}),
                "api_key": params["api_key"],
            }
            body = json.dumps(payload).encode("utf-8")
        query = "&".join(f"{k}={v}" for k, v in (params or {}).items())
        return FakeResponse(status, body, f"{url}?{query}")


def fixed_clock(when: dt.datetime) -> Callable[[], dt.datetime]:
    return lambda: when


def with_data(name: str, edit: Callable[[list[dict[str, Any]]], list[dict[str, Any]]]) -> bytes:
    """A fixture's answer with its `data` list passed through `edit` (a new release, a dropped area)."""
    payload = fixture_json(name)
    payload["data"] = edit(list(payload["data"]))
    payload["stats"]["number_of_records"] = len(payload["data"])  # as Ember's own answer would state
    return json.dumps(payload).encode("utf-8")


def wide_rows() -> tuple[list[str], list[dict[str, str]]]:
    """The wide yearly CSV fixture as (headers, rows), for a test to edit."""
    reader = csv.DictReader(io.StringIO(fixture_bytes(WIDE_CSV).decode("utf-8")))
    return list(reader.fieldnames or []), list(reader)


def csv_bytes(fields: list[str], rows: list[dict[str, str]]) -> bytes:
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=fields, lineterminator="\n", extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue().encode("utf-8")


def dated_wide_csv(month: int = 1) -> bytes:
    """The wide yearly fixture with its `Year` column given as Ember's yearly download may give it:
    `Date`, `YYYY-MM-01` (review of 2026-10-23: the yearly and monthly pages describe their files in the
    same words, "One row per area, month and electricity source")."""
    fields, rows = wide_rows()
    for row in rows:
        row["Date"] = f"{row.pop('Year')}-{month:02d}-01"
    return csv_bytes(["Date" if f == "Year" else f for f in fields], rows)
