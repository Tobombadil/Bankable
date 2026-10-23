"""Where price runs keep their output, beside the record connectors' tree (`pipeline/connectors/store.py`):

    snapshots/{source_id}/{ts}.{request key}.{ext}     raw bytes as fetched (immutable evidence)
    runs/{source_id}/{ts}.json                         the price run record (the commit marker)
    prices/{source_id}/intervals/{yyyy}-{mm}.parquet   interval prices, one file per UTC month
    prices/{source_id}/corrections/{ts}.parquet        stored intervals a run found changed
    prices/{source_id}/retail.parquet                  EIA's state retail table (one file)
    prices/{source_id}/downloads/{ts}.{key}.json       the download ledger: one entry per capped download
    prices/{source_id}/attempts/{ts}.json              one marker per backfill, written as it starts

Snapshot names start with the run's `ts` token so DA-10 retention reads their age from the name
as it does for every snapshot (`services/retention/snapshots.py`). The interval files are
rewritten on merge; a run writes them only after its DQ checks pass, and its run record last.

**Why a ledger beside the run record (review 2026-10-23).** The run record is written last, so a
run that dies after a download (a parse that raises past the runner, an out-of-memory kill, a
timeout) leaves no record of it, and a count read only from run records under-counts ERCOT's
"no more than three times within a 12-month period". A request whose `meta.max_downloads` is set
therefore writes its ledger entry *before* the request is sent (`state: started`) and rewrites it
once the bytes are stored (`stored`, with the snapshot's key and SHA-256, so a later run re-uses the
bytes instead of downloading them again) or the request failed (`failed`). Every entry counts as a
download except a `failed` one whose server answered with an error status (no report was served):
an entry left at `started` is a request whose outcome the process did not live to see, and is
counted. The backfill attempt markers serve the scheduler the same way: a backfill killed before
its run record still shows as attempted (`infra/scheduler/prices.py::effective_mode`).
"""

from __future__ import annotations

import datetime as dt
import io
import json
import pathlib
import re
from typing import Any

import pandas as pd
import pyarrow as pa  # type: ignore[import-untyped]  # pandas's parquet engine; no stubs installed
import pyarrow.parquet as pq  # type: ignore[import-untyped]

from pipeline.connectors.store import Store

_MONTH = re.compile(r"(\d{4})-(\d{2})\.parquet$")
#: The columns a coverage plan, a gap fill or the loader's summaries read: never `raw` or the
#: per-row provenance strings, which are most of a month file's size in memory.
KEY_COLUMNS = ["location", "market", "interval_start"]


def prices_root(store: Store, source_id: str) -> pathlib.Path:
    return store.root / "prices" / source_id


def month_path(store: Store, source_id: str, month: str) -> pathlib.Path:
    """`month` is `YYYY-MM` (UTC)."""
    return prices_root(store, source_id) / "intervals" / f"{month}.parquet"


def corrections_path(store: Store, source_id: str, ts: str) -> pathlib.Path:
    return prices_root(store, source_id) / "corrections" / f"{ts}.parquet"


def retail_path(store: Store, source_id: str) -> pathlib.Path:
    return prices_root(store, source_id) / "retail.parquet"


def list_months(store: Store, source_id: str) -> list[str]:
    """Every stored month for `source_id`, oldest first, as `YYYY-MM`."""
    prefix = store.key(prices_root(store, source_id) / "intervals") + "/"
    out = []
    for key in store.backend.list(prefix):
        m = _MONTH.search(key)
        if m:
            out.append(f"{m.group(1)}-{m.group(2)}")
    return sorted(set(out))


def month_of(ts: pd.Timestamp | dt.datetime) -> str:
    stamp = pd.Timestamp(ts)
    stamp = stamp.tz_convert("UTC") if stamp.tzinfo is not None else stamp.tz_localize("UTC")
    return f"{stamp.year:04d}-{stamp.month:02d}"


def month_labels(stamps: pd.Series) -> pd.Series:
    """`month_of` for a whole column at once (`YYYY-MM`, UTC)."""
    return pd.to_datetime(stamps, utc=True).dt.strftime("%Y-%m")


def months_between(first: dt.date, last: dt.date) -> list[str]:
    out: list[str] = []
    cursor = dt.date(first.year, first.month, 1)
    while cursor <= last:
        out.append(f"{cursor.year:04d}-{cursor.month:02d}")
        cursor = (cursor + dt.timedelta(days=32)).replace(day=1)
    return out


def read_month(
    store: Store, source_id: str, month: str, columns: list[str] | None = None
) -> pd.DataFrame | None:
    """One stored month; `columns` reads only those (the file is columnar, so the others are never
    decoded)."""
    path = month_path(store, source_id, month)
    if not store.exists(path):
        return None
    if columns is None:
        df = store.read_parquet(path)
    else:
        df = _read_shared_strings(store.read_bytes(path), columns)
    for col in ("interval_start", "interval_end"):
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], utc=True)
    return df


