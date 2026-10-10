"""Connector base contract (docs/20 §3.1, docs/04 E-4/E-17, docs/21 §3 provenance quartet).

A connector is three pure steps:

    fetch()            -> RawSnapshot         network, bytes in, nothing parsed
    parse(RawSnapshot) -> list[dict]          source-shaped rows, never touches the network
    normalize(rows)    -> DataFrame           canonical schema + provenance on every row

`fetch` never runs in CI; `parse` and `normalize` run on recorded fixtures (docs/04 E-6).
Every emitted record carries `source_id, source_url, retrieved_at, licence_id, raw`
(`CLAUDE.md`; docs/04 DA-2) plus a `record_id` = `{source_id}:{source_record_id}` that is stable
across runs (docs/20 §3.1: queue id, notice id, or a content hash of the identifying columns —
the strategy is documented in each connector's docstring).
"""

from __future__ import annotations

import ast
import datetime as dt
import hashlib
import inspect
import json
import math
import pathlib
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, ClassVar, Literal

import pandas as pd

from pipeline.connectors.canonical import CANONICAL_COLUMNS as _PROPOSAL_BASE
from pipeline.connectors.canonical import load_status_map
from pipeline.connectors.dedupe import suffix_duplicates
from pipeline.connectors.http import PoliteSession
from pipeline.connectors.registry import SourceEntry

Kind = Literal["proposal", "opportunity", "document"]
Egress = Literal["plain", "browser", "residential", "api_key"]
SnapshotMode = Literal["full", "incremental"]
#: What a row that disappears from a full-register source means there (`Connector.removal_meaning`).
RemovalMeaning = Literal["withdrawn", "completed", "closed", "unknown"]
REMOVAL_MEANINGS: tuple[RemovalMeaning, ...] = ("withdrawn", "completed", "closed", "unknown")

PROVENANCE = ("source_id", "source_url", "retrieved_at", "licence_id")

#: Shared modules whose code decides what `parse`/`normalize` produce for every connector; their
#: digest is part of each connector's effective parser version (`Connector.parser_digest`).
_SHARED_PARSER_MODULES = (
    "pipeline/connectors/base.py",
    "pipeline/connectors/canonical.py",
    "pipeline/connectors/dedupe.py",
    "pipeline/connectors/iso_queue.py",
    "pipeline/connectors/opportunity.py",
    "pipeline/normalize.py",
    "pipeline/status_map.yaml",
    "pipeline/vendor/gridstatus/queues.py",
)
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_DIGESTS: dict[type, str] = {}

# docs/21 §3.1 proposal fields as already produced by pipeline/normalize.py, with the store
# spelling `licence_id` (docs/04 §10) and the jsonb-style `raw` payload appended.
PROPOSAL_COLUMNS: list[str] = [c if c != "licence" else "licence_id" for c in _PROPOSAL_BASE] + ["raw"]

# docs/21 §3.3 opportunity fields (kind, issuer, title, jurisdiction, technologies, open_at,
# due_at, status) plus identity, provenance and the audit columns every record carries.
OPPORTUNITY_COLUMNS: list[str] = [
    "record_id",
    "source_id",
    "source_record_id",
    "source_url",
    "retrieved_at",
    "licence_id",
    "kind",
    "issuer",
    "title",
    "summary",
    "jurisdiction",
    "technologies",
    "capacity_sought_mw",
    "budget_amount",
    "budget_currency",
    "open_at",
    "due_at",
    "status",
    "status_raw",
    "status_rule",
    "identifiers",
    "raw",
]

# docs/21 §3.8 `document` entity — the stage-evidence shape for docket/filing feeds (docs/22 §10
# "docket linkage"). A connector of this kind ingests filings, not lifecycle entities: `raw`
# carries the source-shaped hit, and the three fields below exist only so this kind can flow
# through the same DQ-gate/diff machinery as proposal and opportunity rows (`docs/20` §3.3):
# `lifecycle_state` is a constant ("filed") since a filing does not change state once accessioned,
# and `capacity_mw`/`proposed_cod` stay null. Subject linkage (`subject_type`/`subject_id`),
# object storage and text extraction are populated by a later enrichment stage, not the connector.
DOCUMENT_COLUMNS: list[str] = [
    "record_id",
    "source_id",
    "source_record_id",
    "source_url",
    "retrieved_at",
    "licence_id",
    "doc_type",
    "title",
    "published_date",
    "accession_number",
    "docket_refs",
    "filer",
    "affiliations",
    "document_class",
    "document_type",
    "project_name_hint",
    "state_hint",
    "identifiers",
    "lifecycle_state",
    "status_raw",
    "status_rule",
    "capacity_mw",
    "proposed_cod",
    "raw",
]


