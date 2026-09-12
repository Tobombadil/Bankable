"""gb.find_a_tender — UK Find a Tender Service, OCDS release packages.

Fetch: GET `/api/1.0/ocdsReleasePackages?updatedFrom=<now-2d>&limit=100`, following
`links.next` cursors (capped); pages are bundled into one JSON document. Rolling window, so
`snapshot_mode = incremental`.
Parse: keep releases with an energy CPV (09*, 31*, 45231*, 71314*) in `tender.classification`,
`tender.items[].classification` / `additionalClassifications` or lot items; when one ocid has
several releases in the window the latest by `date` wins.
source_record_id: the `ocid` (the procurement process; releases are its versions).
Personal data: `parties[].contactPoint` (named officers, emails, phones) is removed before the
snapshot is stored (`redact`, docs/13 §5.4 rule 1) — buyer organisations stay.
Reuse: OGL v3 with credit.
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
from pipeline.connectors.opportunity import classify_technologies, deadline_passed, technologies_str, to_utc

API_URL = "https://www.find-tender.service.gov.uk/api/1.0/ocdsReleasePackages"
NOTICE_URL = "https://www.find-tender.service.gov.uk/Notice/{notice_id}"
CPV_PREFIXES = ("09", "31", "45231", "71314")
MAX_PAGES = 40
WINDOW_DAYS = 2


def release_cpvs(rel: dict[str, Any]) -> list[str]:
    tender = rel.get("tender") or {}
    codes: list[str] = []

    def add(c: Any) -> None:
        if isinstance(c, dict) and str(c.get("scheme", "CPV")).upper() == "CPV" and c.get("id"):
            codes.append(str(c["id"]))

    add(tender.get("classification"))
    for item in tender.get("items") or []:
        add(item.get("classification"))
        for c in item.get("additionalClassifications") or []:
            add(c)
    for lot in tender.get("lots") or []:
        add(lot.get("classification"))
    return sorted(set(codes))


def is_energy(codes: list[str]) -> bool:
    return any(c.startswith(CPV_PREFIXES) for c in codes)


def strip_contacts(package: dict[str, Any]) -> dict[str, Any]:
    for rel in package.get("releases") or []:
        for party in rel.get("parties") or []:
            party.pop("contactPoint", None)
        for key in ("buyer", "tender"):
            obj = rel.get(key)
            if isinstance(obj, dict):
                obj.pop("contactPoint", None)
    return package


class Connector(BaseConnector):
    source_id: ClassVar[str] = "gb.find_a_tender"
    kind: ClassVar[Kind] = "opportunity"
    ext: ClassVar[str] = "json"
    honour_robots: ClassVar[bool] = False
    snapshot_mode: ClassVar[SnapshotMode] = "incremental"
    status_key: ClassVar[str] = "find_a_tender"
    status_map_path: ClassVar[pathlib.Path | None] = pathlib.Path(__file__).with_name("status_map.yaml")
    personal_data_columns: ClassVar[tuple[str, ...]] = ("contactPoint",)
    key_source_columns: ClassVar[tuple[str, ...]] = ("ocid", "id", "tag", "tender", "buyer")

    def fetch(self) -> RawSnapshot:
        t0 = time.monotonic()
        now = dt.datetime.now(dt.UTC)
        since = (now - dt.timedelta(days=WINDOW_DAYS)).strftime("%Y-%m-%dT%H:%M:%S")
        url: str | None = f"{API_URL}?updatedFrom={since}&limit=100"
        pages: list[dict[str, Any]] = []
        while url and len(pages) < MAX_PAGES:
            r = self.http.get(url, honour_robots=False, timeout=120)
            if r.status_code != 200:
                raise ConnectorError(f"GET {url} -> HTTP {r.status_code}")
            page = r.json()
            pages.append(page)
            if not page.get("releases"):
                break
            url = (page.get("links") or {}).get("next")
        payload = json.dumps({"request": {"updatedFrom": since}, "pages": pages}, ensure_ascii=False).encode(
            "utf-8"
        )
        return RawSnapshot(
            content=payload,
            content_type="application/json",
            url=API_URL,
            retrieved_at=now,
            http_status=200,
            ext="json",
            elapsed_s=round(time.monotonic() - t0, 2),
            requests_made=len(pages),
            meta={"updated_from": since, "pages": len(pages)},
        )

    def redact(self, content: bytes) -> bytes:
        doc = json.loads(content)
        doc["pages"] = [strip_contacts(p) for p in doc.get("pages", [])]
        return json.dumps(doc, ensure_ascii=False).encode("utf-8")

    def parse(self, raw: RawSnapshot) -> list[dict[str, Any]]:
        doc = json.loads(raw.content)
        latest: dict[str, dict[str, Any]] = {}
        for page in doc.get("pages", []):
            if "releases" not in page:
                raise ParseError(f"FTS page without releases: {str(page)[:200]}")
            for rel in page["releases"]:
                codes = release_cpvs(rel)
                if not is_energy(codes):
                    continue
                ocid = str(rel.get("ocid") or "")
                if not ocid:
                    continue
                tender = rel.get("tender") or {}
                row = {
                    "ocid": ocid,
                    "id": rel.get("id"),
                    "tag": list(rel.get("tag") or []),
                    "date": rel.get("date"),
                    "buyer": (rel.get("buyer") or {}).get("name"),
                    "cpv": codes,
                    "tender": {
                        k: tender.get(k)
                        for k in (
                            "id",
                            "title",
                            "description",
                            "status",
                            "value",
                            "tenderPeriod",
                            "mainProcurementCategory",
                        )
                    },
                    "awards": [
                        {
                            "id": a.get("id"),
                            "status": a.get("status"),
                            "value": a.get("value"),
                            "date": a.get("date"),
                        }
                        for a in rel.get("awards") or []
                    ],
                }
                prev = latest.get(ocid)
                if prev is None or str(row["date"]) >= str(prev["date"]):
                    latest[ocid] = row
        return list(latest.values())

    def normalize(self, rows: list[dict[str, Any]], raw: RawSnapshot) -> pd.DataFrame:
        now = raw.retrieved_at
        recs: list[dict[str, Any]] = []
        for r in rows:
            t = r.get("tender") or {}
            tags = r.get("tag") or []
            tag = (
                "award"
                if "award" in tags
                else "tenderCancellation"
                if "tenderCancellation" in tags
                else tags[0]
                if tags
                else ""
            )
            period = t.get("tenderPeriod") or {}
            due = to_utc(period.get("endDate"))
            ctx = {"status_raw": t.get("status"), "tag": tag, "deadline_passed": deadline_passed(due, now)}
            state, rule = harmonise_status(self.status_key, ctx, self.status_map)
            value = t.get("value") or {}
            title = t.get("title")
            recs.append(
                {
                    "source_record_id": r["ocid"],
                    "source_url": NOTICE_URL.format(notice_id=r.get("id")),
                    "kind": "procurement_notice" if tag in ("planning", "planningUpdate") else "tender",
                    "issuer": r.get("buyer"),
                    "title": title,
                    "summary": None,
                    "jurisdiction": "GB",
                    "technologies": technologies_str(classify_technologies(title, t.get("description"))),
                    "capacity_sought_mw": None,
                    "budget_amount": float(value["amount"])
                    if isinstance(value.get("amount"), int | float)
                    else None,
                    "budget_currency": value.get("currency"),
                    "open_at": to_utc(period.get("startDate") or r.get("date")),
                    "due_at": due,
                    "status": state,
                    "status_raw": t.get("status"),
                    "status_rule": rule,
                    "identifiers": json.dumps(
                        {
                            "ocid": r["ocid"],
                            "fts_notice_id": r.get("id"),
                            "cpv": r.get("cpv"),
                            "tender_id": t.get("id"),
                        }
                    ),
                }
            )
        df = pd.DataFrame(recs, columns=list(recs[0]) if recs else ["source_record_id"])
        return self.finalize(df, rows, raw)
