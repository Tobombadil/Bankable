"""us.rfp.<slug> - US utility and state RFPs, one connector driven by the curated issuer registry.

docs/28 (utility RFP registry) decides, issuer by issuer, what a connector may do; the registry is
`data/vendored/rfp_issuers/issuers.yaml`. This module is the one connector for all of them: each
`data/sources.yaml` row with `connector_module: us_rfp` is bound here, and
`connector_class_for(source_id)` builds that issuer's connector class from its registry row. A row
the registry does not clear (`issuers.IssuerRow.refusal`: `automated_reading` other than `allowed`,
a verdict other than `cleared`, no `connector` block) gets no class at all, so it cannot be fetched
by any path. Tranche 1 (docs/28 §8.1) is the six cleared issuers with a listing: PSO, Appalachian
Power, SWEPCO and Indiana Michigan Power (one AEP page template), Connecticut DEEP (news-release
index) and New Jersey BPU (public-notice table and notice PDFs). The page formats are in
`extract.py`, the classifier and fact readers in `classify.py`.

Fetch: the listing (and its per-item pages, `extract.py`), politely: identified User-Agent,
robots.txt per host, at most 0.5 rps per host and slower when robots.txt declares a
`Crawl-delay`, conditional requests where the host gives validators, no further request in the run
after a 401/403/451 or a challenge (the run is `blocked`; the next scheduled tick asks once more,
`extract.py` docstring). Monthly, the row's terms page is re-read and every quoted operative clause must still
be on it (`extract.terms_check`); if one is gone the run is `blocked` until a reviewer re-reads the
terms. The snapshot is one JSON bundle of page regions (`extract.py` docstring); solicitation
documents are never fetched or stored. Contact e-mail addresses and phone numbers are removed from
the stored regions (`redact`, docs/13 §5.4 rule 1).

Parse: every listing item, classified (`classify.classify`); only `generation` items are records.
Sell-side, fuel and gas-infrastructure, transmission-only, services and non-energy items, awards,
RFIs and stakeholder notices are left out of the published kind, counted on the snapshot's
`meta.left_out` by rule.

source_record_id: AEP, the item's per-RFP page path (with its fragment, since APCo's storage PSA
and CPA share a page), else a hash of its title; CT DEEP, the release's path; NJ BPU, the docket
number the notice states (notices on one docket are one solicitation), else the notice's path.

Unchanged check: a CT listing changes whenever DEEP posts anything (bat week, boat launches), and its
look-back window moves daily, so `canonical_content` compares the extracted items, not the bytes.
A day whose window holds no new procurement release parses to no rows: an `ok` run that carries the
earlier releases forward and emits nothing (the source columns are declared, `parse`).

Empty registers: an AEP page always lists its archive, so an empty one is a template change and is
held (DQ zero rows). CT DEEP's window and NJ BPU's 400-day register of solicitation notices may
legitimately hold none (`may_be_empty`); a missing listing table is still a `ParseError`.

Snapshot mode: AEP and NJ BPU are whole registers (the page lists every current item; an item that
leaves it is a `removed` event, stored non-public, `removal_meaning = unknown`); CT DEEP is a
window over a news index (`incremental`: earlier releases are carried forward).

Licence: each row's `data/sources.yaml` entry, which may be no broader than the registry row's
`manifest` (checked when the connector is built). The four AEP rows are `noncommercial`: gated
under `PLATFORM_POSTURE=commercial`, published under `noncommercial` (docs/26; `services/posture.py`).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import pathlib
import re
from typing import Any, ClassVar, cast
from urllib.parse import urlsplit

import pandas as pd

from pipeline.connectors.base import Connector as BaseConnector
from pipeline.connectors.base import (
    ConnectorError,
    GateViolation,
    Kind,
    ParseError,
    RawSnapshot,
    SnapshotMode,
    _code_digest,
)
from pipeline.connectors.canonical import harmonise_status
from pipeline.connectors.http import PoliteSession
from pipeline.connectors.opportunity import deadline_passed, technologies_str, to_utc
from pipeline.connectors.registry import RegistrationError, SourceEntry
from pipeline.connectors.us_rfp import extract
from pipeline.connectors.us_rfp.issuers import IssuerRow, registry_rows

HERE = pathlib.Path(__file__).resolve().parent
#: Modules of this package beside `connector.py` whose code decides what a run parses to; their
#: digest, and the issuer row's connector block, are part of the effective parser version.
PARSER_MODULES = ("classify.py", "extract.py", "htmltree.py", "issuers.py")
#: The most any host is asked, in requests per second (docs/28 §8.2: "0.5 rps or slower").
MAX_RPS = 0.5
#: How often the terms page is re-read (docs/28 §8.2: "re-read it monthly").
TERMS_CHECK_EVERY = dt.timedelta(days=30)
_REUSE_RANK = {"open": 4, "attribution": 3, "noncommercial": 2, "restricted": 1, "unknown": 1}
_PUBLICATION_RANK = {"raw_ok": 2, "derived_only": 1, "none": 0}
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE = re.compile(r"(?<!\d)(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}(?!\d)")


class UtilityRfpConnector(BaseConnector):
    """The family base; `connector_class_for` binds one subclass per cleared issuer row."""

    source_id: ClassVar[str]
    kind: ClassVar[Kind] = "opportunity"
    ext: ClassVar[str] = "json"
    honour_robots: ClassVar[bool] = True
    status_key: ClassVar[str] = "utility_rfp"
    status_map_path: ClassVar[pathlib.Path | None] = HERE / "status_map.yaml"
    #: `due_at` is not required: many listings state no deadline, and on a register of two to eight
    #: items one more dateless RFP would read as a null spike (pipeline/connectors/dq.py).
    dq_required_fields: ClassVar[tuple[str, ...]] = ("title", "issuer", "jurisdiction")
    key_source_columns: ClassVar[tuple[str, ...]] = ("key", "title", "url", "status_raw")
    issuer_row: ClassVar[IssuerRow]
    #: Read by the loader (`services.ingest.loader.connector_issuer_org_type`): the row's `issuer`
    #: becomes the record's issuer organisation, of this type, marked a curated issuer (US-303).
    issuer_org_type: ClassVar[str | None] = None

    def __init__(self, source: SourceEntry, http: PoliteSession | None = None) -> None:
        super().__init__(source, http)
        row = self.issuer_row
        manifest = row.manifest
        if _REUSE_RANK.get(source.reuse, 0) > _REUSE_RANK.get(
            str(manifest.get("reuse")), 0
        ) or _PUBLICATION_RANK.get(source.publication or "none", 0) > _PUBLICATION_RANK.get(
            str(manifest.get("publication")), 0
        ):
            raise GateViolation(
                f"{source.id}: data/sources.yaml says reuse={source.reuse!r}, "
                f"publication={source.publication!r}; the issuer registry's manifest allows at most "
                f"{manifest} (docs/28 §3.3)"
            )
        for host in {row.listing_host, urlsplit(row.terms_url).netloc.lower()} - {""}:
            self.http.rate_limits[host] = min(
                self.http.rate_limits.get(host, MAX_RPS), MAX_RPS, source.max_rps
            )

    # ------------------------------------------------------------------ versions
    @classmethod
    def parser_digest(cls) -> str:
        h = hashlib.sha256(super().parser_digest().encode("ascii"))
        for name in PARSER_MODULES:
            h.update(_code_digest(HERE / name).encode("ascii"))
        h.update(cls.issuer_row.config_digest().encode("ascii"))
        return h.hexdigest()[:8]

    # ------------------------------------------------------------------ fetch
    def fetch(self) -> RawSnapshot:
        row = self.issuer_row
        now = self.now()
        previous_bundle, previous_meta = self._previous()
        fx = extract.Fetcher(
            http=self.http,
            row=row,
            previous_pages={str(p["url"]): p for p in previous_bundle.get("pages") or [] if p.get("url")},
            previous_validators=dict(previous_meta.get("validators") or {}),
            max_rps=min(MAX_RPS, self.source.max_rps),
        )
        terms = previous_meta.get("terms_check")
        if not isinstance(terms, dict) or _older_than(terms.get("checked_at"), now, TERMS_CHECK_EVERY):
            terms = extract.terms_check(fx, now)
        bundle: dict[str, Any] = {
            "issuer": row.id,
            "extractor": row.extractor,
            "listing_url": row.listing_url,
        }
        if row.extractor == "aep_energy_rfps":
            pages, truncated = extract.fetch_aep(fx, now)
        elif row.extractor == "news_release_index":
            previous_at = (
                _parse_iso((self.previous.record.get("snapshot") or {}).get("retrieved_at"))
                if self.previous
                else None
            )
            since = extract.news_window_start(now, previous_at, row)
            bundle["window_start"] = since.date().isoformat()
            pages, truncated = extract.fetch_news(fx, now, since)
        elif row.extractor == "notice_table_pdf":
            pages, truncated = extract.fetch_notices(fx, now)
        else:  # issuers.refusal() admits only the three extractors
            raise ConnectorError(f"{row.id}: unknown extractor {row.extractor!r}")
        bundle["pages"] = pages
        content = json.dumps(bundle, ensure_ascii=False, sort_keys=True, indent=1).encode("utf-8")
        kept, left_out = extract.records(bundle, row, now.date())
        return RawSnapshot(
            content=content,
            content_type="application/json",
            url=row.listing_url.replace("{year}", str(now.year)),
            retrieved_at=now,
            http_status=200,
            ext="json",
            requests_made=self.http.requests_made,
            meta={
                "issuer": row.id,
                "extractor": row.extractor,
                "pages": len(pages),
                "truncated": truncated,
                "validators": fx.validators,
                "terms_check": terms,
                "notes": fx.notes,
                "items_kept": len(kept),
                "left_out": _left_out_counts(left_out),
            },
        )

    def _previous(self) -> tuple[dict[str, Any], dict[str, Any]]:
        """The previous run's bundle and snapshot meta, or empty ones (first run, unreadable)."""
        if self.previous is None:
            return {}, {}
        try:
            bundle = json.loads(self.previous.content())
        except (ConnectorError, ValueError, OSError):
            return {}, {}
        return (bundle if isinstance(bundle, dict) else {}), self.previous.meta

    def redact(self, content: bytes) -> bytes:
        """Contact e-mail addresses and phone numbers out of the stored regions (docs/13 §5.4
        rule 1): role mailboxes ("PSO2026RFP@aep.com") and press-office lines are contact details,
        and nothing the connector publishes needs them."""
        try:
            bundle = json.loads(content)
        except ValueError:
            return content
        changed = False
        for page in bundle.get("pages") or []:
            region = page.get("region")
            if isinstance(region, str):
                scrubbed = _PHONE.sub("[phone removed]", _EMAIL.sub("[email removed]", region))
                if scrubbed != region:
                    page["region"] = scrubbed
                    changed = True
        if not changed:
            return content
        return json.dumps(bundle, ensure_ascii=False, sort_keys=True, indent=1).encode("utf-8")

    def canonical_content(self, content: bytes) -> bytes:
        """The extracted items, not the bytes (module docstring, "Unchanged check")."""
        try:
            bundle = json.loads(content)
            kept, _ = extract.records(bundle, self.issuer_row, dt.date(2000, 1, 1))
        except Exception:  # unreadable: compared byte for byte
            return content
        kept = sorted(kept, key=lambda r: str(r["key"]))
        return json.dumps(kept, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")

    # ------------------------------------------------------------------ parse
    def parse(self, raw: RawSnapshot) -> list[dict[str, Any]]:
        try:
            bundle = json.loads(raw.content)
        except ValueError as exc:
            raise ParseError(f"{self.source_id}: the snapshot is not a JSON bundle") from exc
        if not isinstance(bundle, dict) or bundle.get("issuer") != self.source_id:
            raise ParseError(f"{self.source_id}: the bundle names issuer {bundle.get('issuer')!r}")
        kept, _ = extract.records(bundle, self.issuer_row, raw.retrieved_at.date())
        # Every record has the same keys (`extract.RECORD_KEYS`), so the source columns are declared
        # rather than read off the rows: a CT DEEP day with no new release parses to no rows, and the
        # DQ schema check would otherwise read that as every key column removed (live run 2026-10-21).
        raw.meta["source_header"] = list(extract.RECORD_KEYS)
        return kept

    def normalize(self, rows: list[dict[str, Any]], raw: RawSnapshot) -> pd.DataFrame:
        row = self.issuer_row
        now = raw.retrieved_at
        recs: list[dict[str, Any]] = []
        for r in rows:
            due = to_utc(r.get("due_at"))
            ctx = {"status_raw": r.get("status_raw"), "deadline_passed": deadline_passed(due, now)}
            state, rule = harmonise_status(self.status_key, ctx, self.status_map)
            identifiers: dict[str, Any] = {"issuer_registry_id": row.id}
            if r.get("docket"):
                identifiers["docket"] = r["docket"]
            if len(row.states) > 1:
                identifiers["states"] = row.states
            recs.append(
                {
                    "source_record_id": r["key"],
                    "source_url": r.get("url") or raw.url,
                    "kind": row.kind,
                    "issuer": row.issuer,
                    "title": r.get("title"),
                    "summary": None,
                    "jurisdiction": row.jurisdiction,
                    "technologies": technologies_str(list(r.get("technologies") or [])),
                    "capacity_sought_mw": r.get("capacity_sought_mw"),
                    "budget_amount": None,
                    "budget_currency": None,
                    "open_at": to_utc(r.get("open_at")),
                    "due_at": due,
                    "status": state,
                    "status_raw": r.get("status_raw"),
                    "status_rule": rule,
                    "identifiers": json.dumps(identifiers, sort_keys=True),
                }
            )
        df = pd.DataFrame(recs, columns=list(recs[0]) if recs else ["source_record_id"])
        return self.finalize(df, rows, raw)


def _older_than(stamp: Any, now: dt.datetime, age: dt.timedelta) -> bool:
    when = _parse_iso(stamp)
    return when is None or now - when >= age


def _parse_iso(stamp: Any) -> dt.datetime | None:
    if not stamp:
        return None
    try:
        when = dt.datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=dt.UTC)