class ConnectorError(Exception):
    """A fetch failed in a way the run should record as `failed` (docs/04 E-17)."""


class ParseError(ConnectorError):
    """The payload was fetched but is not what the parser expects (e.g. HTML instead of xlsx)."""


class BlockedError(ConnectorError):
    """robots.txt or a challenge page refused the fetch; the run is recorded as `blocked`."""


class GateViolation(Exception):
    """A publication gate would be crossed. Never caught-and-continued (docs/04 E-17)."""


@dataclass
class RawSnapshot:
    """Bytes as fetched plus the HTTP metadata the `snapshot` row needs (docs/21 §4.3)."""

    content: bytes
    content_type: str
    url: str
    retrieved_at: dt.datetime
    http_status: int
    ext: str
    headers: dict[str, str] = field(default_factory=dict)
    elapsed_s: float = 0.0
    requests_made: int = 1
    redacted: bool = False
    meta: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.retrieved_at.tzinfo is None:
            raise ValueError("retrieved_at must be timezone-aware UTC (docs/04 E-4)")

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.content).hexdigest()

    @property
    def retrieved_at_iso(self) -> str:
        return self.retrieved_at.astimezone(dt.UTC).isoformat(timespec="seconds").replace("+00:00", "Z")

    @classmethod
    def from_file(
        cls,
        path: pathlib.Path,
        url: str,
        content_type: str = "application/octet-stream",
        retrieved_at: dt.datetime | None = None,
        **meta: Any,
    ) -> RawSnapshot:
        """Build a snapshot from a recorded fixture (tests only; `fetch` never runs in CI)."""
        return cls(
            content=path.read_bytes(),
            content_type=content_type,
            url=url,
            retrieved_at=retrieved_at or dt.datetime(2026, 9, 12, tzinfo=dt.UTC),
            http_status=200,
            ext=path.suffix.lstrip("."),
            meta=meta,
        )


@dataclass
class PreviousSnapshot:
    """The stored snapshot this run's bytes will be compared against (`Store.last_snapshot`, the
    same one `Store.last_snapshot_sha` names), handed to the connector before `fetch()`.

    `record` is the latest run record that produced or matched it, so its `snapshot.meta` carries
    whatever validators the connector kept last time (`Last-Modified`, `ETag`). A connector that
    asks upstream with a conditional request and hears 304 returns `content()` unchanged; the
    runner's own SHA comparison then records the run `unchanged` (docs/20 §3.2). Nothing else
    about an unchanged run differs from a byte-identical re-download."""

    record: dict[str, Any]
    load: Callable[[], bytes]
    _content: bytes | None = field(default=None, repr=False)

    @property
    def sha256(self) -> str:
        return str((self.record.get("snapshot") or {}).get("sha256") or "")

    @property
    def meta(self) -> dict[str, Any]:
        meta = (self.record.get("snapshot") or {}).get("meta")
        return meta if isinstance(meta, dict) else {}

    def content(self) -> bytes:
        """The stored bytes, read once and checked against the recorded SHA-256."""
        if self._content is None:
            body = self.load()
            if hashlib.sha256(body).hexdigest() != self.sha256:
                raise ConnectorError("stored snapshot does not match its recorded sha256")
            self._content = body
        return self._content


@dataclass(frozen=True)
class FetchWindow:
    """The span an incremental connector asks upstream for (audit 2026-09-30 F3, F12).

    `anchor` says how `start` was chosen: `rolling` (no promoted run to anchor on: the connector's
    `window_days` back from now), `watermark` (the start of the UTC day of the last promoted run's
    `retrieved_at` minus `window_overlap`, so a run after a gap of any length up to
    `max_catchup_days` asks for everything since the last run that reached `normalized/`) or
    `clamped` (the gap is longer than `max_catchup_days`: the window starts there and the run
    records the loss it cannot recover)."""

    start: dt.datetime
    end: dt.datetime
    anchor: str
    watermark: dt.datetime | None = None

    @property
    def days(self) -> float:
        return max((self.end - self.start).total_seconds() / 86400.0, 0.0)

    def meta(self) -> dict[str, Any]:
        return {
            "window_start": self.start.astimezone(dt.UTC).isoformat(timespec="seconds"),
            "window_end": self.end.astimezone(dt.UTC).isoformat(timespec="seconds"),
            "window_anchor": self.anchor,
            "watermark": (
                self.watermark.astimezone(dt.UTC).isoformat(timespec="seconds") if self.watermark else None
            ),
        }


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _floor_day(t: dt.datetime) -> dt.datetime:
    t = t.astimezone(dt.UTC)
    return dt.datetime(t.year, t.month, t.day, tzinfo=dt.UTC)


