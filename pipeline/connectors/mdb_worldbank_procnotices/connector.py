"""mdb.worldbank.procnotices — World Bank Procurement Notices API, energy sectors.

Sector filter confirmed 2026-09-12: the notice document carries a nested `sector[]` with
`sector_code`/`sector_description`; the API filters on the dotted path
`sector.sector_code=<CODE>` with `^` as OR (`sector_exact=Energy`, `sector_code=`, comma and pipe
lists all return 0 or the unfiltered total). Energy codes used: LU solar, LH hydro, LW wind,
LB biomass, LI geothermal, LN non-renewable generation, LT transmission & distribution,
LZ other energy, LP public administration – energy, LE energy (33,950 notices on 2026-09-12).
Fetch: newest first (`srt=noticedate&order=desc`), 100 per page, until a page is older than the
window or the page cap is hit; `fl=` names the fields so contact_* personal data is never
requested (docs/13 §5.4). Rolling window -> `snapshot_mode = incremental`.
source_record_id: the notice `id` (`OP00468319`).
Reuse: CC BY 4.0 (World Bank Open Data), credit required.
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib
import time
from typing import Any, ClassVar

import pandas as pd

from pipeline.connectors.base import Connector as BaseConnector
from pipeline.connectors.base import ConnectorError, ParseError, RawSnapshot
from pipeline.connectors.opportunity import (
    classify_technologies,
    deadline_passed,
    iso2,
    technologies_str,
    to_utc,
)
from pipeline.normalize import harmonise_status

API_URL = "https://search.worldbank.org/api/v2/procnotices"
NOTICE_URL = "https://projects.worldbank.org/en/projects-operations/procurement-detail/{notice_id}"
SECTOR_CODES = ["LU", "LH", "LW", "LB", "LI", "LN", "LT", "LZ", "LP", "LE"]
SECTOR_TECH = {
    "LU": "solar_pv",
    "LH": "hydro",
    "LW": "wind",
    "LB": "biomass",
    "LI": "geothermal",
    "LT": "transmission",
    "LN": "thermal",
}
FIELDS = [
    "id",
    "notice_type",
    "noticedate",
    "notice_status",
    "submission_deadline_date",
    "submission_deadline_time",
    "project_ctry_code",
    "project_ctry_name",
    "project_id",
    "project_name",
    "regionname",
    "sector",
    "agency_name",
    "bid_reference_no",
    "bid_currency_code",
    "bid_estimate_amount",
    "bid_description",
    "procurement_group",
    "procurement_group_desc",
    "procurement_method_code",
    "procurement_method_name",
    "unspsc_classification",
    "submission_date",
    "noticetitle",
    "api_modified_date",
    "market_approach_name",
    "notice_lang_code",
    "notice_version_no",
    "contact_organization",
]
OPEN_TYPES = {
    "Invitation for Bids",
    "Request for Bids",
    "Request for Proposals",
    "Request for Expression of Interest",
    "Invitation for Prequalification",
    "Specific Procurement Notice",
}
KIND_BY_TYPE = {"General Procurement Notice": "procurement_notice", "Contract Award": "tender"}
PAGE = 100
MAX_PAGES = 10
WINDOW_DAYS = 14


class Connector(BaseConnector):
    source_id: ClassVar[str] = "mdb.worldbank.procnotices"
    kind: ClassVar[str] = "opportunity"
    ext: ClassVar[str] = "json"
    honour_robots: ClassVar[bool] = False
    snapshot_mode: ClassVar[str] = "incremental"
    status_key: ClassVar[str] = "worldbank"
    status_map_path: ClassVar[pathlib.Path | None] = pathlib.Path(__file__).with_name("status_map.yaml")
    key_source_columns: ClassVar[tuple[str, ...]] = (
        "id",
        "notice_type",
        "noticedate",
        "noticetitle",
        "project_ctry_code",
    )

    def params(self, offset: int) -> dict[str, Any]:
        return {
            "format": "json",
            "rows": PAGE,
            "os": offset,
            "sector.sector_code": "^".join(SECTOR_CODES),
            "srt": "noticedate",
            "order": "desc",
            "fl": ",".join(FIELDS),
        }

    def fetch(self) -> RawSnapshot:
        t0 = time.monotonic()
        now = dt.datetime.now(dt.UTC)
        cutoff = now - dt.timedelta(days=WINDOW_DAYS)
        pages: list[dict[str, Any]] = []
        for page_no in range(MAX_PAGES):
            r = self.http.get(API_URL, honour_robots=False, params=self.params(page_no * PAGE), timeout=120)
            if r.status_code != 200:
                raise ConnectorError(f"GET {API_URL} -> HTTP {r.status_code}")
            page = r.json()
            pages.append(page)
            notices = _notices(page)
            if not notices:
                break
            oldest = min((to_utc(n.get("noticedate"), "%d-%b-%Y") for n in notices), default=None)
            if oldest is not None and oldest < pd.Timestamp(cutoff):
                break
        url = r.url if pages else API_URL
        payload = json.dumps({"request": self.params(0), "pages": pages}, ensure_ascii=False).encode("utf-8")
        return RawSnapshot(
            content=payload,
            content_type="application/json",
            url=url.split("&os=")[0],
            retrieved_at=now,
            http_status=200,
            ext="json",
            elapsed_s=round(time.monotonic() - t0, 2),
            requests_made=len(pages),
            meta={
                "total": pages[0].get("total"),
                "pages": len(pages),
                "sector_filter": "sector.sector_code=" + "^".join(SECTOR_CODES),
            },
        )

    def redact(self, content: bytes) -> bytes:
        doc = json.loads(content)
        for page in doc.get("pages", []):
            for n in _notices(page):
                for k in [k for k in n if k.startswith("contact_") and k != "contact_organization"]:
                    n.pop(k, None)
        return json.dumps(doc, ensure_ascii=False).encode("utf-8")

    def parse(self, raw: RawSnapshot) -> list[dict[str, Any]]:
        doc = json.loads(raw.content)
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for page in doc.get("pages", []):
            if "procnotices" not in page:
                raise ParseError(f"World Bank page without procnotices: {str(page)[:200]}")
            for n in _notices(page):
                nid = str(n.get("id") or "")
                if not nid or nid in seen:
                    continue
                seen.add(nid)
                rows.append(
                    {
                        k: v
                        for k, v in n.items()
                        if not (k.startswith("contact_") and k != "contact_organization")
                    }
                )
        return rows

    def normalize(self, rows: list[dict[str, Any]], raw: RawSnapshot) -> pd.DataFrame:
        now = raw.retrieved_at
        recs: list[dict[str, Any]] = []
        for r in rows:
            ntype = str(r.get("notice_type") or "")
            due = to_utc(r.get("submission_deadline_date"))
            ctx = {
                "status_raw": ntype,
                "deadline_passed": deadline_passed(due, now),
                "is_open_type": "yes" if ntype in OPEN_TYPES else "no",
            }
            state, rule = harmonise_status(self.status_key, ctx, self.status_map)
            title = (r.get("noticetitle") or r.get("bid_description") or "").strip() or None
            sectors = [s.get("sector_code") for s in (r.get("sector") or []) if isinstance(s, dict)]
            techs = classify_technologies(title, r.get("project_name"))
            for code in sectors:
                tok = SECTOR_TECH.get(str(code))
                if tok and tok not in techs:
                    techs.append(tok)
            amount = r.get("bid_estimate_amount")
            try:
                budget = float(amount) if amount not in (None, "") else None
            except (TypeError, ValueError):
                budget = None
            recs.append(
                {
                    "source_record_id": str(r.get("id")),
                    "source_url": NOTICE_URL.format(notice_id=r.get("id")),
                    "kind": KIND_BY_TYPE.get(ntype, "tender"),
                    "issuer": r.get("agency_name") or r.get("contact_organization"),
                    "title": title,
                    "summary": None,
                    "jurisdiction": iso2(r.get("project_ctry_code")) or "global",
                    "technologies": technologies_str(techs),
                    "capacity_sought_mw": None,
                    "budget_amount": budget,
                    "budget_currency": r.get("bid_currency_code"),
                    "open_at": to_utc(r.get("noticedate"), "%d-%b-%Y"),
                    "due_at": due,
                    "status": state,
                    "status_raw": ntype,
                    "status_rule": rule,
                    "identifiers": json.dumps(
                        {
                            "wb_notice_id": r.get("id"),
                            "wb_project_id": r.get("project_id"),
                            "bid_reference_no": r.get("bid_reference_no"),
                            "sector_codes": sectors,
                        }
                    ),
                }
            )
        df = pd.DataFrame(recs, columns=list(recs[0]) if recs else ["source_record_id"])
        return self.finalize(df, rows, raw)


def _notices(page: dict[str, Any]) -> list[dict[str, Any]]:
    pn = page.get("procnotices")
    if isinstance(pn, dict):
        return [v for v in pn.values() if isinstance(v, dict)]
    return list(pn or [])
