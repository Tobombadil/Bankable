"""us.grants_gov.search2 — Grants.gov Search2 API (federal funding opportunities).

Fetch: POST `/v1/api/search2` with keyword "energy" and `oppStatuses = forecasted|posted`,
paged 100 at a time (151 hits on 2026-09-12); pages are bundled into one JSON document so
`parse` runs offline. The hit set is the full population of open/forecast notices matching the
keyword, so a notice that closes disappears and diff emits `removed` (snapshot_mode = full).
Parse: `data.oppHits[]`; `errorcode != 0` is a ParseError.
source_record_id: the Grants.gov opportunity `id` (`number` = DE-FOA-… goes to identifiers).
Reuse: US federal work, public domain. Opportunity kind: `foa`.
"""

from __future__ import annotations

import datetime as dt
import html
import json
import pathlib
import time
from typing import Any, ClassVar

import pandas as pd

from pipeline.connectors.base import Connector as BaseConnector
from pipeline.connectors.base import ConnectorError, Kind, ParseError, RawSnapshot
from pipeline.connectors.canonical import harmonise_status
from pipeline.connectors.opportunity import classify_technologies, deadline_passed, technologies_str, to_utc

API_URL = "https://api.grants.gov/v1/api/search2"
DETAIL_URL = "https://www.grants.gov/search-results-detail/{id}"
PAGE = 100
MAX_PAGES = 20


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

    def fetch(self) -> RawSnapshot:
        t0 = time.monotonic()
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
        payload = json.dumps(
            {"request": {"keyword": self.keyword, "oppStatuses": self.statuses}, "pages": pages},
            ensure_ascii=False,
        ).encode("utf-8")
        return RawSnapshot(
            content=payload,
            content_type="application/json",
            url=API_URL,
            retrieved_at=dt.datetime.now(dt.UTC),
            http_status=200,
            ext="json",
            elapsed_s=round(time.monotonic() - t0, 2),
            requests_made=len(pages),
            meta={"hit_count": (pages[0].get("data") or {}).get("hitCount"), "pages": len(pages)},
        )

    def parse(self, raw: RawSnapshot) -> list[dict[str, Any]]:
        doc = json.loads(raw.content)
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for page in doc.get("pages", []):
            if page.get("errorcode") not in (0, "0", None):
                raise ParseError(f"Grants.gov error {page.get('errorcode')}: {page.get('msg')}")
            for hit in (page.get("data") or {}).get("oppHits") or []:
                if str(hit.get("id")) in seen:
                    continue
                seen.add(str(hit.get("id")))
                rows.append(dict(hit))
        return rows

    def normalize(self, rows: list[dict[str, Any]], raw: RawSnapshot) -> pd.DataFrame:
        now = raw.retrieved_at
        recs: list[dict[str, Any]] = []
        for r in rows:
            due = to_utc(r.get("closeDate"), "%m/%d/%Y")
            ctx = {"status_raw": r.get("oppStatus"), "deadline_passed": deadline_passed(due, now)}
            state, rule = harmonise_status(self.status_key, ctx, self.status_map)
            title = html.unescape(str(r.get("title") or "")).strip()
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
                    "budget_amount": None,
                    "budget_currency": None,
                    "open_at": to_utc(r.get("openDate"), "%m/%d/%Y"),
                    "due_at": due,
                    "status": state,
                    "status_raw": r.get("oppStatus"),
                    "status_rule": rule,
                    "identifiers": json.dumps(
                        {
                            "grants_gov_number": r.get("number"),
                            "grants_gov_id": r.get("id"),
                            "agency_code": r.get("agencyCode"),
                            "aln": r.get("cfdaList") or [],
                        }
                    ),
                }
            )
        df = pd.DataFrame(recs, columns=list(recs[0]) if recs else ["source_record_id"])
        return self.finalize(df, rows, raw)
