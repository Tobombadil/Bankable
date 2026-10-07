"""eu.ted.api — TED (Tenders Electronic Daily) Search API v3, energy CPV notices.

Fetch: POST `/v3/notices/search` with expert query
`(classification-cpv=09* OR 31* OR 45231* OR 71314*) AND publication-date>=<window start>`, 100
notices per page, bundled into one JSON document. The run is `snapshot_mode = incremental`: rows
are upserted onto the previous snapshot and never "removed". The window starts at the day before
the last promoted run (`Connector.fetch_window`, 2026-10-07): a run after an outage catches up
(audit 2026-09-30 F3 measured 2,786 notices a rolling 3-day window would never have fetched), and
the first run, with nothing to anchor on, looks back 3 days. A catch-up may page up to ten times
the normal cap; a fetch that still stops at the cap is marked `truncated` and held.
`links` is not requested (it is ~7 KB per notice); the canonical URL is derived.
TED publishes on business days, so the row-count gate compares like-for-like weekdays by
`open_at` (the publication date; `pipeline/connectors/dq.py`, audit F6).
Parse: `notices[]` as returned (multilingual title dict, list-valued fields).
source_record_id: `publication-number` (e.g. `626048-2026`).
Personal data: no contact fields are requested (docs/13 §5.4 rule 1); buyer name is an organisation.
Reuse: notices freely reusable (Decision 2011/833/EU); credit the source, no TED logo.
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib
import time
from typing import Any, ClassVar

import pandas as pd

from pipeline.connectors.base import Connector as BaseConnector
from pipeline.connectors.base import ConnectorError, Kind, ParseError, RawSnapshot, SnapshotMode
from pipeline.connectors.canonical import harmonise_status
from pipeline.connectors.opportunity import (
    classify_technologies,
    deadline_passed,
    iso2,
    known_budget,
    technologies_str,
    to_utc,
)

API_URL = "https://api.ted.europa.eu/v3/notices/search"
NOTICE_URL = "https://ted.europa.eu/en/notice/-/detail/{pubnum}"
FIELDS = [
    "publication-number",
    "notice-title",
    "buyer-name",
    "buyer-country",
    "publication-date",
    "deadline-receipt-tender-date-lot",
    "notice-type",
    "contract-nature",
    "classification-cpv",
    "place-of-performance",
    "form-type",
    "estimated-value-glo",
    "estimated-value-cur-glo",
    "procedure-type",
    "total-value",
    "total-value-cur",
]
# energy, electrical equipment, power-plant construction, energy consultancy
CPV_PREFIXES = ("09", "31", "45231", "71314")
CPV_QUERY = "(" + " OR ".join(f"classification-cpv={p}*" for p in CPV_PREFIXES) + ")"
PAGE = 100
MAX_PAGES = 30
WINDOW_DAYS = 3
KIND_BY_PREFIX = {
    "pin": "procurement_notice",
    "cn": "tender",
    "can": "tender",
    "veat": "tender",
    "qu": "tender",
    "subco": "tender",
    "brin": "procurement_notice",
}
CPV_TECH = {
    "093": "solar_pv",
    "09330000": "solar_pv",
    "09331000": "solar_pv",
    "09332000": "solar_pv",
    "45251100": "wind",
    "45251120": "hydro",
    "45251143": "nuclear",
    "45231400": "transmission",
    "45231220": "gas",
    "45231221": "gas",
    "45231223": "gas",
    "45232220": "transmission",
    "31121": "gas",
    "31122": "gas",
    "31210000": "transmission",
    "31170000": "transmission",
    "31200000": "transmission",
    "45251111": "nuclear",
    "09111": "coal",
    "09120000": "gas",
    "09123000": "gas",
    "45231210": "gas",
    "71314": "efficiency",
}


def first_text(v: Any) -> str | None:
    """Multilingual dict -> English if present else the first value; lists -> first element."""
    if isinstance(v, dict):
        if "eng" in v:
            v = v["eng"]
        elif v:
            v = next(iter(v.values()))
        else:
            return None
    if isinstance(v, list):
        v = v[0] if v else None
    return None if v is None else str(v).strip()


def cpv_technologies(codes: list[str]) -> list[str]:
    out: list[str] = []
    for c in codes:
        for prefix, tok in CPV_TECH.items():
            if c.startswith(prefix) and tok not in out:
                out.append(tok)
    return out


class Connector(BaseConnector):
    source_id: ClassVar[str] = "eu.ted.api"
    kind: ClassVar[Kind] = "opportunity"
    ext: ClassVar[str] = "json"
    honour_robots: ClassVar[bool] = False
    snapshot_mode: ClassVar[SnapshotMode] = "incremental"
    window_days: ClassVar[int] = WINDOW_DAYS
    window_date_column: ClassVar[str | None] = "open_at"
    status_key: ClassVar[str] = "ted"
    status_map_path: ClassVar[pathlib.Path | None] = pathlib.Path(__file__).with_name("status_map.yaml")
    key_source_columns: ClassVar[tuple[str, ...]] = (
        "publication-number",
        "notice-title",
        "notice-type",
        "buyer-name",
        "publication-date",
    )

    def query(self, since: dt.datetime) -> str:
        """The expert query for notices published on or after `since`'s UTC date."""
        return f"{CPV_QUERY} AND publication-date>={since.astimezone(dt.UTC).strftime('%Y%m%d')}"

    def fetch(self) -> RawSnapshot:
        t0 = time.monotonic()
        now = self.now()
        window = self.fetch_window(now)
        query = self.query(window.start)
        cap = self.page_cap(MAX_PAGES, window)
        pages: list[dict[str, Any]] = []
        seen = 0
        truncated = False
        for page_no in range(1, cap + 1):
            body = {"query": query, "fields": FIELDS, "limit": PAGE, "page": page_no, "scope": "ALL"}
            r = self.http.post(API_URL, json=body, timeout=120)
            if r.status_code != 200:
                raise ConnectorError(f"POST {API_URL} -> HTTP {r.status_code}: {r.text[:200]}")
            page = r.json()
            pages.append(page)
            seen += len(page.get("notices") or [])
            if not page.get("notices") or seen >= int(page.get("totalNoticeCount", 0)):
                break
        else:
            truncated = seen < int(pages[-1].get("totalNoticeCount", 0)) if pages else False
        payload = json.dumps(
            {"request": {"query": query, "fields": FIELDS}, "pages": pages}, ensure_ascii=False
        ).encode("utf-8")
        return RawSnapshot(
            content=payload,
            content_type="application/json",
            url=API_URL,
            retrieved_at=now,
            http_status=200,
            ext="json",
            elapsed_s=round(time.monotonic() - t0, 2),
            requests_made=len(pages),
            meta={
                "query": query,
                "total": pages[0].get("totalNoticeCount"),
                "pages": len(pages),
                "page_cap": cap,
                "truncated": truncated,
                **window.meta(),
            },
        )

    def parse(self, raw: RawSnapshot) -> list[dict[str, Any]]:
        doc = json.loads(raw.content)
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for page in doc.get("pages", []):
            if "notices" not in page:
                raise ParseError(f"TED page without notices: {str(page)[:200]}")
            for n in page["notices"]:
                pn = str(n.get("publication-number") or "")
                if not pn or pn in seen:
                    continue
                seen.add(pn)
                rows.append({k: v for k, v in n.items() if k != "links"})
        return rows

    def normalize(self, rows: list[dict[str, Any]], raw: RawSnapshot) -> pd.DataFrame:
        now = raw.retrieved_at
        recs: list[dict[str, Any]] = []
        for r in rows:
            ntype = str(r.get("notice-type") or "")
            deadlines = [to_utc(str(d)[:10]) for d in (r.get("deadline-receipt-tender-date-lot") or [])]
            due = min((d for d in deadlines if d is not None), default=None)
            ctx = {
                "status_raw": ntype,
                "form_type": r.get("form-type"),
                "deadline_passed": deadline_passed(due, now),
            }
            state, rule = harmonise_status(self.status_key, ctx, self.status_map)
            title = first_text(r.get("notice-title"))
            cpv = [str(c) for c in (r.get("classification-cpv") or [])]
            techs = classify_technologies(title)
            for t in cpv_technologies(cpv):
                if t not in techs:
                    techs.append(t)
            amount = r.get("estimated-value-glo") or r.get("total-value")
            currency = first_text(r.get("estimated-value-cur-glo") or r.get("total-value-cur"))
            recs.append(
                {
                    "source_record_id": str(r.get("publication-number")),
                    "source_url": NOTICE_URL.format(pubnum=r.get("publication-number")),
                    "kind": KIND_BY_PREFIX.get(ntype.split("-")[0], "tender"),
                    "issuer": first_text(r.get("buyer-name")),
                    "title": title,
                    "summary": None,
                    "jurisdiction": iso2(first_text(r.get("buyer-country"))) or "EU",
                    "technologies": technologies_str(techs),
                    "capacity_sought_mw": None,
                    "budget_amount": known_budget(amount) if isinstance(amount, int | float) else None,
                    "budget_currency": currency,
                    "open_at": to_utc(str(r.get("publication-date") or "")[:10]),
                    "due_at": due,
                    "status": state,
                    "status_raw": ntype,
                    "status_rule": rule,
                    "identifiers": json.dumps(
                        {
                            "ted_notice_id": r.get("publication-number"),
                            "cpv": sorted(set(cpv)),
                            "procedure_type": r.get("procedure-type"),
                            "contract_nature": sorted(set(r.get("contract-nature") or [])),
                        }
                    ),
                }
            )
        df = pd.DataFrame(recs, columns=list(recs[0]) if recs else ["source_record_id"])
        return self.finalize(df, rows, raw)
