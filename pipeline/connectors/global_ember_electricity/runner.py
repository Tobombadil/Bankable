"""One Ember run, end to end: gate, plan, fetch, snapshot, parse, normalise, check, diff, write.

Where it writes (the record connectors' tree, `pipeline/connectors/store.py`):

    snapshots/global.ember.electricity/{ts}.{request key}.json   each API answer, key removed (evidence)
    snapshots/global.ember.electricity/{ts}.inbox{n}.csv         a person's download, as read (evidence)
    normalized/global.ember.electricity/{ts}.parquet             the values of every dataset that passed
    revisions/global.ember.electricity/{ts}.parquet              values Ember changed or dropped since
    runs/global.ember.electricity/{ts}.json                      the run record (written last)

**A failed download keeps the last good data.** Nothing from the API is stored unless every API request
the run made answered: a refused key, a timeout or a server error ends the run `blocked` or `failed`
with no API value, and the loader (`services/global_figures/load.py`) keeps serving what it holds. The
inbox is read whatever the API answered, so a person's download (the only source of yearly capacity)
never waits for the API to recover; the run's status still says what the API did, and the refresh job
loads whatever the run wrote. A dataset whose checks hold (`dq.py`) is left out of `normalized/`; the run
ends `partial`.

**Unchanged.** Ember's answers carry a timestamp in `stats`, so each request is compared by a digest of
its `data` alone (`ember.data_digest`) with the last run that wrote the same request; a repeat is
`unchanged` and is neither parsed nor written again.

**Revisions, not events.** Ember amends its series between releases ("regularly amended with the latest
available data", the yearly data page). A changed value is a statistical revision, not a real-world
change, so it is counted on the run record and kept in `revisions/`, and never becomes an `event` or a
public change (docs/24-trends.md: a source's first load is not news). Values before the start of a
request's window (the monthly series' rolling 36 months) left the window; they are not counted `gone`.

**The inbox.** A person's download of Ember's CSV, at `inbox/global.ember.electricity/<name>.csv` under
the data root, with `<name>.json` beside it:

    {"downloaded_at": "<ISO time of the download>", "dataset": "yearly", "reviewed_sha256": "<sha256>"}

is read once: a file whose SHA-256 an earlier run loaded is skipped, and so is one downloaded before the
newest download of its dataset already loaded (`older_than_loaded`: a file superseded in an earlier run,
or an old one copied in again, never replaces newer figures). `downloaded_at` is the rows'
`retrieved_at`, and a file without one is held: the time the file was placed (its modification time,
which a copy resets) is recorded as `placed_at`, never as a retrieval time. `dataset` is optional (the
file's name, else its structure, decides; `ember.classify_file`). `reviewed_sha256` is needed once per
dataset: the first download of Ember's yearly (or monthly) file is held until a person has read what it
parsed to (the hold's data: countries, areas, periods, World's latest year) and records the file's
SHA-256, so a layout this parser has never met is caught by a person before anything is served
(`python -m pipeline.connectors.global_ember_electricity check <file>` prints the same summary and the
sidecar to place). Without `EMBER_API_KEY` a run reads the inbox alone; with neither a key nor a new
file it ends `blocked`, so the source's health says what is missing.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import os
import pathlib
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import pandas as pd
import requests

from pipeline.connectors.base import ConnectorError, ParseError, RawSnapshot, utcnow
from pipeline.connectors.global_ember_electricity import SOURCE_ID
from pipeline.connectors.global_ember_electricity import dq as checks
from pipeline.connectors.global_ember_electricity.areas import WORLD_ID, unknown_iso3
from pipeline.connectors.global_ember_electricity.ember import (
    API_BASE,
    DATASET_PAGES,
    FUEL_SOURCES,
    ORIGINS,
    PARSER_VERSION,
    EmberRequest,
    FileDataset,
    Observation,
    ParseReport,
    as_yearly,
    classify_file,
    data_digest,
    normalize,
    parse_api,
    parse_csv,
    plan,
    redact,
    redact_bytes,
    source_for,
)
from pipeline.connectors.http import HttpBlocked, HttpFailed, PoliteSession
from pipeline.connectors.registry import PUBLISHABLE_REUSE, Registry, SourceEntry
from pipeline.connectors.store import Store, ts_token

log = logging.getLogger("pipeline.connectors.global_ember_electricity")

ENV_KEY = "EMBER_API_KEY"
TRIGGERS = ("schedule", "manual", "backfill", "retry")
#: An answer for every area since 2000 is about 20 MB; the API states no rate limit.
TIMEOUT_S = 180.0
INBOX = "inbox"
FILE_METRICS = ("generation_twh", "share_of_generation_pct", "capacity_gw")


class EmberGateRefused(ConnectorError):
    """The manifest row may not be fetched for a publication path (CLAUDE.md)."""


class KeyRefused(ConnectorError):
    """The API answered 401 or 403: the key is missing, wrong or revoked (not retried)."""


class MissingApiKey(ConnectorError):
    """`EMBER_API_KEY` is not set and no new file waits in the inbox: nothing to read (not retried)."""


@dataclass
class RunResult:
    record: dict[str, Any]
    run_path: pathlib.Path
    ts: str


@dataclass
class _Frame:
    dataset: str
    origin: str
    df: pd.DataFrame
    report: ParseReport
    observations: int
    missing: dict[str, int]
    #: The run record's entry for the request or file this frame came from (marked written/loaded).
    source_ref: dict[str, Any]
    #: The first period of the request's window (the monthly series' rolling start); values before it
    #: left the window and are not revisions.
    window_start: str | None = None
    #: Checks decided before the frame's own (a download with no recorded time, a first download not
    #: yet reviewed); recorded with the frame's profile so a person sees what the file parsed to.
    holds: list[dict[str, Any]] = field(default_factory=list)
    #: A download's dataset and what decided it (`ember.classify_file`); None for an API answer.
    kind: FileDataset | None = None

    @property
    def label(self) -> str:
        return f"{self.dataset}/{self.origin}"


@dataclass(frozen=True)
class InboxFile:
    """A person's download in the inbox and what its sidecar (`<name>.json`) says."""

    key: str
    body: bytes
    #: When the person downloaded it, as recorded in the sidecar; None when nothing records it.
    downloaded_at: str | None
    #: When the file was placed in the inbox (its modification time; a copy resets it): kept apart from
    #: any retrieval time.
    placed_at: str
    declared_dataset: str | None
    reviewed_sha256: str | None
    sidecar_error: str | None = None

    @property
    def name(self) -> str:
        return self.key.rsplit("/", 1)[-1]

    @property
    def sidecar_name(self) -> str:
        return self.name[: -len(".csv")] + ".json"