def _read_shared_strings(body: bytes, columns: list[str]) -> pd.DataFrame:
    """`columns` of a parquet file, each string column read dictionary-encoded and handed back as
    an object column whose rows share one Python string per distinct value. Read the usual way,
    every cell is its own string object: about 1.3 KB a row for the loader's columns (a source
    URL and a timestamp text per interval), 470 MB for three months of ERCOT (measured
    2026-10-23); shared, the same columns are 8 bytes a row each."""
    buf = io.BytesIO(body)
    schema = pq.read_schema(buf)
    strings = [c for c in columns if c in schema.names and pa.types.is_string(schema.field(c).type)]
    buf.seek(0)
    table = pq.read_table(buf, columns=columns, read_dictionary=strings, use_threads=False)
    df = table.to_pandas(ignore_metadata=True)
    for col in strings:
        df[col] = df[col].astype(object)
    return df


def read_months(
    store: Store, source_id: str, months: list[str], columns: list[str] | None = None
) -> pd.DataFrame:
    frames = [
        f for f in (read_month(store, source_id, m, columns) for m in months) if f is not None and len(f)
    ]
    if not frames:
        return pd.DataFrame(columns=columns) if columns else pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def write_month(store: Store, source_id: str, month: str, df: pd.DataFrame) -> pathlib.Path:
    ordered = df.sort_values(["location", "market", "interval_start"]).reset_index(drop=True)
    return store.write_parquet(month_path(store, source_id, month), ordered)


#: The order a month file's rows are written in (`write_month`, `write_month_table`).
MONTH_ORDER = [("location", "ascending"), ("market", "ascending"), ("interval_start", "ascending")]


def read_month_table(store: Store, source_id: str, month: str) -> pa.Table | None:
    """One stored month as an Arrow table: its strings stay in Arrow buffers (a month of ERCOT's
    fifteen-minute rows with their `raw` source rows is about 60 MB as Python objects)."""
    path = month_path(store, source_id, month)
    if not store.exists(path):
        return None
    return pq.read_table(io.BytesIO(store.read_bytes(path)))


def write_month_table(store: Store, source_id: str, month: str, table: pa.Table) -> pathlib.Path:
    """`write_month` for an Arrow table: the same file, rows in `MONTH_ORDER`."""
    path = month_path(store, source_id, month)
    store.guard(path)
    buf = io.BytesIO()
    pq.write_table(table.sort_by(MONTH_ORDER), buf)
    store.backend.put(store.key(path), buf.getvalue())
    return path


# ------------------------------------------------------------------- download ledger, attempts
#: Ledger states (module docstring). Only `failed` with an HTTP error status is not a download.
LEDGER_STATES = ("started", "stored", "failed")


def _write_json(store: Store, path: pathlib.Path, body: dict[str, Any]) -> pathlib.Path:
    store.guard(path)
    store.backend.put(store.key(path), json.dumps(body, sort_keys=True, default=str).encode("utf-8"))
    return path


def _read_jsons(store: Store, root: pathlib.Path) -> list[tuple[str, dict[str, Any]]]:
    """`(name without .json, body)` for every readable JSON object under `root`, oldest first."""
    out: list[tuple[str, dict[str, Any]]] = []
    for key in sorted(store.backend.list(store.key(root) + "/")):
        if not key.endswith(".json"):
            continue
        try:
            body = json.loads(store.backend.get(key).decode("utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(body, dict):
            out.append((key.rsplit("/", 1)[-1][: -len(".json")], body))
    return out


def ledger_path(store: Store, source_id: str, ts: str, key: str) -> pathlib.Path:
    return prices_root(store, source_id) / "downloads" / f"{ts}.{key}.json"


def write_ledger(store: Store, source_id: str, ts: str, key: str, entry: dict[str, Any]) -> pathlib.Path:
    """Write (or rewrite) one download's ledger entry; `entry["state"]` is one of `LEDGER_STATES`."""
    if entry.get("state") not in LEDGER_STATES:
        raise ValueError(f"ledger state must be one of {LEDGER_STATES}, got {entry.get('state')!r}")
    return _write_json(store, ledger_path(store, source_id, ts, key), {**entry, "run_ts": ts, "key": key})


def read_ledger(store: Store, source_id: str) -> list[dict[str, Any]]:
    return [body for _name, body in _read_jsons(store, prices_root(store, source_id) / "downloads")]


def counts_as_download(entry: dict[str, Any]) -> bool:
    """A ledger entry is a download unless the server answered it with an error status."""
    if entry.get("state") != "failed":
        return True
    status = entry.get("http_status")
    return not (isinstance(status, int) and status >= 400)


def attempt_path(store: Store, source_id: str, ts: str) -> pathlib.Path:
    return prices_root(store, source_id) / "attempts" / f"{ts}.json"


def write_attempt(store: Store, source_id: str, ts: str, body: dict[str, Any]) -> pathlib.Path:
    return _write_json(store, attempt_path(store, source_id, ts), {**body, "run_ts": ts})


def backfill_attempts(store: Store, source_id: str) -> set[str]:
    """The `ts` of every backfill this store shows started: an attempt marker, or (for a store from
    before the markers) a backfill run record."""
    seen = {name for name, body in _read_jsons(store, prices_root(store, source_id) / "attempts")}
    seen.update(name for name, record in store._run_entries(source_id) if record.get("mode") == "backfill")
    return seen