def _left_out_counts(left_out: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for r in left_out:
        rule = str(r.get("classification_rule"))
        counts[rule] = counts.get(rule, 0) + 1
    return dict(sorted(counts.items()))


_CLASSES: dict[str, type[UtilityRfpConnector]] = {}


def connector_class_for(source_id: str) -> type[UtilityRfpConnector]:
    """The connector class for one issuer row, built once per process. Raises `RegistrationError`
    for an id the registry does not hold or does not clear (`IssuerRow.refusal`)."""
    cached = _CLASSES.get(source_id)
    if cached is not None:
        return cached
    row = registry_rows().get(source_id)
    if row is None:
        raise RegistrationError(f"{source_id} is not in data/vendored/rfp_issuers/issuers.yaml")
    reason = row.refusal()
    if reason is not None:
        raise RegistrationError(
            f"{source_id}: the issuer registry does not clear it for automated reading: {reason}"
        )
    mode: SnapshotMode = "incremental" if row.extractor == "news_release_index" else "full"
    cls = cast(
        "type[UtilityRfpConnector]",
        type(
            "UtilityRfp_" + re.sub(r"\W", "_", source_id.removeprefix("us.rfp.")),
            (UtilityRfpConnector,),
            {
                "__module__": __name__,
                "__doc__": f"{source_id}: {row.name} ({row.extractor}); see the module docstring.",
                "source_id": source_id,
                "issuer_row": row,
                "snapshot_mode": mode,
                "may_be_empty": row.extractor != "aep_energy_rfps",
                "issuer_org_type": row.issuer_type,
            },
        ),
    )
    _CLASSES[source_id] = cls
    return cls
