"""us.grants_gov.search2 — Grants.gov Search2 API (federal funding opportunities).

Fetch: POST `/v1/api/search2` with keyword "energy" and `oppStatuses = forecasted|posted`,
paged 100 at a time (151 hits on 2026-09-12); pages are bundled into one JSON document so
`parse` runs offline. The hit set is the full population of open/forecast notices matching the
keyword, so a notice that closes disappears and diff emits `removed` (snapshot_mode = full).
Parse: `data.oppHits[]`; `errorcode != 0` is a ParseError.
source_record_id: the Grants.gov opportunity `id` (`number` = DE-FOA-… goes to identifiers).
Reuse: US federal work, public domain; the API's terms of service ask every service that uses it
to show "This product uses the Grants.gov API but is not endorsed or certified by the U.S.
Department of Health and Human Services." (`data/sources.yaml` `attribution`). Kind: `foa`.
Token (2026-10-10, review docs/51 §2.7 item 6): every response page carries a `token`, a signed
JWT (HS256) issued fresh per request. It is bearer-shaped, so it is not stored (`redact`), and
because it differed on every request no two fetches of the same hits ever matched: every hourly
run ended `ok` and queued a load. `canonical_content` compares the pages without it, which also
covers snapshots stored before the redaction.

Funding figures (lane rfp-value, 2026-10-22). The search hit carries no amount: 0 of the 136
Grants.gov notices in the 2026-10-20 store had one. The public `fetchOpportunity` endpoint (POST
`{"opportunityId": <id>}`, no authentication) returns the notice's synopsis or forecast with
`estimatedFunding` (estimated total programme funding), `awardCeiling`, `awardFloor` and
`numberOfAwards` (expected number of awards), as numeric strings, `"0"` or absent when not stated.
`fetch` reads it for the notices the search returned, that is the notices this source puts in the
store, and for no others. A detail read earlier is reused while its last read or confirmation is
younger than `DETAIL_MAX_AGE` and the hit's status and dates are unchanged, so a steady hourly run
asks for none; at most `DETAIL_CAP` are read per run, new notices first, and the rest wait for the
next run. Requests share the host's 1 rps bucket with the search (`data/sources.yaml`
`rate_limit_rps`; the terms publish no number and reserve the right to limit). A failed or refused
detail, or an unreadable previous snapshot, keeps what there is and never fails the run; the
counts are in the snapshot's `meta`.

The detail reads are bounded so they can never cost the search its run (review of lane rfp-value,
2026-10-22: with `fetchOpportunity` answering 503 for all 136 notices, each read went through the
session's 4 attempts and backoff, 544 POSTs and about 1,593 s, or about 12,784 s for a 429 with
`Retry-After: 30`, past the scheduler's 600 s fetch timeout, so the search snapshot was never
stored, and the run kept asking after a 429). Each read is one attempt (`DETAIL_ATTEMPTS`) with a
`DETAIL_TIMEOUT_S` timeout; the first transport failure, 429, 5xx, challenge or other non-200
answer stops the reads for the run (`stats["stopped"]`), as do `DETAIL_MAX_ERROR_BODIES` error
bodies in a row; and no read starts later than `DETAIL_BUDGET_S` after the run's fetch began. What
was not read keeps its previous reading and waits for the next run, so an outage costs one request
an hour. A notice whose own read failed (an error body, a refusal, a transport failure) is not
asked again for `DETAIL_RETRY_AFTER`: the time of the failure goes in the snapshot's
`meta["details"]["failed_at"]` (`{time: [ids]}`, read back like `checked` below), so an id the
endpoint cannot answer costs one request a day rather than one every hourly run (review of lane
rfp-value, 2026-10-22). Only the figures, their dates and the ids are kept (`detail_subset`): the
response's contact name, email and phone (`agencyContact*`, a programme officer) are never stored.
The bundle's `details` map is keyed by opportunity id; `parse` attaches each to its hit as `detail`,
so the stored `raw` carries the figures and the time they were read.

A re-read that finds the same figures is a confirmation, not a new reading (review of lane
rfp-value, 2026-10-22). The detail stays as it was, `retrieved_at` included, and only its `for_hit`
follows the hit; the time of the confirmation goes in the snapshot's `meta["details"]["checked"]`
(`{time: [ids]}`), which the next run reads from the previous run record. So the bundle, and
therefore the record's served `identifiers.value`, changes only when a figure does: before this, the
weekly re-read of 105 notices with unchanged figures stored a new snapshot, loaded it, and moved
`last_changed` on all 105 (sort=-last_changed, the RSS feed, `updated_since` syncs). Now such a run
is `unchanged`: no snapshot, no load.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import html
import json
import pathlib
import time
from collections.abc import Iterator
from typing import Any, ClassVar

import pandas as pd

from pipeline.connectors.base import Connector as BaseConnector
from pipeline.connectors.base import ConnectorError, Kind, ParseError, RawSnapshot
from pipeline.connectors.canonical import harmonise_status
from pipeline.connectors.http import HttpBlocked, HttpFailed
from pipeline.connectors.objectstore import StoreError
from pipeline.connectors.opportunity import (
    StatedFigure,
    StatedValue,
    classify_technologies,
    deadline_passed,
    parse_amount,
    stated_value_of,
    technologies_str,
    to_utc,
)

API_URL = "https://api.grants.gov/v1/api/search2"
DETAIL_API_URL = "https://api.grants.gov/v1/api/fetchOpportunity"
DETAIL_URL = "https://www.grants.gov/search-results-detail/{id}"
PAGE = 100
MAX_PAGES = 20
#: Per-response page keys that are not data: the request-scoped JWT (module docstring).
VOLATILE_PAGE_KEYS = frozenset({"token"})
#: A stored detail younger than this is reused while its hit is unchanged (module docstring).
DETAIL_MAX_AGE = dt.timedelta(days=7)
#: The most detail requests one run makes (~2.5 minutes at 1 rps).
DETAIL_CAP = 150
#: Wall-clock budget, from the start of the run's fetch: no detail read starts after it. The
#: scheduler kills a fetch job at 600 s (`infra/scheduler/app.py`), and the search pages read
#: before the details must still be stored.
DETAIL_BUDGET_S = 300.0
#: One detail read's timeout, and its attempts: a failure is not retried within the run (it stops
#: the reads: module docstring), so the session's backoff and `Retry-After` waits never apply here.
DETAIL_TIMEOUT_S = 20
DETAIL_ATTEMPTS = 1
#: Error bodies (HTTP 200 with `errorcode != 0`, or no data) in a row that stop the reads.
DETAIL_MAX_ERROR_BODIES = 10
#: How long a notice whose detail read failed waits before it is asked again (module docstring).
DETAIL_RETRY_AFTER = dt.timedelta(days=1)
#: Hit fields whose change means the notice was revised, so its detail is read again.
DETAIL_HIT_KEYS = ("oppStatus", "openDate", "closeDate")
#: The only detail fields stored. Everything else, the programme officer's contact fields
#: included, is dropped before the bundle exists.
DETAIL_TOP_KEYS = ("id", "revision", "opportunityNumber", "docType")
DETAIL_BLOCK_KEYS = (
    "version",
    "estimatedFunding",
    "awardCeiling",
    "awardFloor",
    "numberOfAwards",
    "postingDate",
    "lastUpdatedDate",
    "fiscalYear",
)
DETAIL_BLOCKS = ("synopsis", "forecast")


def _without_tokens(content: bytes) -> tuple[dict[str, Any], bool] | None:
    """The bundle with every page's `token` removed, and whether any page had one; None when the
    bytes are not a bundle this connector wrote (a parse error then reports them as they are)."""
    try:
        doc = json.loads(content)
        pages = doc["pages"]
        had = any(VOLATILE_PAGE_KEYS & set(page) for page in pages)
        doc["pages"] = [{k: v for k, v in page.items() if k not in VOLATILE_PAGE_KEYS} for page in pages]
    except (ValueError, KeyError, TypeError, AttributeError):
        return None
    return doc, had


def detail_subset(response: Any, retrieved_at: str, hit: dict[str, Any]) -> dict[str, Any] | None:
    """What is kept of one `fetchOpportunity` response: the ids, and from its synopsis and/or
    forecast block the funding figures and dates (`DETAIL_BLOCK_KEYS`), stamped with when it was
    read and the hit fields it was read for. None for an error body or a response without data."""
    if not isinstance(response, dict) or response.get("errorcode") not in (0, "0"):
        return None
    data = response.get("data")
    if not isinstance(data, dict):
        return None
    out: dict[str, Any] = {k: data[k] for k in DETAIL_TOP_KEYS if data.get(k) not in (None, "")}
    for name in DETAIL_BLOCKS:
        block = data.get(name)
        if isinstance(block, dict):
            out[name] = {k: block[k] for k in DETAIL_BLOCK_KEYS if block.get(k) not in (None, "")}
    out["retrieved_at"] = retrieved_at
    out["for_hit"] = {k: hit.get(k) for k in DETAIL_HIT_KEYS}
    return out


def _detail_block(detail: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    """The block that describes the notice now: the one its `docType` names, else the synopsis
    (a posted notice), else the forecast."""
    doc_type = str(detail.get("docType") or "")
    for name in (doc_type, *DETAIL_BLOCKS):
        block = detail.get(name)
        if name in DETAIL_BLOCKS and isinstance(block, dict):
            return name, block
    return None


def stated_value(row: dict[str, Any]) -> StatedValue | None:
    """The notice's funding as its Grants.gov detail states it (lane rfp-value): estimated total
    programme funding leads, else the award ceiling (also when the total is a placeholder, as
    363912's 2,147,483,647: `stated_value_of`); the award floor is listed beside either but never
    leads, because it is the least one award can be. Grants.gov states these in US dollars (its
    notice pages print them with `$`), and `numberOfAwards` becomes `expected_awards`."""
    detail = row.get("detail")
    if not isinstance(detail, dict):
        return None
    found = _detail_block(detail)
    if found is None:
        return None
    name, block = found
    funding = parse_amount(block.get("estimatedFunding"))
    ceiling = parse_amount(block.get("awardCeiling"))
    floor = parse_amount(block.get("awardFloor"))
    figures: list[StatedFigure] = []
    if funding is not None:
        figures.append(StatedFigure("total_programme_funding", funding, "USD", f"{name}.estimatedFunding"))
    if ceiling is not None:
        figures.append(StatedFigure("award_ceiling", ceiling, "USD", f"{name}.awardCeiling"))
    if figures and floor is not None:
        figures.append(StatedFigure("award_floor", floor, "USD", f"{name}.awardFloor", may_lead=False))
    awards = parse_amount(block.get("numberOfAwards"))
    retrieved = detail.get("retrieved_at")
    return stated_value_of(
        figures,
        expected_awards=int(awards) if awards is not None and awards == int(awards) else None,
        retrieved_at=str(retrieved) if retrieved else None,
        endpoint=DETAIL_API_URL,
    )


def _parse_instant(value: Any) -> dt.datetime | None:
    try:
        t = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo is not None else t.replace(tzinfo=dt.UTC)


def detail_is_fresh(
    detail: dict[str, Any], hit: dict[str, Any], now: dt.datetime, checked_at: str | None = None
) -> bool:
    """A stored detail is reused when it was read for the same hit status and dates, and its last
    read (`retrieved_at`) or confirmation (`checked_at`, module docstring) is younger than
    `DETAIL_MAX_AGE`."""
    if detail.get("for_hit") != {k: hit.get(k) for k in DETAIL_HIT_KEYS}:
        return False
    times = [t for t in (_parse_instant(detail.get("retrieved_at")), _parse_instant(checked_at)) if t]
    return bool(times) and dt.timedelta(0) <= now - max(times) < DETAIL_MAX_AGE


#: Keys of a stored detail that say when and for which hit it was read, not what it states.
DETAIL_STAMP_KEYS = frozenset({"retrieved_at", "for_hit"})


def same_figures(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """Whether two readings of a detail state the same thing (ids, revision, figures, dates)."""
    return {k: v for k, v in a.items() if k not in DETAIL_STAMP_KEYS} == {
        k: v for k, v in b.items() if k not in DETAIL_STAMP_KEYS
    }


def _stamp(at: dt.datetime) -> str:
    return at.astimezone(dt.UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _monotonic() -> float:
    """The detail reads' wall clock (`DETAIL_BUDGET_S`); a function so tests can move it."""
    return time.monotonic()


@contextlib.contextmanager
def _attempts(http: Any, attempts: int) -> Iterator[None]:
    """`http` making at most `attempts` attempts per request inside the block (a `PoliteSession`'s
    `max_attempts`; a session without one is left as it is), restored after."""
    before = getattr(http, "max_attempts", None)
    if isinstance(before, int):
        http.max_attempts = attempts
    try:
        yield
    finally:
        if isinstance(before, int):
            http.max_attempts = before


class Connector(BaseConnector):
    source_id: ClassVar[str] = "us.grants_gov.search2"
    kind: ClassVar[Kind] = "opportunity"
    ext: ClassVar[str] = "json"
    honour_robots: ClassVar[bool] = False  # JSON API (its robots.txt answers 403)
    status_key: ClassVar[str] = "grants_gov"
    status_map_path: ClassVar[pathlib.Path | None] = pathlib.Path(__file__).with_name("status_map.yaml")
    key_source_columns: ClassVar[tuple[str, ...]] = (
        "id",
        "number",
        "title",
        "agency",
        "oppStatus",
        "closeDate",
    )
    keyword: ClassVar[str] = "energy"
    statuses: ClassVar[str] = "forecasted|posted"
    removal_meaning = "closed"  # only open/forecast notices are fetched (docstring); docs/51 §2.7

    def fetch(self) -> RawSnapshot:
        t0 = _monotonic()
        pages: list[dict[str, Any]] = []
        start = 0
        while len(pages) < MAX_PAGES:
            body = {
                "keyword": self.keyword,
                "oppStatuses": self.statuses,
                "rows": PAGE,
                "startRecordNum": start,
            }
            r = self.http.post(API_URL, json=body, timeout=90)
            if r.status_code != 200:
                raise ConnectorError(f"POST {API_URL} -> HTTP {r.status_code}")
            page = r.json()
            pages.append(page)
            data = page.get("data") or {}
            hits = data.get("oppHits") or []
            start += len(hits)
            if not hits or start >= int(data.get("hitCount", 0)):
                break
        details, detail_stats = self.fetch_details(_hits(pages), started=t0)
        payload = json.dumps(
            {
                "request": {"keyword": self.keyword, "oppStatuses": self.statuses},
                "pages": pages,
                "details": details,
            },
            ensure_ascii=False,
        ).encode("utf-8")
        return RawSnapshot(
            content=payload,
            content_type="application/json",
            url=API_URL,
            retrieved_at=self.now(),  # the run's clock (utcnow unless the runner fixes it)
            http_status=200,
            ext="json",
            elapsed_s=round(_monotonic() - t0, 2),
            requests_made=len(pages) + detail_stats["requested"],
            meta={
                "hit_count": (pages[0].get("data") or {}).get("hitCount"),
                "pages": len(pages),
                "details": detail_stats,
            },
        )

    def _previous_details(self) -> dict[str, dict[str, Any]]:
        """The `details` map of the stored snapshot this run is compared against, or {} when there
        is none or it cannot be read (the details are then read afresh, as on a first run)."""
        if self.previous is None:
            return {}
        try:
            doc = json.loads(self.previous.content())
        except (ConnectorError, StoreError, ValueError, OSError):
            return {}
        details = doc.get("details") if isinstance(doc, dict) else None
        return (
            {str(k): v for k, v in details.items() if isinstance(v, dict)}
            if isinstance(details, dict)
            else {}
        )

    def _previous_times(self, key: str) -> dict[str, str]:
        """A `{time: [ids]}` map of the previous run record's `meta["details"]` (`checked`: when
        each detail was last confirmed; `failed_at`: when its read last failed; module docstring),
        by opportunity id."""
        meta = self.previous.meta if self.previous is not None else {}
        details = meta.get("details") if isinstance(meta, dict) else None
        times = details.get(key) if isinstance(details, dict) else None
        out: dict[str, str] = {}
        if isinstance(times, dict):
            for stamp, ids in times.items():
                for hid in ids if isinstance(ids, list) else []:
                    out[str(hid)] = str(stamp)
        return out

    def _previous_checks(self) -> dict[str, str]:
        return self._previous_times("checked")

    def fetch_details(
        self, hits: list[dict[str, Any]], started: float | None = None
    ) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
        """One detail per hit (module docstring "Funding figures"): reused, read, confirmed (read
        again with the same figures: the earlier reading is kept), or kept from the previous run
        when a read fails, the reads stopped, the cap is reached or the notice's last read failed
        less than `DETAIL_RETRY_AFTER` ago. `started` is when the run's fetch began on the
        `_monotonic` clock (default: now), from which `DETAIL_BUDGET_S` counts. Returns the map and
        its counts, whose `checked` and `failed_at` carry the confirmation and failure times
        forward and whose `stopped` says why the reads stopped early, if they did."""
        previous = self._previous_details()
        checks = self._previous_checks()
        failures = self._previous_times("failed_at")
        now = self.now()
        # One time per run for the confirmations, so the meta groups them (`{time: [ids]}`).
        run_stamp = _stamp(now)
        details: dict[str, dict[str, Any]] = {}
        checked: dict[str, str] = {}
        failed_at: dict[str, str] = {}
        stats: dict[str, Any] = {
            "reused": 0,
            "fetched": 0,
            "unchanged": 0,
            "failed": 0,
            "deferred": 0,
            "waiting": 0,
            "requested": 0,
        }

        def keep(hid: str, prior: dict[str, Any] | None) -> None:
            if prior is not None:
                details[hid] = prior
                if hid in checks:
                    checked[hid] = checks[hid]

        to_read: list[dict[str, Any]] = []
        for hit in hits:
            hid = str(hit.get("id"))
            prior = previous.get(hid)
            failed = _parse_instant(failures[hid]) if hid in failures else None
            if prior is not None and detail_is_fresh(prior, hit, now, checks.get(hid)):
                keep(hid, prior)
                stats["reused"] += 1
            elif failed is not None and dt.timedelta(0) <= now - failed < DETAIL_RETRY_AFTER:
                # Its last read failed less than `DETAIL_RETRY_AFTER` ago: not asked again yet.
                keep(hid, prior)
                failed_at[hid] = failures[hid]
                stats["waiting"] += 1
            else:
                to_read.append(hit)
        # New notices before stale ones: a notice with no figures at all gains the most.
        to_read.sort(key=lambda h: str(h.get("id")) in previous)
        blocked = False
        stopped: str | None = None
        error_bodies = 0
        started = _monotonic() if started is None else started
        with _attempts(self.http, DETAIL_ATTEMPTS):
            for hit in to_read:
                hid = str(hit.get("id"))
                prior = previous.get(hid)
                if stopped is None and _monotonic() - started >= DETAIL_BUDGET_S:
                    stopped = f"time budget of {DETAIL_BUDGET_S:.0f} s spent"
                if stopped is not None or stats["requested"] >= DETAIL_CAP or not hid.isdigit():
                    stats["deferred"] += 1
                    keep(hid, prior)
                    continue
                stats["requested"] += 1
                subset: dict[str, Any] | None = None
                stamp = _stamp(self.now())
                try:
                    r = self.http.post(
                        DETAIL_API_URL, json={"opportunityId": int(hid)}, timeout=DETAIL_TIMEOUT_S
                    )
                    if r.status_code == 200:
                        subset = detail_subset(r.json(), stamp, hit)
                        error_bodies = 0 if subset is not None else error_bodies + 1
                    else:
                        stopped = f"HTTP {r.status_code}"
                except HttpBlocked as exc:
                    blocked, stopped = True, f"HttpBlocked: {exc}"[:200]
                except HttpFailed as exc:
                    stopped = f"HttpFailed: {exc}"[:200]
                except ValueError:
                    error_bodies += 1
                if stopped is None and error_bodies >= DETAIL_MAX_ERROR_BODIES:
                    stopped = f"{error_bodies} error bodies in a row"
                if subset is None:
                    stats["failed"] += 1
                    failed_at[hid] = run_stamp
                    keep(hid, prior)
                    continue
                stats["fetched"] += 1
                if prior is not None and same_figures(subset, prior):
                    # A confirmation: the reading stands, so nothing the record serves moves.
                    details[hid] = {**prior, "for_hit": subset["for_hit"]}
                    checked[hid] = run_stamp
                    stats["unchanged"] += 1
                else:
                    details[hid] = subset
        stats["blocked"] = blocked
        stats["stopped"] = stopped
        stats["checked"] = _by_time(checked)
        stats["failed_at"] = _by_time(failed_at)
        ordered = {str(h.get("id")): details[str(h.get("id"))] for h in hits if str(h.get("id")) in details}
        return ordered, stats

    def redact(self, content: bytes) -> bytes:
        stripped = _without_tokens(content)
        if stripped is None or not stripped[1]:
            return content
        return json.dumps(stripped[0], ensure_ascii=False).encode("utf-8")

    def canonical_content(self, content: bytes) -> bytes:
        stripped = _without_tokens(content)
        if stripped is None:
            return content
        return json.dumps(stripped[0], ensure_ascii=False, sort_keys=True).encode("utf-8")

    def parse(self, raw: RawSnapshot) -> list[dict[str, Any]]:
        doc = json.loads(raw.content)
        details = doc.get("details") if isinstance(doc.get("details"), dict) else {}
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for page in doc.get("pages", []):
            if page.get("errorcode") not in (0, "0", None):
                raise ParseError(f"Grants.gov error {page.get('errorcode')}: {page.get('msg')}")
            for hit in (page.get("data") or {}).get("oppHits") or []:
                if str(hit.get("id")) in seen:
                    continue
                seen.add(str(hit.get("id")))
                row = dict(hit)
                detail = details.get(str(hit.get("id")))
                if isinstance(detail, dict):
                    row["detail"] = detail
                rows.append(row)
        return rows

    def normalize(self, rows: list[dict[str, Any]], raw: RawSnapshot) -> pd.DataFrame:
        now = raw.retrieved_at
        recs: list[dict[str, Any]] = []
        for r in rows:
            due = to_utc(r.get("closeDate"), "%m/%d/%Y")
            ctx = {"status_raw": r.get("oppStatus"), "deadline_passed": deadline_passed(due, now)}
            state, rule = harmonise_status(self.status_key, ctx, self.status_map)
            title = html.unescape(str(r.get("title") or "")).strip()
            value = stated_value(r)
            identifiers: dict[str, Any] = {
                "grants_gov_number": r.get("number"),
                "grants_gov_id": r.get("id"),
                "agency_code": r.get("agencyCode"),
                "aln": r.get("cfdaList") or [],
            }
            if value is not None:
                identifiers["value"] = value.identifiers()
            recs.append(
                {
                    "source_record_id": str(r.get("id")),
                    "source_url": DETAIL_URL.format(id=r.get("id")),
                    "kind": "foa",
                    "issuer": (r.get("agency") or r.get("agencyCode") or "").strip() or None,
                    "title": title or None,
                    "summary": None,
                    "jurisdiction": "US",
                    "technologies": technologies_str(classify_technologies(title)),
                    "capacity_sought_mw": None,
                    "budget_amount": value.amount if value else None,
                    "budget_currency": value.currency if value else None,
                    "open_at": to_utc(r.get("openDate"), "%m/%d/%Y"),
                    "due_at": due,
                    "status": state,
                    "status_raw": r.get("oppStatus"),
                    "status_rule": rule,
                    "identifiers": json.dumps(identifiers),
                }
            )
        df = pd.DataFrame(recs, columns=list(recs[0]) if recs else ["source_record_id"])
        return self.finalize(df, rows, raw)


def _by_time(times: dict[str, str]) -> dict[str, list[int | str]]:
    """`{id: time}` as the snapshot meta keeps it: `{time: [ids]}`, times and ids in order."""
    out: dict[str, list[int | str]] = {}
    for hid in sorted(times, key=lambda i: (len(i), i)):
        out.setdefault(times[hid], []).append(int(hid) if hid.isdigit() else hid)
    return dict(sorted(out.items()))


def _hits(pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every hit once, in page order."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for page in pages:
        for hit in (page.get("data") or {}).get("oppHits") or []:
            hid = str(hit.get("id"))
            if hid not in seen:
                seen.add(hid)
                out.append(hit)
    return out
