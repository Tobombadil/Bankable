"""Test support: a store holding Ember runs from the recorded fixtures, loaded into a session (offline).

`seed` runs the connector against `FakeSession` (the API fixtures) and, by default, a person's download of the
yearly CSV in the inbox, then loads the newest run of each dataset. The download carries the sidecar a person
writes (its download time and, unless `reviewed=False`, the review of the first download that
`runner._file_holds` asks for). The fixtures are synthetic values in Ember's published shapes
(`pipeline/connectors/global_ember_electricity/testing.py`).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import pathlib
from typing import Any

from sqlalchemy.orm import Session

from pipeline.connectors.global_ember_electricity import SOURCE_ID
from pipeline.connectors.global_ember_electricity.runner import ENV_KEY, EmberRun
from pipeline.connectors.global_ember_electricity.testing import (
    TEST_KEY,
    WIDE_CSV,
    FakeSession,
    fixed_clock,
    fixture_bytes,
)
from pipeline.connectors.store import Store
from services.global_figures.load import load_latest

WHEN = dt.datetime(2026, 10, 8, 9, 49, tzinfo=dt.UTC)


YEARLY_FILE = "release_generation_yearly_global.csv"


def drop_in_inbox(
    root: pathlib.Path,
    body: bytes,
    downloaded_at: str | None = "2026-10-07T12:00:00Z",
    *,
    name: str = YEARLY_FILE,
    reviewed: bool = True,
    dataset: str | None = None,
) -> pathlib.Path:
    """A person's download and its sidecar in the inbox; returns the CSV's path."""
    inbox = root / "inbox" / SOURCE_ID
    inbox.mkdir(parents=True, exist_ok=True)
    path = inbox / name
    path.write_bytes(body)
    sidecar: dict[str, Any] = {}
    if downloaded_at is not None:
        sidecar["downloaded_at"] = downloaded_at
    if reviewed:
        sidecar["reviewed_sha256"] = hashlib.sha256(body).hexdigest()
    if dataset is not None:
        sidecar["dataset"] = dataset
    if sidecar:
        path.with_suffix(".json").write_text(json.dumps(sidecar))
    return path


def seed(
    session: Session,
    root: pathlib.Path,
    *,
    api: bool = True,
    inbox: bool = True,
    http: FakeSession | None = None,
    when: dt.datetime = WHEN,
    body: bytes | None = None,
) -> dict[str, Any]:
    """One connector run (the API with a test key when `api`, the inbox CSV when `inbox`: `body`, else the
    wide fixture), then the load."""
    store = Store(root)
    if inbox:
        drop_in_inbox(root, body if body is not None else fixture_bytes(WIDE_CSV))
    env = {ENV_KEY: TEST_KEY} if api else {}
    result = EmberRun(store, http=http or FakeSession(), env=env, clock=fixed_clock(when)).run()  # type: ignore[arg-type]
    report = load_latest(session, store=store)
    session.flush()
    return {"run": result.record, "load": report, "store": store}