def gate(entry: SourceEntry) -> None:
    """Refuse a row whose terms or status forbid a publication path (CLAUDE.md)."""
    status = str(entry.raw.get("status") or "")
    if entry.never_ingest:
        raise EmberGateRefused(f"{entry.id} is a private aggregator; it has no connector by design")
    if status in ("pending", "excluded"):
        raise EmberGateRefused(f"{entry.id} is `status: {status}` in data/sources.yaml; nothing is fetched")
    if entry.reuse not in PUBLISHABLE_REUSE:
        raise EmberGateRefused(f"{entry.id} has reuse={entry.reuse!r}, not publishable; not fetched")
    if entry.publication == "none":
        raise EmberGateRefused(f"{entry.id}: publication=none publishes nothing; not fetched")


def _iso(t: dt.datetime) -> str:
    return t.astimezone(dt.UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _missing(observations: Sequence[Observation], metrics: tuple[str, ...]) -> dict[str, int]:
    """Per metric, the known-series observations that carry no value for it."""
    out = dict.fromkeys(metrics, 0)
    for obs in observations:
        if source_for(obs.series) is None:
            continue
        for metric in metrics:
            if metric not in obs.values:
                out[metric] += 1
    return out


def _known(observations: Sequence[Observation]) -> int:
    return sum(1 for o in observations if source_for(o.series) is not None)


def _parse_time(text: str) -> dt.datetime | None:
    try:
        when = dt.datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo is not None else when.replace(tzinfo=dt.UTC)


def world_sample(df: pd.DataFrame) -> dict[str, Any] | None:
    """World's newest period of generation by source (and capacity where the file has it), so a person
    reviewing a first download sees at a glance whether a year reads as a year (`2025`, ~30,000 TWh)
    and not as one month."""
    world = df[(df["area_id"] == WORLD_ID) & (~df["is_aggregate"].astype(bool))] if not df.empty else df
    generation = world[world["metric"] == "generation_twh"] if not world.empty else world
    if generation.empty:
        return None
    period = str(generation["period"].max())
    latest = world[world["period"] == period]
    out: dict[str, Any] = {"period": period}
    for metric in ("generation_twh", "capacity_gw"):
        values = latest[latest["metric"] == metric]
        pairs = zip(values["source"], values["value"], strict=True)
        by_source = {str(s): round(float(v), 2) for s, v in pairs}
        if by_source:
            out[metric] = {s: by_source[s] for s in FUEL_SOURCES if s in by_source}
            out[f"total_{metric}"] = round(sum(out[metric].values()), 2)
    return out


def review_summary(frame_df: pd.DataFrame, prof: Mapping[str, Any], kind: FileDataset) -> dict[str, Any]:
    """What a person reads before accepting a first download (`first_file_review`, and the `check`
    command): the dataset and why, the areas and countries keyed, the periods, World's newest one."""
    kinds = frame_df.groupby("area_kind")["area_id"].nunique() if not frame_df.empty else pd.Series(dtype=int)
    return {
        "dataset": kind.dataset,
        "dataset_basis": kind.basis,
        "dated_years": kind.dated_years,
        "areas_by_kind": {str(k): int(v) for k, v in kinds.items()},
        "countries": prof.get("countries"),
        "first_period": str(frame_df["period"].min()) if not frame_df.empty else None,
        "latest_period": prof.get("latest_period"),
        "metrics": sorted(frame_df["metric"].unique()) if not frame_df.empty else [],
        "world_latest": world_sample(frame_df),
    }


class EmberRun:
    """One run. `http`, `env` and `clock` are injectable so the tests drive it from recorded answers."""

    def __init__(
        self,
        store: Store,
        *,
        registry: Registry | None = None,
        http: PoliteSession | None = None,
        env: Mapping[str, str] | None = None,
        clock: Callable[[], dt.datetime] = utcnow,
        trigger: str = "manual",
        run_id: str | None = None,
    ) -> None:
        self.store = store
        self.registry = registry or Registry()
        self.entry = self.registry.get(SOURCE_ID)
        self.env: Mapping[str, str] = env if env is not None else os.environ
        self.clock = clock
        self.http = http or PoliteSession(rate_limits={"api.ember-energy.org": self.entry.max_rps})
        self.trigger = trigger if trigger in TRIGGERS else "manual"
        self.run_id = run_id or str(uuid.uuid4())
        self.key = str(self.env.get(ENV_KEY) or "").strip()

    # ------------------------------------------------------------------------- history
    def _history(self) -> list[tuple[str, dict[str, Any]]]:
        return list(self.store._run_entries(SOURCE_ID))

    def _last_written(self, label: str) -> tuple[str, dict[str, Any]] | None:
        """`(ts, dataset entry)` of the newest run that wrote dataset `label` (`yearly/api`, ...)."""
        for ts, record in reversed(self._history()):
            entry = (record.get("datasets") or {}).get(label)
            if entry and entry.get("written"):
                return ts, entry
        return None

    def _last_digest(self, key: str) -> str | None:
        for _ts, record in reversed(self._history()):
            for req in record.get("requests") or []:
                if req.get("key") == key and req.get("written"):
                    return str(req.get("data_sha256") or "") or None
        return None

    def _loaded_inbox(self) -> tuple[set[str], dict[str, str]]:
        """The downloads earlier runs are done with (loaded, or older than one loaded), by SHA-256, and
        per dataset the download time of the newest one loaded."""
        seen: set[str] = set()
        newest: dict[str, str] = {}
        for _ts, record in self._history():
            for item in record.get("inbox") or []:
                state, sha = item.get("state"), item.get("sha256")
                if state in ("loaded", "older_than_loaded") and sha:
                    seen.add(str(sha))
                when, dataset = item.get("downloaded_at"), item.get("dataset")
                if state == "loaded" and isinstance(when, str) and isinstance(dataset, str):
                    newest[dataset] = max(newest.get(dataset, ""), when)
        return seen, newest

    # --------------------------------------------------------------------------- fetch
    def fetch(self, request: EmberRequest) -> RawSnapshot:
        """One API GET, the key in the query string only; the answer and its URL come back with the key
        removed. Raises `HttpBlocked` (robots, challenge), `HttpFailed` (retries spent), `KeyRefused`
        (401/403) or `ConnectorError` (any other status but 200)."""
        t0 = time.monotonic()
        before = self.http.requests_made
        params = {**dict(request.params), "api_key": self.key}
        resp = self.http.get(
            f"{API_BASE}{request.path}", params=params, timeout=TIMEOUT_S, honour_robots=True
        )
        if resp.status_code in (401, 403):
            raise KeyRefused(f"{request.key}: HTTP {resp.status_code} (the API key was refused)")
        if resp.status_code != 200:
            raise ConnectorError(f"{request.key}: HTTP {resp.status_code}")
        return RawSnapshot(
            content=redact_bytes(bytes(resp.content), [self.key]),
            content_type=resp.headers.get("Content-Type", "application/json"),
            url=request.url,
            retrieved_at=self.clock(),
            http_status=resp.status_code,
            ext="json",
            headers={k: v for k, v in resp.headers.items() if k.lower() in ("etag", "last-modified")},
            elapsed_s=round(time.monotonic() - t0, 2),
            requests_made=max(self.http.requests_made - before, 1),
            redacted=True,
            meta={"key": request.key},
        )

    def _inbox_files(self) -> list[InboxFile]:
        """Each CSV in the inbox with what its sidecar says, in name order."""
        root = self.store.root / INBOX / SOURCE_ID
        keys = sorted(self.store.backend.list(self.store.key(root) + "/"))
        out: list[InboxFile] = []
        for key in keys:
            if not key.lower().endswith(".csv"):
                continue
            body = self.store.backend.get(key)
            sidecar_key = key[: -len(".csv")] + ".json"
            meta: dict[str, Any] = {}
            error: str | None = None
            if sidecar_key in keys:
                try:
                    loaded = json.loads(self.store.backend.get(sidecar_key).decode("utf-8"))
                    meta = loaded if isinstance(loaded, dict) else {}
                    if not isinstance(loaded, dict):
                        error = "the sidecar is not a JSON object"
                except (ValueError, UnicodeDecodeError) as exc:
                    error = f"the sidecar is not JSON: {exc}"[:300]
            local = self.store.base_root / key
            stamp = (
                dt.datetime.fromtimestamp(local.stat().st_mtime, dt.UTC) if local.exists() else self.clock()
            )
            when = meta.get("downloaded_at")
            downloaded = None
            if isinstance(when, str) and when.strip():
                parsed = _parse_time(when)
                if parsed is None:
                    error = error or f"downloaded_at is not an ISO time: {when[:60]!r}"
                else:
                    downloaded = _iso(parsed)
            reviewed = meta.get("reviewed_sha256")
            out.append(
                InboxFile(
                    key=key,
                    body=body,
                    downloaded_at=downloaded,
                    placed_at=_iso(stamp),
                    declared_dataset=str(meta["dataset"]) if isinstance(meta.get("dataset"), str) else None,
                    reviewed_sha256=reviewed.strip().lower() if isinstance(reviewed, str) else None,
                    sidecar_error=error,
                )
            )
        return out

    # ----------------------------------------------------------------------------- run
    def run(self) -> RunResult:
        gate(self.entry)
        started = self.clock()
        ts = ts_token(started)
        record: dict[str, Any] = {
            "id": self.run_id,
            "source_id": SOURCE_ID,
            "parser_version": PARSER_VERSION,
            "trigger": self.trigger,
            "started_at": _iso(started),
            "status": "running",
            "api_key_present": bool(self.key),
            "requests": [],
            "inbox": [],
            "datasets": {},
            "dq": {"checks": []},
            "outputs": {},
            "rows_seen": 0,
            "rows_new": 0,
            "rows_changed": 0,
            "rows_gone": 0,
            "bytes": 0,
        }
        api_status: str | None = None
        api_error: BaseException | None = None
        try:
            frames = self._api_frames(ts, record)
        except (HttpBlocked, KeyRefused) as exc:
            frames, api_status, api_error = [], "blocked", exc
        except (HttpFailed, ConnectorError, requests.RequestException) as exc:
            frames, api_status, api_error = [], "failed", exc
        # No API value is kept after an API error (all or nothing), but the inbox is read whatever the API
        # answered: a person's download is the only yearly capacity, and it should not wait for the API.
        frames.extend(self._inbox_frames(ts, record))
        if api_error is not None:
            return self._finish(ts, record, frames=frames, status=api_status, error=api_error)
        if not self.key:
            fresh = [
                i for i in record["inbox"] if i.get("state") not in ("already_loaded", "older_than_loaded")
            ]
            if not fresh:
                err = MissingApiKey(
                    f"{ENV_KEY} is not set: the API was not asked and no new file waits in the inbox "
                    "(docs/26-global-figures.md §2); the figures already loaded stay served"
                )
                return self._finish(ts, record, frames=[], status="blocked", error=err)
            record["dq"]["checks"].append(
                checks._check("api_key", "warn", f"{ENV_KEY} is not set; only the inbox was read")
            )
        return self._finish(ts, record, frames=frames, status=None, error=None)

    def _api_frames(self, ts: str, record: dict[str, Any]) -> list[_Frame]:
        fetched = [(req, self.fetch(req)) for req in plan(self.clock().date(), key_present=bool(self.key))]
        # Every request answered: only now is anything stored (all or nothing).
        frames: list[_Frame] = []
        for req, raw in fetched:
            digest = data_digest(raw.content)
            snap = self.store.write_snapshot(SOURCE_ID, ts, f"{req.key}.json", raw.content)
            unchanged = digest == self._last_digest(req.key)
            entry: dict[str, Any] = {
                "key": req.key,
                "dataset": req.dataset,
                "url": req.url,
                "http_status": raw.http_status,
                "bytes": len(raw.content),
                "sha256": raw.sha256,
                "data_sha256": digest,
                "snapshot_key": self.store.key(snap),
                "elapsed_s": raw.elapsed_s,
                "requests": raw.requests_made,
                "retrieved_at": raw.retrieved_at_iso,
                "unchanged": unchanged,
                "written": False,
            }
            record["requests"].append(entry)
            record["bytes"] += len(raw.content)
            if unchanged:
                continue
            observations, report = parse_api(raw, req)
            if req.dataset == "yearly" and classify_file(observations, name=req.key).dated_years:
                # A yearly answer dating its years `2025-01-01` (one period a year, all January) is still
                # yearly data; read otherwise, every row would be dropped as a month.
                observations = as_yearly(observations)
            df = normalize(
                observations,
                dataset=req.dataset,
                origin="api",
                source_url=req.url,
                retrieved_at=raw.retrieved_at_iso,
                licence_id=self.entry.licence_id,
                report=report,
            )
            frames.append(
                _Frame(
                    req.dataset,
                    "api",
                    df,
                    report,
                    _known(observations),
                    _missing(observations, req.metrics),
                    entry,
                    window_start=str(req.params.get("start_date") or "") or None,
                )
            )
        return frames

    def _inbox_frames(self, ts: str, record: dict[str, Any]) -> list[_Frame]:
        loaded, loaded_at = self._loaded_inbox()
        newest: dict[str, tuple[_Frame, dict[str, Any]]] = {}
        for n, inbox in enumerate(self._inbox_files()):
            sha = hashlib.sha256(inbox.body).hexdigest()
            item: dict[str, Any] = {
                "key": inbox.key,
                "sha256": sha,
                "bytes": len(inbox.body),
                "downloaded_at": inbox.downloaded_at,
                "placed_at": inbox.placed_at,
            }
            record["inbox"].append(item)
            if sha in loaded:
                item["state"] = "already_loaded"
                continue
            snap = self.store.write_snapshot(SOURCE_ID, ts, f"inbox{n}.csv", inbox.body)
            item["snapshot_key"] = self.store.key(snap)
            when = _parse_time(inbox.downloaded_at) if inbox.downloaded_at else None
            raw = RawSnapshot(
                content=inbox.body,
                content_type="text/csv",
                url=inbox.key,
                retrieved_at=when or self.clock(),
                http_status=200,
                ext="csv",
            )
            try:
                observations, report = parse_csv(raw)
            except ParseError as exc:
                item["state"] = "failed"
                item["error"] = str(exc)[:1000]
                record["dq"]["checks"].append(
                    checks._check("inbox_parse", "hold", str(exc)[:500], key=inbox.key)
                )
                continue
            kind = classify_file(observations, name=inbox.key, declared=inbox.declared_dataset)
            item["dataset_basis"] = kind.basis
            if kind.dataset is None:
                item["state"] = "held"
                item["error"] = str(kind.hold)
                record["dq"]["checks"].append(
                    checks._check("inbox_dataset", "hold", str(kind.hold), key=inbox.key, basis=kind.basis)
                )
                continue
            if kind.dated_years:
                observations = as_yearly(observations)
            dataset = kind.dataset
            item["dataset"] = dataset
            if inbox.downloaded_at and inbox.downloaded_at < loaded_at.get(dataset, ""):
                # Older than a download already loaded (one superseded in an earlier run, or an old file
                # copied in again): loading it would replace newer figures with older ones.
                item["state"] = "older_than_loaded"
                item["newest_loaded_downloaded_at"] = loaded_at[dataset]
                continue
            df = normalize(
                observations,
                dataset=dataset,
                origin="file",
                source_url=DATASET_PAGES[dataset],
                retrieved_at=raw.retrieved_at_iso,
                licence_id=self.entry.licence_id,
                report=report,
            )
            frame = _Frame(
                dataset,
                "file",
                df,
                report,
                _known(observations),
                _missing(observations, FILE_METRICS),
                item,
                holds=self._file_holds(inbox, sha, dataset, kind),
                kind=kind,
            )
            previous = newest.get(dataset)
            newer = (
                str(previous[1]["downloaded_at"] or "") > str(inbox.downloaded_at or "")
                if previous
                else False
            )
            if newer:
                item["state"] = "superseded"
                continue
            if previous is not None:
                previous[1]["state"] = "superseded"
            item["state"] = "read"
            newest[dataset] = (frame, item)
        return [frame for frame, _item in newest.values()]

    def _file_holds(
        self, inbox: InboxFile, sha: str, dataset: str, kind: FileDataset
    ) -> list[dict[str, Any]]:
        """Holds a download earns before its values are checked: no recorded download time (its
        placed-at time is not a retrieval time), and, for the first download of a dataset, no
        person's review of what it parsed to."""
        holds: list[dict[str, Any]] = []
        sidecar = {
            "downloaded_at": inbox.downloaded_at or "<when you downloaded it, ISO 8601>",
            "dataset": dataset,
        }
        if inbox.sidecar_error:
            holds.append(
                checks._check(
                    "inbox_sidecar", "hold", inbox.sidecar_error, key=inbox.key, sidecar=inbox.sidecar_name
                )
            )
        elif inbox.downloaded_at is None:
            holds.append(
                checks._check(
                    "inbox_download_time",
                    "hold",
                    f'no download time recorded: add {inbox.sidecar_name} with "downloaded_at"; the time '
                    f"the file was placed ({inbox.placed_at}) is not a retrieval time",
                    key=inbox.key,
                    placed_at=inbox.placed_at,
                    sidecar=sidecar,
                )
            )
        if self._last_written(f"{dataset}/file") is None and inbox.reviewed_sha256 != sha:
            holds.append(
                checks._check(
                    "first_file_review",
                    "hold",
                    f"the first download of Ember's {dataset} file waits for a person's check: read this "
                    f'check\'s `summary`, and if it is right add "reviewed_sha256": "{sha}" to '
                    f"{inbox.sidecar_name}",
                    key=inbox.key,
                    sha256=sha,
                    sidecar={**sidecar, "reviewed_sha256": sha},
                    dataset_basis=kind.basis,
                )
            )
        return holds

    # -------------------------------------------------------------------------- finish
    def _revisions(self, frame: _Frame) -> tuple[dict[str, int], pd.DataFrame]:
        """Counts of values new, changed and gone against the last run that wrote this dataset, and
        the changed and gone values themselves. Values before the request's window start left the
        window (the monthly series rolls forward a month at a time) and are not counted."""
        current = frame.df[["record_id", "value"]]
        last = self._last_written(frame.label)
        path = self.store.normalized_path(SOURCE_ID, last[0]) if last else None
        if path is None or not self.store.exists(path):
            return {"new": len(current), "changed": 0, "gone": 0}, pd.DataFrame()
        before = self.store.read_parquet(path)
        before = before[(before["dataset"] == frame.dataset) & (before["origin"] == frame.origin)]
        if frame.window_start:
            before = before[before["period"].astype(str) >= frame.window_start]
        merged = before[["record_id", "value"]].merge(
            current, on="record_id", how="outer", suffixes=("_before", "_after"), indicator=True
        )
        new = merged[merged["_merge"] == "right_only"]
        gone = merged[merged["_merge"] == "left_only"]
        both = merged[merged["_merge"] == "both"]
        changed = both[(both["value_before"] - both["value_after"]).abs() > 1e-9]
        counts = {"new": len(new), "changed": len(changed), "gone": len(gone)}
        if changed.empty and gone.empty:
            return counts, pd.DataFrame()
        parts = [changed.assign(kind="changed"), gone.assign(kind="gone")]
        revisions = pd.concat([p for p in parts if not p.empty], ignore_index=True)
        return counts, revisions[["record_id", "value_before", "value_after", "kind"]]

    def _baseline(self, frame: _Frame) -> tuple[str, dict[str, Any]] | None:
        """The other route's last load of the same dataset (for a route's first load)."""
        for origin in ORIGINS:
            label = f"{frame.dataset}/{origin}"
            if origin == frame.origin:
                continue
            last = self._last_written(label)
            if last is not None and last[1].get("profile"):
                return label, dict(last[1]["profile"])
        return None

    def _check_frame(self, frame: _Frame, record: dict[str, Any]) -> str:
        """Run the checks on one frame, record them and the dataset's entry; returns the level."""
        prof = checks.profile(frame.df, observations=frame.observations, missing=frame.missing)
        last = self._last_written(frame.label)
        found = checks.run_checks(
            frame.df,
            prof,
            last[1].get("profile") if last else None,
            unmapped=frame.report.unmapped,
            uncoded=frame.report.uncoded_countries,
            baseline=self._baseline(frame) if last is None else None,
        )
        unknown = unknown_iso3(set(frame.df["iso3"].dropna().astype(str))) if not frame.df.empty else []
        if unknown:
            found.append(
                checks._check(
                    "iso3_unmapped", "warn", f"{len(unknown)} country codes kept as alpha-3", codes=unknown
                )
            )
        for hold in frame.holds:
            if hold["check"] == "first_file_review" and frame.kind is not None:
                hold["data"]["summary"] = review_summary(frame.df, prof, frame.kind)
            found.append(hold)
        for c in found:
            c["data"]["dataset"] = frame.label
        record["dq"]["checks"].extend(found)
        level = checks.status_of(found)
        record["datasets"][frame.label] = {
            "rows": len(frame.df),
            "written": False,
            "dq_status": level,
            "profile": prof,
            "parse": frame.report.to_dict(),
            "revisions": {},
            "window_start": frame.window_start,
            "retrieved_at": str(frame.df["retrieved_at"].iloc[0]) if not frame.df.empty else None,
            "source_url": str(frame.df["source_url"].iloc[0]) if not frame.df.empty else None,
        }
        record["rows_seen"] += len(frame.df)
        return level

    def _finish(
        self,
        ts: str,
        record: dict[str, Any],
        *,
        frames: list[_Frame],
        status: str | None,
        error: BaseException | None,
    ) -> RunResult:
        written: list[_Frame] = []
        revisions: list[pd.DataFrame] = []
        held = any(c["level"] == "hold" for c in record["dq"]["checks"])
        for frame in frames:
            if self._check_frame(frame, record) == "hold":
                held = True
                if frame.origin == "file":
                    frame.source_ref["state"] = "held"
                continue
            counts, revs = self._revisions(frame)
            record["datasets"][frame.label].update({"written": True, "revisions": counts})
            record["rows_new"] += counts["new"]
            record["rows_changed"] += counts["changed"]
            record["rows_gone"] += counts["gone"]
            if not revs.empty:
                revisions.append(revs.assign(dataset=frame.label))
            if frame.origin == "api":
                frame.source_ref["written"] = True
            else:
                frame.source_ref["state"] = "loaded"
            written.append(frame)
        if written:
            out = pd.concat([f.df for f in written], ignore_index=True)
            path = self.store.write_parquet(self.store.normalized_path(SOURCE_ID, ts), out)
            record["outputs"]["normalized"] = self.store.locate(path)
            record["outputs"]["datasets"] = sorted(f.label for f in written)
        if revisions:
            rpath = self.store.root / "revisions" / SOURCE_ID / f"{ts}.parquet"
            self.store.write_parquet(rpath, pd.concat(revisions, ignore_index=True))
            record["outputs"]["revisions"] = self.store.locate(rpath)
        if status is None:
            status = "partial" if held else "ok" if written else "unchanged"
        record["status"] = status
        if error is not None:
            record["error"] = redact(f"{type(error).__name__}: {error}", [self.key])[:2000]
            record["error_class"] = type(error).__name__
        level = checks.status_of(record["dq"]["checks"])
        record["dq"]["status"] = level
        record["dq_status"] = {"pass": "pass", "warn": "warn", "hold": "fail"}[level]
        record["finished_at"] = _iso(self.clock())
        record["egress_class"] = "plain"
        record["events_emitted"] = 0
        run_path = self.store.write_run(SOURCE_ID, ts, record)
        return RunResult(record=record, run_path=run_path, ts=ts)


def check_file(
    body: bytes, *, name: str, downloaded_at: str | None = None, dataset: str | None = None
) -> dict[str, Any]:
    """What a person's download would load as, before it goes in the inbox (the `check` command): the
    dataset and why, the review summary, the checks a first load would meet (no previous run to compare),
    and the sidecar to place beside it. Writes nothing and asks no host."""
    sha = hashlib.sha256(body).hexdigest()
    raw = RawSnapshot(
        content=body, content_type="text/csv", url=name, retrieved_at=utcnow(), http_status=200, ext="csv"
    )
    try:
        observations, report = parse_csv(raw)
    except ParseError as exc:
        return {"ok": False, "sha256": sha, "error": str(exc)}
    kind = classify_file(observations, name=name, declared=dataset)
    if kind.dataset is None:
        return {"ok": False, "sha256": sha, "error": kind.hold, "dataset_basis": kind.basis}
    if kind.dated_years:
        observations = as_yearly(observations)
    df = normalize(
        observations,
        dataset=kind.dataset,
        origin="file",
        source_url=DATASET_PAGES[kind.dataset],
        retrieved_at=raw.retrieved_at_iso,
        licence_id="check",
        report=report,
    )
    prof = checks.profile(df, observations=_known(observations), missing=_missing(observations, FILE_METRICS))
    found = checks.run_checks(df, prof, None, unmapped=report.unmapped, uncoded=report.uncoded_countries)
    when = _parse_time(downloaded_at) if downloaded_at else None
    return {
        "ok": checks.status_of(found) != "hold",
        "sha256": sha,
        "summary": review_summary(df, prof, kind),
        "checks": [c for c in found if c["level"] != "pass"],
        "parse": report.to_dict(),
        "sidecar": {
            "downloaded_at": _iso(when) if when else "<when you downloaded it, ISO 8601>",
            "dataset": kind.dataset,
            "reviewed_sha256": sha,
        },
    }


def run(
    store: Store,
    *,
    trigger: str = "manual",
    run_id: str | None = None,
    registry: Registry | None = None,
    http: PoliteSession | None = None,
    env: Mapping[str, str] | None = None,
    clock: Callable[[], dt.datetime] = utcnow,
) -> RunResult:
    return EmberRun(
        store, registry=registry, http=http, env=env, clock=clock, trigger=trigger, run_id=run_id
    ).run()
