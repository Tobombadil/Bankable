"""The retention job's data-room rule: objects no document row names are swept (docs/19 §3, "the
retention job sweeps orphans"; "Data rooms, as built").

Deleting a file, a folder, an item or an account deletes the rows in its transaction and removes the
objects once it commits (`services.projects.data_room_store.delete_after_commit`); an upload whose
row never commits removes its object on rollback. What a crash, a store outage or a missing store
setting leaves behind is an *orphan*: an object under `data-rooms/` that no
`private_project_document.object_key` names. It is unreadable already (its sealed key went with its
row), and this rule deletes it once it is older than `ORPHAN_GRACE`, so an upload whose row has not
committed yet is never taken.

It runs only where the data room is in use: `DATA_ROOMS=on`, or `DATA_ROOM_STORE` named explicitly
(a deployment that turned data rooms off after using them still sweeps). Elsewhere it is skipped and
reaches no store. The report holds counts only: no object key, no file name.

Rows whose object is missing are counted (`rows_without_object`), not changed: a download of one
answers `503`, and an operator decides.
"""

from __future__ import annotations

import datetime as dt
import os
from collections.abc import Mapping
from typing import Any, Final

import sqlalchemy as sa
from sqlalchemy.orm import Session

from services.projects import data_room, data_room_store

RULE_NAME: Final = "data_room_orphans"
RULE: Final = (
    "data-room objects that no document row names are deleted once 6 hours old (docs/19 §3: the "
    "retention job sweeps orphans)"
)
ORPHAN_GRACE: Final = dt.timedelta(hours=6)


def _in_use(env: Mapping[str, str]) -> bool:
    return data_room_store.data_rooms_enabled(env) or bool((env.get(data_room_store.ENV_STORE) or "").strip())


def sweep_orphans(
    db: Session,
    now: dt.datetime,
    *,
    dry_run: bool,
    store: data_room_store.ObjectStore | None = None,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """The rule's report (module docstring). `store` is the configured one unless given."""
    source = os.environ if env is None else env
    if store is None:
        if not _in_use(source):
            return {"status": "skipped", "rule": RULE, "reason": "data rooms are off and no store is named"}
        try:
            store = data_room_store.store_from_env(source)
        except data_room_store.DataRoomUnavailable:
            return {"status": "skipped", "rule": RULE, "reason": "no data-room store is configured"}
    if not sa.inspect(db.get_bind()).has_table("private_project_document"):
        return {
            "status": "skipped",
            "rule": RULE,
            "reason": "the data-room tables do not exist (before 0043)",
        }
    cutoff = now - ORPHAN_GRACE
    known = data_room.stored_object_keys(db)
    report: dict[str, Any] = {
        "status": "applied",
        "rule": RULE,
        "cutoff": cutoff.isoformat(),
        "action": "delete the object",
        "matched": 0,
        "changed": 0,
        "kept_recent": 0,
        "rows_without_object": 0,
        "errors": 0,
    }
    listed: set[str] = set()
    try:
        for key, modified in store.list_objects():
            listed.add(key)
            if key in known:
                continue
            if modified > cutoff:
                report["kept_recent"] += 1
                continue
            report["matched"] += 1
            if dry_run:
                continue
            try:
                store.delete(key)
            except data_room_store.DataRoomUnavailable:
                report["errors"] += 1
            else:
                report["changed"] += 1
    except data_room_store.DataRoomUnavailable:
        report["errors"] += 1
        return report
    report["rows_without_object"] = len(known - listed)
    return report
