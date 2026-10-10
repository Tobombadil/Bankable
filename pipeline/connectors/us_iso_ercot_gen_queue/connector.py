"""us.iso.ercot.gen_queue — ERCOT GIS Report (EMIL PG7-200-ER, MIS reportTypeId 15933).

Fetch: the public MIS document list (`IceDocListJsonWS?reportTypeId=15933`) lists monthly
`GIS_Report_<Month><Year>.xlsx` files (and the co-located battery report, which is skipped); the
newest GIS_Report is downloaded via `mirDownload?doclookupId=<DocID>`.
Unchanged poll (2026-10-10): ERCOT is polled daily for a report published monthly, so on most days
the listing names the document the previous run already stored. When the listed DocID and publish
date are the ones the previous snapshot recorded (`snapshot.meta`), `fetch` returns the stored bytes
without downloading the workbook again, and the runner's SHA comparison ends the run `unchanged`.
An unchanged day costs the listing alone: one request of about 80 KB (80,098 bytes on 2026-09-12)
in place of two requests and about 0.75 MB (the September 2026 workbook is 673,369 bytes). A
stored object that cannot be read or fails its SHA-256 costs one download, no more.
Parse: gridstatus 0.36.0's ERCOT parser (vendored, `pipeline/vendor/gridstatus`) over the
"Project Details - Large Gen" sheet.
source_record_id: the ERCOT INR ("Queue ID"), unique within a report.
Reuse: open (Website User Agreement clause 5) — raw-ok. Rate: 0.5 rps (clause 6).
"""

from __future__ import annotations

import logging
import time
from typing import Any, ClassVar

import pandas as pd

from pipeline.connectors.base import Connector as BaseConnector
from pipeline.connectors.base import (
    ConnectorError,
    Kind,
    ParseError,
    PreviousSnapshot,
    RawSnapshot,
    RemovalMeaning,
)
from pipeline.connectors.iso_queue import XLSX_MAGIC, normalize_iso_rows, queue_rows, restate_iso_status

DOC_LIST = "https://www.ercot.com/misapp/servlets/IceDocListJsonWS?reportTypeId=15933"
DOWNLOAD = "https://www.ercot.com/misdownload/servlets/mirDownload?doclookupId={doc_id}"
log = logging.getLogger(__name__)


def latest_gis_document(doc_list: dict[str, Any]) -> dict[str, Any]:
    docs = [d["Document"] for d in doc_list["ListDocsByRptTypeRes"]["DocumentList"]]
    gis = [d for d in docs if "GIS_Report" in d.get("ConstructedName", "") and d.get("Extension") == "xlsx"]
    if not gis:
        raise ParseError("no GIS_Report xlsx in the ERCOT document list")
    return max(gis, key=lambda d: str(d.get("PublishDate", "")))