def _code_digest(path: pathlib.Path) -> str:
    """Digest of what a file *does*: a Python module's AST without docstrings (so a comment or
    docstring edit is not a parser change), a YAML file's parsed content, else its bytes."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return "missing"
    if path.suffix == ".py":
        tree = ast.parse(text)
        for node in ast.walk(tree):
            body = getattr(node, "body", None)
            if not (isinstance(body, list) and body and isinstance(body[0], ast.Expr)):
                continue
            first = body[0].value
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                body.pop(0)
        text = ast.dump(tree, annotate_fields=False, include_attributes=False)
    elif path.suffix in (".yaml", ".yml"):
        import yaml

        text = json.dumps(yaml.safe_load(text), sort_keys=True, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def json_default(v: Any) -> Any:
    if isinstance(v, (dt.datetime, dt.date, pd.Timestamp)):
        return v.isoformat()
    if isinstance(v, float) and pd.isna(v):
        return None
    if v is pd.NA or v is pd.NaT:
        return None
    if hasattr(v, "item"):  # numpy scalars
        return v.item()
    return str(v)


def raw_json(row: dict[str, Any]) -> str:
    """Serialise a source-shaped row as the jsonb-style `raw` payload (NaN -> null)."""
    clean = {str(k): (None if _isna(v) else v) for k, v in row.items()}
    return json.dumps(clean, default=json_default, ensure_ascii=False, sort_keys=True)


def _isna(v: Any) -> bool:
    if v is None:
        return True
    try:
        return bool(pd.isna(v)) if not isinstance(v, (list, dict, tuple, set)) else False
    except (TypeError, ValueError):
        return False


def content_hash(*parts: Any) -> str:
    """Deterministic `source_record_id` for sources without a stable id (docs/20 §3.1)."""
    key = "|".join("" if _isna(p) else str(p).strip() for p in parts)
    return "h" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]  # noqa: S324 - identity key, not security


class Connector:
    """Base class. Subclasses set the class attributes and implement fetch/parse/normalize."""

    source_id: ClassVar[str]
    kind: ClassVar[Kind]
    egress: ClassVar[Egress] = "plain"
    ext: ClassVar[str] = "bin"
    #: The declared parser version. Bump it on a deliberate change of what the parser produces; the
    #: version a run records (`effective_parser_version`) also carries a digest of the parser's code
    #: and status map, so a change nobody bumped still moves it (audit 2026-09-30 F10). A run whose
    #: effective version differs from the previous promoted run's restates that run's output under
    #: the current code before the diff: a parser fix is a restatement, never news (runner docstring).
    parser_version: ClassVar[str] = "1.0.0"
    honour_robots: ClassVar[bool] = True
    #: Incremental windows (audit 2026-09-30 F3, F12): the rolling span a run asks for when no
    #: promoted run anchors it (`fetch_window`); 0 for full-register sources.
    window_days: ClassVar[int] = 0
    #: How far before the last promoted run's `retrieved_at` an anchored window starts (rounded
    #: down to the UTC day), so late-published and re-dated records are fetched again.
    window_overlap: ClassVar[dt.timedelta] = dt.timedelta(days=1)
    #: The longest gap a run catches up on; beyond it the window is clamped and the run says so.
    max_catchup_days: ClassVar[int] = 60
    #: How many times its normal page cap a catch-up run may page, so a long gap is not truncated.
    max_catchup_page_factor: ClassVar[int] = 10
    #: A register that can legitimately hold no rows (ERCOT's unpublished large-load report); for
    #: any other source a run of zero rows is held (audit F13).
    may_be_empty: ClassVar[bool] = False
    #: Normalised column dating each row's publication, for the weekday-aware row-count gate of an
    #: incremental source (`pipeline/connectors/dq.py`, audit F6). None: no daily profile.
    window_date_column: ClassVar[str | None] = None
    #: "full": the payload is the whole register, so a row that disappears is a `removed` event.
    #: "incremental": the payload is a window (last N days); rows are upserted onto the previous
    #: normalised snapshot and disappearance means nothing.
    snapshot_mode: ClassVar[SnapshotMode] = "full"
    #: What a `removed` diff event means at this source (2026-10-10, docs/51 §2.7 item 1). A row
    #: that is no longer in the file is not by itself news: an EIA-860M unit leaves the Planned
    #: sheet when it starts operating, a grants.gov notice leaves the search when it closes.
    #: `withdrawn` only where the source itself says a row leaves because the request was withdrawn;
    #: `completed` where it leaves only on reaching operation; `closed` where it leaves only when
    #: the notice closes; `unknown` (the default) everywhere else. The loader publishes a removal
    #: as `withdrawn` for the first value alone; every other removal is stored as a non-public
    #: `removed_from_source` event carrying this value (`services/ingest/loader.py`), unless the
    #: connector announces removals (below). An incremental source never emits `removed`, so the
    #: value is moot there.
    removal_meaning: ClassVar[RemovalMeaning] = "unknown"
    #: Whether a removal at this source is announced publicly (owner decision 2026-10-10). This is a
    #: publication choice, not a statement of what a removal means: `removal_meaning` stays what the
    #: source says. When True and the meaning is `unknown`, the loader writes the public, alertable
    #: `delisted` event, worded "No longer in <register_name>'s report (reason not stated)" and kept
    #: out of social drafts; it never says "withdrawn". Set on the full-register interconnection
    #: queues only (ERCOT, CAISO, NYISO, NESO), where leaving the register is queue news whatever
    #: the reason. EIA-860M keeps the non-public `removed_from_source`: a unit that leaves its
    #: Planned sheet may have started operating, so even "no longer in the report" would mislead.
    announce_removals: ClassVar[bool] = False
    #: The register's display name in that sentence, the short name record pages print for the
    #: source ("ERCOT"). Required where `announce_removals` is True. A removal is announced only
    #: when no row of the current frame belongs to the same project (`project_root`).
    register_name: ClassVar[str | None] = None
    #: key of this source in its status_map.yaml `sources:` block
    status_key: ClassVar[str] = ""
    #: per-connector status map (docs/04 DA-5); None = pipeline/status_map.yaml
    status_map_path: ClassVar[pathlib.Path | None] = None
    #: "hold": duplicate source_record_id holds the run (docs/04 DA-6);
    #: "suffix": the parser has no stable composite key; every member of a duplicated id gets a
    #: content-derived `#h…` suffix (`pipeline/connectors/dedupe.py`, order-independent) and the
    #: run records a DQ warning — must be justified in the docstring.
    dedupe_strategy: ClassVar[Literal["hold", "suffix"]] = "hold"
    #: canonical fields whose null rate is watched (docs/04 DA-6 "null spike")
    dq_required_fields: ClassVar[tuple[str, ...]] = ()
    #: source columns that feed canonical fields; their removal holds the run (schema drift)
    key_source_columns: ClassVar[tuple[str, ...]] = ()
    #: raw-column names to strip before the snapshot is stored (docs/13 §5.4 rule 1)
    personal_data_columns: ClassVar[tuple[str, ...]] = ()
    #: Another source id whose stored snapshot is the same upstream file (`data/sources.yaml`:
    #: "fetch shared with ..."). The runner hands that source's latest snapshot over as `shared`
    #: before `fetch()`, so a conditional request answered 304 reuses those bytes instead of
    #: downloading the file a second time. None for a connector that fetches on its own.
    shares_fetch_with: ClassVar[str | None] = None

    def __init__(self, source: SourceEntry, http: PoliteSession | None = None) -> None:
        if source.id != self.source_id:
            raise ValueError(f"{type(self).__name__} is bound to {self.source_id}, got {source.id}")
        self.source = source
        self.http = http or PoliteSession(rate_limits={source.host: source.max_rps})
        self._status_map: dict[str, Any] | None = None
        #: Set by the runner before `fetch()` (`PreviousSnapshot`); None on a first run, a replay,
        #: or when the stored object is gone. Only a connector that makes conditional requests reads it.
        self.previous: PreviousSnapshot | None = None
        #: The latest snapshot of `shares_fetch_with`, set by the runner the same way; None otherwise.
        self.shared: PreviousSnapshot | None = None
        #: `retrieved_at` of the last promoted run (its output reached `normalized/`), set by the
        #: runner before `fetch()`; an incremental connector anchors its window on it.
        self.watermark: dt.datetime | None = None
        #: The run's clock (the runner sets it when a caller fixes `now`); `fetch` reads `self.now()`.
        self.clock: Callable[[], dt.datetime] = utcnow

    # ------------------------------------------------------------------ identity
    @classmethod
    def project_root(cls, record_key: str) -> str:
        """The project a record key belongs to. `record_key` is the record id without its
        `<source_id>:` prefix, which is also the stored link key. Where one project can be listed
        under several keys (NESO's `<pid>/<stage>` and `<pid>#<n>`, NYISO's `#h...` suffix on a
        repeated queue position), a key that disappears while another key of the same project is
        still listed is a re-key, not a departure, and the loader never announces it
        (`services/ingest/loader.py`; lane E2 follow-up, 2026-10-10). Default: the key itself, for a
        source with no such convention. Must be a pure function of the key."""
        return record_key

    # ------------------------------------------------------------------ versions
    @classmethod
    def parser_digest(cls) -> str:
        """Eight hex characters over the code that turns bytes into this connector's rows: every
        connector module in the class's MRO, its status map and the shared parsing modules."""
        cached = _DIGESTS.get(cls)
        if cached is not None:
            return cached
        paths: list[pathlib.Path] = []
        for klass in cls.__mro__:
            if klass is Connector or not (isinstance(klass, type) and issubclass(klass, Connector)):
                continue
            src = inspect.getsourcefile(klass)
            if src:
                paths.append(pathlib.Path(src))
        if cls.status_map_path is not None:
            paths.append(pathlib.Path(cls.status_map_path))
        paths.extend(_REPO_ROOT / p for p in _SHARED_PARSER_MODULES)
        h = hashlib.sha256()
        for path in sorted(set(paths)):
            h.update(_code_digest(path).encode("ascii"))
        digest = h.hexdigest()[:8]
        _DIGESTS[cls] = digest
        return digest

    @classmethod
    def effective_parser_version(cls) -> str:
        """`{declared}+{digest}`: what a run record names as the code that produced its rows."""
        return f"{cls.parser_version}+{cls.parser_digest()}"

    # ------------------------------------------------------------------ windows
    def now(self) -> dt.datetime:
        return self.clock()

    def fetch_window(self, now: dt.datetime | None = None) -> FetchWindow:
        """The span this run asks upstream for (`FetchWindow`). Anchored on the last promoted run
        when there is one, so a gap of any length up to `max_catchup_days` is caught up and a run
        right after the last one asks only for what is new plus the overlap; otherwise the rolling
        `window_days`."""
        now = now or self.now()
        if self.watermark is None or self.watermark > now:
            return FetchWindow(now - dt.timedelta(days=self.window_days), now, "rolling")
        start = _floor_day(self.watermark - self.window_overlap)
        floor = now - dt.timedelta(days=self.max_catchup_days)
        if start < floor:
            return FetchWindow(floor, now, "clamped", self.watermark)
        return FetchWindow(start, now, "watermark", self.watermark)

    def page_cap(self, base_pages: int, window: FetchWindow) -> int:
        """Pages a run may request: the connector's normal cap, scaled up in proportion when a
        catch-up window is longer than its rolling `window_days` (bounded by
        `max_catchup_page_factor`). A run that still hits the cap marks its snapshot
        `meta["truncated"]` and is held (audit F3: a page cap used to truncate silently)."""
        if not self.window_days or window.days <= self.window_days:
            return base_pages
        factor = min(math.ceil(window.days / self.window_days), self.max_catchup_page_factor)
        return base_pages * max(factor, 1)

    def row_dates(self, df: pd.DataFrame, rows: list[dict[str, Any]]) -> pd.Series | None:
        """Publication date per normalised row (index-aligned with `df`) for the weekday-aware
        row-count gate; None when the connector declares no `window_date_column`."""
        col = self.window_date_column
        if not col or col not in df.columns:
            return None
        return pd.to_datetime(df[col], errors="coerce", utc=True)

    # ------------------------------------------------------------------ contract
    def fetch(self) -> RawSnapshot:
        raise NotImplementedError

    def parse(self, raw: RawSnapshot) -> list[dict[str, Any]]:
        raise NotImplementedError

    def normalize(self, rows: list[dict[str, Any]], raw: RawSnapshot) -> pd.DataFrame:
        raise NotImplementedError

    def restate_status(self, df: pd.DataFrame) -> pd.DataFrame | None:
        """`lifecycle_state` and `status_rule` for each row of a previously stored normalised frame
        of this source, recomputed from the row's own `raw` payload under the *current* status map
        (index-aligned with `df`). The runner diffs against the restated frame, so a status-map
        correction is a silent reclassification recorded on the run, never a `status_change`
        event (docs/22 §8). Default None: this connector cannot restate, and the stored states
        are diffed as they are."""
        return None

    def restate_capacity(self, df: pd.DataFrame) -> pd.Series | None:
        """`capacity_mw` for each row of a previously stored normalised frame of this source,
        recomputed from the row's own `raw` payload under the *current* capacity rule
        (index-aligned with `df`; null where the row cannot be read). Same purpose as
        `restate_status`: a corrected derivation is restated before the diff, so it is never
        published as a `capacity_change`. Default None: nothing to restate."""
        return None

    def redact(self, content: bytes) -> bytes:
        """Strip contact identifiers before the snapshot is stored; default: nothing to strip."""
        return content

    def canonical_content(self, content: bytes) -> bytes:
        """What the unchanged short-circuit compares (runner step 2): the redacted payload without
        the bytes that differ on every request while the data does not, such as a request echo
        carrying the current time or a per-response token (review 2026-10-10 §2.7 item 6). Only
        the comparison reads it: the stored snapshot keeps the redacted bytes. Must be a pure
        function of `content`, and return `content` unchanged when it cannot read it, so an
        unreadable payload is compared byte for byte. Default: the bytes themselves."""
        return content

    # ------------------------------------------------------------------ helpers
    @property
    def status_map(self) -> dict[str, Any]:
        if self._status_map is None:
            path = self.status_map_path
            self._status_map = load_status_map(path) if path else load_status_map()
        return self._status_map

    @property
    def columns(self) -> list[str]:
        if self.kind == "proposal":
            return PROPOSAL_COLUMNS
        if self.kind == "opportunity":
            return OPPORTUNITY_COLUMNS
        return DOCUMENT_COLUMNS

    def finalize(self, df: pd.DataFrame, rows: list[dict[str, Any]], raw: RawSnapshot) -> pd.DataFrame:
        """Stamp provenance, build record_id, attach `raw`, order columns, resolve duplicates.

        `rows` and `df` must be aligned row-for-row (the parser's rows are the `raw` payload).
        """
        if len(rows) != len(df):
            raise ParseError(f"normalize produced {len(df)} rows for {len(rows)} parsed rows")
        out = df.copy().reset_index(drop=True)
        out["source_id"] = self.source_id
        out["retrieved_at"] = raw.retrieved_at_iso
        out["licence_id"] = self.source.licence_id
        if "source_url" not in out.columns:
            out["source_url"] = raw.url
        out["source_url"] = out["source_url"].fillna(raw.url)
        if "licence" in out.columns:
            out = out.drop(columns=["licence"])
        out["source_record_id"] = out["source_record_id"].astype("string").str.strip()
        missing_id = out["source_record_id"].isna() | (out["source_record_id"] == "")
        if bool(missing_id.any()):
            raise ParseError(f"{int(missing_id.sum())} rows without source_record_id")
        out["record_id"] = self.source_id + ":" + out["source_record_id"]
        raw_payloads = [raw_json(r) for r in rows]
        if self.dedupe_strategy == "suffix":
            # Content-derived, order-independent suffix on every member of a duplicated group
            # (pipeline/connectors/dedupe.py; audit 2026-09-18 item 1). Never positional.
            out["record_id"], resolved = suffix_duplicates(out, raw_payloads)
            out.attrs["duplicates_resolved"] = resolved
        else:
            out.attrs["duplicates_resolved"] = 0
        out["raw"] = raw_payloads
        for c in self.columns:
            if c not in out.columns:
                out[c] = None
        out = out[self.columns]
        for c in ("source_record_id", "record_id", "source_url", "licence_id", "source_id", "raw"):
            out[c] = out[c].astype("string")
        return out


def to_parquet_safe(df: pd.DataFrame) -> pd.DataFrame:
    """Parquet needs homogeneous columns: object columns become strings, lists become JSON."""
    out = df.copy()
    for c in out.columns:
        if out[c].dtype == object:
            out[c] = (
                out[c]
                .map(
                    lambda v: (
                        json.dumps(v, default=json_default)
                        if isinstance(v, (list, dict))
                        else (None if _isna(v) else str(v))
                    )
                )
                .astype("string")
            )
    return out