class Connector(BaseConnector):
    source_id: ClassVar[str] = "us.iso.ercot.gen_queue"
    kind: ClassVar[Kind] = "proposal"
    ext: ClassVar[str] = "xlsx"
    honour_robots: ClassVar[bool] = False  # MIS servlets are an API, not a crawlable site
    status_key: ClassVar[str] = "ercot"
    #: Not `withdrawn`, though an ERCOT removal is the report's only departure signal (docs/22 §8):
    #: the report's own notes say it "Excludes projects that have a status of Inactive (status = INA)",
    #: and a developer may split a project into new INR numbers, so a row also leaves when the
    #: project goes inactive or is re-numbered (the NOTES block of tests/fixtures/ercot_gis_report.xlsx).
    removal_meaning: ClassVar[RemovalMeaning] = "unknown"
    #: For the same reason a removal is announced as `delisted`, "No longer in ERCOT's report
    #: (reason not stated)", never as a withdrawal (owner decision 2026-10-10).
    announce_removals: ClassVar[bool] = True
    register_name: ClassVar[str | None] = "ERCOT"
    #: `project_root` is the base default, the INR itself: a split project gets "different INR
    #: numbers" (the report's own note), which share nothing with the original, and the two
    #: letter-suffixed INRs in the September 2026 report (15INR0064b, 17INR0027b, of 1,758) have no
    #: sibling under the same stem, so no key convention groups one project's rows.
    key_source_columns: ClassVar[tuple[str, ...]] = (
        "Queue ID",
        "Project Name",
        "Status",
        "Capacity (MW)",
        "Generation Type",
        # The three milestone dates decide the lifecycle state (pipeline/status_map.yaml `ercot`);
        # losing one would silently move rows between states, so its removal holds the run.
        "IA Signed",
        "Approved for Energization",
        "Approved for Synchronization",
    )

    def _stored_report(self, url: str, meta: dict[str, Any]) -> tuple[bytes, PreviousSnapshot] | None:
        """The previous snapshot's bytes when it is the document the listing names now: the same
        DocID, fetched from the same URL, with the same publish date where both record one (module
        docstring, "Unchanged poll"). None means download: no previous snapshot, a different or
        re-published document, or a stored object that cannot be read or fails its SHA-256."""
        stored = self.previous
        if stored is None:
            return None
        before = stored.meta
        if str(before.get("doc_id") or "") != str(meta["doc_id"]):
            return None
        if (stored.record.get("snapshot") or {}).get("fetched_url") != url:
            return None
        if before.get("publish_date") and before.get("publish_date") != meta.get("publish_date"):
            return None
        try:
            body = stored.content()
        except Exception as e:  # an unusable stored object costs one download, no more
            log.warning("stored GIS report not reusable: %r", e, extra={"source_id": self.source_id})
            return None
        return (body, stored) if body.startswith(XLSX_MAGIC) else None

    def fetch(self) -> RawSnapshot:
        t0 = time.monotonic()
        listing = self.http.get(DOC_LIST, honour_robots=False)
        try:
            doc = latest_gis_document(listing.json())
        except (ValueError, KeyError) as e:
            raise ConnectorError(f"ERCOT document list unreadable: {e!r}") from e
        url = DOWNLOAD.format(doc_id=doc["DocID"])
        meta: dict[str, Any] = {
            "doc_id": doc["DocID"],
            "friendly_name": doc.get("FriendlyName"),
            "publish_date": doc.get("PublishDate"),
            "product": "PG7-200-ER",
        }
        reusable = self._stored_report(url, meta)
        if reusable is not None:
            body, stored = reusable
            snapshot = stored.record.get("snapshot") or {}
            return RawSnapshot(
                content=body,
                content_type=str(snapshot.get("content_type") or ""),
                url=url,
                retrieved_at=self.now(),
                http_status=listing.status_code,
                ext="xlsx",
                headers={},
                elapsed_s=round(time.monotonic() - t0, 2),
                requests_made=1,
                meta={
                    **meta,
                    "reused": {
                        "run_id": stored.record.get("id"),
                        "sha256": stored.sha256,
                        "reason": "the listing names the document already stored",
                    },
                },
            )
        r = self.http.get(url, honour_robots=False, timeout=180)
        if r.status_code != 200:
            raise ConnectorError(f"GET {url} -> HTTP {r.status_code}")
        return RawSnapshot(
            content=r.content,
            content_type=r.headers.get("Content-Type", ""),
            url=url,
            retrieved_at=self.now(),
            http_status=r.status_code,
            ext="xlsx",
            headers=dict(r.headers),
            elapsed_s=round(time.monotonic() - t0, 2),
            requests_made=2,
            meta=meta,
        )

    def parse(self, raw: RawSnapshot) -> list[dict[str, Any]]:
        return queue_rows("Ercot", raw)

    def normalize(self, rows: list[dict[str, Any]], raw: RawSnapshot) -> pd.DataFrame:
        return normalize_iso_rows(self, "ercot", rows, raw)

    def restate_status(self, df: pd.DataFrame) -> pd.DataFrame | None:
        return restate_iso_status(self, "ercot", df)
