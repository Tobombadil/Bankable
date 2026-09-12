"""us.ferc.elibrary — FERC eLibrary undocumented JSON search backend (docket filings, stage evidence).

Fetch: POST `/eLibrarywebapi/api/Search/AdvancedSearch` (docs/02 §7, the "undocumented JSON
backend" caveat). Two operational findings from probing this endpoint on 2026-09-12, recorded
here because they shape the design and are not written down anywhere else yet:

1. `filterDate` has **no observable effect**. `totalHits` for a description search was identical
   (33,511) whether `filterDate` was a 30-day window, a 1-day window, or omitted, across every
   `dateType` value tried (`filed_date`, `FILED`, `issued_date`). This connector therefore filters
   the "last N days" window (`WINDOW_DAYS`) **client-side** on `filedDate`, not server-side.
2. `sortBy` accepts only the empty string in this deployment; any other value observed (including
   plausible ones like `"filed_date"` or `"filed_date desc"`) returns **HTTP 200 with
   `success: false`** and `errorMessage: "Object reference not set to an instance of an object."`
   — a live instance of the `docs/02` §7 "HTTP 200 with success:false must be treated as error"
   caveat, caused by a server-side null-reference exception. `sortBy` is therefore always sent as
   `""` and the recorded fixture `ferc_elibrary_search_error.json` is one real `sortBy` misfire,
   used to test the "treat as error, retry with backoff" handling.

Given (1), relevance-only description search does not surface recent filings (verified: the top
hits for "interconnection agreement" by description are all 2003-2008). What *does* work: FERC's
own docket-numbering convention embeds the two-digit filing year (`ER26-...`, `CP26-...`), so a
**docket-number substring search** (`searchDocketNumber: true`) on `ER<yy>` / `CP<yy>` for the
year(s) the window spans returns filings that are, empirically, overwhelmingly recent (measured:
23 of 25 `ER26` hits and all 25 `CP26` hits on 2026-09-12 fell within the prior 14 days). This is
the primary channel; the `DESCRIPTION_TERMS` searches (searchDescription: true) are a secondary,
best-effort pass to catch matching filings under other docket-number patterns, and most of what
they turn up falls outside the window and is dropped by the same client-side filter.
Docket classes: ER (electric rate filings, including LGIAs) and CP (gas certificates), per
`data/sources.yaml` notes and `docs/02` §7.

Parse: `data.pages[].response.searchHits[]`, deduplicated on the misspelled-but-real API key
`acesssionNumber` (verified on the wire 2026-09-12; not a typo in this module), kept only when
(a) at least one `docketNumbers` entry starts with `ER` or `CP` and (b) `filedDate` falls in
`[retrieved_at - WINDOW_DAYS, retrieved_at]`. `success: false` on any page fails the run closed.
Normalise: `kind = "document"` (docs/21 §3.8) — a filing does not change lifecycle state once
accessioned, so `lifecycle_state` is the constant `"filed"` and `capacity_mw`/`proposed_cod` stay
null; they exist only so this kind flows through the shared DQ/diff machinery (`base.py`).
`docket_refs` is a `|`-joined string of every docket number the filing was filed under (the field
the docket-linkage script keys on, `docs/22` §10). `project_name_hint`/`state_hint` are a
best-effort regex lift from `description` (a capitalised phrase ending "Project"/"Pipeline"/etc.,
and a state name or ", XX" abbreviation); most filings carry neither.
source_record_id: `acesssionNumber` (unique per filing, e.g. `20260911-5219`).
source_url: `https://elibrary.ferc.gov/eLibrary/filelist?accession_number=<accession>` — a stable,
publicly reachable URL pattern for the eLibrary file list (verified 200 on 2026-09-12); the SPA
resolves the file list client-side from the query parameter.
Reuse: US federal government work, public domain (`data/sources.yaml`); no gating needed.
Rate limit: 0.5 rps (docs/02 §7, `registry.HOST_DEFAULT_RPS["elibrary.ferc.gov"]`). A full run
issues up to ~18 POSTs (2 docket classes x up to 2 years x 3 pages, plus 3 description terms x 2
pages), so a run takes roughly half a minute; acceptable at the `cadence: realtime` polling window
this feeds (a scheduler, not this module, decides how often to run it).
"""

from __future__ import annotations

import datetime as dt
import html
import json
import re
import time
from typing import Any, ClassVar

import pandas as pd

from pipeline.connectors.base import Connector as BaseConnector
from pipeline.connectors.base import ConnectorError, Kind, ParseError, RawSnapshot, SnapshotMode
from pipeline.normalize import US_STATES

API_URL = "https://elibrary.ferc.gov/eLibrarywebapi/api/Search/AdvancedSearch"
FILELIST_URL = "https://elibrary.ferc.gov/eLibrary/filelist?accession_number={accession}"
RESULTS_PER_PAGE = 100
MAX_ATTEMPTS = 3

_DOCKET_PREFIX_RE = re.compile(r"^([A-Z]+)")
_PROJECT_HINT_RE = re.compile(
    r"\b((?:[A-Z][A-Za-z0-9&.'-]*\s+){1,6}(?:Project|Pipeline|Expansion|Terminal|Facility|Storage Hub))\b"
)
_STATE_ABBR_RE = re.compile(r",\s*([A-Z]{2})\b")
_STATE_ABBRS = frozenset(US_STATES.values())
_STATE_NAME_RE = re.compile(
    r"\b(" + "|".join(sorted((k.title() for k in US_STATES), key=len, reverse=True)) + r")\b"
)


def _project_name_hint(text: str) -> str | None:
    m = _PROJECT_HINT_RE.search(text)
    return m.group(1).strip() if m else None


def _state_hint(text: str) -> str | None:
    m = _STATE_ABBR_RE.search(text)
    if m and m.group(1) in _STATE_ABBRS:
        return m.group(1)
    m2 = _STATE_NAME_RE.search(text)
    if m2:
        return US_STATES[m2.group(1).lower()]
    return None


def _doc_type(category: str | None, document_class: str | None) -> str:
    if document_class == "Notice":
        return "notice"
    if category == "Issuance":
        return "order"
    return "filing"


def _parse_mdy(v: Any) -> dt.date | None:
    if not v:
        return None
    try:
        month, day, year = (int(p) for p in str(v).split("/"))
        return dt.date(year, month, day)
    except ValueError:
        return None


def _search_body(
    search_text: str, *, description: bool, docket_number: bool, page: int, start: dt.date, end: dt.date
) -> dict[str, Any]:
    """The working request shape from `data/sources.yaml`'s probe. `sortBy` is always `""`
    (see module docstring finding 2); `filterDate` is sent for the record even though it has no
    measured effect (finding 1) — a future backend fix would then narrow results for free."""
    return {
        "searchText": search_text,
        "searchDescription": description,
        "searchFullText": False,
        "searchDocketNumber": docket_number,
        "resultsPerPage": RESULTS_PER_PAGE,
        "curPage": page,
        "sortBy": "",
        "groupBy": "DOCKET",
        "filterDate": {
            "dateType": "filed_date",
            "startDate": start.strftime("%m/%d/%Y"),
            "endDate": end.strftime("%m/%d/%Y"),
        },
        "docketNumbers": [],
        "documentClasses": [],
        "documentTypes": [],
        "availability": "P",
        "categories": [],
        "idList": [],
        "accessionNumber": "",
        "libraryTypes": [],
        "filedBy": "",
        "affiliations": "",
    }


class Connector(BaseConnector):
    source_id: ClassVar[str] = "us.ferc.elibrary"
    kind: ClassVar[Kind] = "document"
    ext: ClassVar[str] = "json"
    honour_robots: ClassVar[bool] = False  # JSON API, POST only
    snapshot_mode: ClassVar[SnapshotMode] = "incremental"
    key_source_columns: ClassVar[tuple[str, ...]] = (
        "acesssionNumber",
        "description",
        "docketNumbers",
        "filedDate",
        "classTypes",
        "category",
        "affiliations",
    )
    #: how far back a run looks for filings (docs/02 §7 "realtime" cadence; the scheduler decides
    #: how often this connector actually runs, so the window is wide enough to survive a missed run).
    WINDOW_DAYS: ClassVar[int] = 30
    DOCKET_CLASSES: ClassVar[tuple[str, ...]] = ("ER", "CP")
    DESCRIPTION_TERMS: ClassVar[tuple[str, ...]] = (
        "interconnection agreement",
        "large generator",
        "certificate",
    )
    MAX_PAGES_DOCKET: ClassVar[int] = 3
    MAX_PAGES_DESCRIPTION: ClassVar[int] = 2

    def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        """POST with retry-with-backoff on `success: false` (docs/02 §7); 5xx/429/connection
        errors are already retried inside `self.http.post`."""
        last_error = ""
        for attempt in range(1, MAX_ATTEMPTS + 1):
            r = self.http.post(API_URL, json=body, timeout=90)
            if r.status_code != 200:
                raise ConnectorError(f"POST {API_URL} -> HTTP {r.status_code}")
            doc: dict[str, Any] = r.json()
            if doc.get("success", True):
                return doc
            last_error = str(doc.get("errorMessage"))
            if attempt < MAX_ATTEMPTS:
                time.sleep(2.0**attempt)
        raise ConnectorError(f"FERC eLibrary success:false after {MAX_ATTEMPTS} attempts: {last_error}")

    def fetch(self) -> RawSnapshot:
        t0 = time.monotonic()
        now = dt.datetime.now(dt.UTC)
        start = (now - dt.timedelta(days=self.WINDOW_DAYS)).date()
        end = now.date()
        years = sorted({f"{start.year % 100:02d}", f"{end.year % 100:02d}"})

        docket_queries = [
            {
                "text": f"{cls}{yy}",
                "description": False,
                "docket_number": True,
                "max_pages": self.MAX_PAGES_DOCKET,
            }
            for cls in self.DOCKET_CLASSES
            for yy in years
        ]
        description_queries = [
            {
                "text": term,
                "description": True,
                "docket_number": False,
                "max_pages": self.MAX_PAGES_DESCRIPTION,
            }
            for term in self.DESCRIPTION_TERMS
        ]
        queries: list[dict[str, Any]] = docket_queries + description_queries

        pages: list[dict[str, Any]] = []
        for q in queries:
            for page_num in range(1, int(q["max_pages"]) + 1):
                body = _search_body(
                    str(q["text"]),
                    description=bool(q["description"]),
                    docket_number=bool(q["docket_number"]),
                    page=page_num,
                    start=start,
                    end=end,
                )
                doc = self._post(body)
                pages.append({"request": body, "response": doc})
                if len(doc.get("searchHits") or []) < RESULTS_PER_PAGE:
                    break

        payload = json.dumps({"pages": pages}, ensure_ascii=False).encode("utf-8")
        return RawSnapshot(
            content=payload,
            content_type="application/json",
            url=API_URL,
            retrieved_at=now,
            http_status=200,
            ext="json",
            elapsed_s=round(time.monotonic() - t0, 2),
            requests_made=len(pages),
            meta={"queries": len(queries), "pages": len(pages), "window_days": self.WINDOW_DAYS},
        )

    def parse(self, raw: RawSnapshot) -> list[dict[str, Any]]:
        doc = json.loads(raw.content)
        window_start = raw.retrieved_at.date() - dt.timedelta(days=self.WINDOW_DAYS)
        window_end = raw.retrieved_at.date()
        seen: dict[str, dict[str, Any]] = {}
        for page in doc.get("pages", []):
            resp = page["response"]
            if not resp.get("success", True):
                raise ParseError(f"FERC eLibrary error: {resp.get('errorMessage')}")
            for hit in resp.get("searchHits") or []:
                acc = hit.get("acesssionNumber")
                if not acc or acc in seen:
                    continue
                docket_nums = [d for d in (hit.get("docketNumbers") or []) if d]
                classes = {m.group(1) for d in docket_nums if (m := _DOCKET_PREFIX_RE.match(d))}
                if not classes & set(self.DOCKET_CLASSES):
                    continue
                filed = _parse_mdy(hit.get("filedDate"))
                if filed is None or not (window_start <= filed <= window_end):
                    continue
                seen[acc] = hit
        return list(seen.values())

    def normalize(self, rows: list[dict[str, Any]], raw: RawSnapshot) -> pd.DataFrame:
        recs: list[dict[str, Any]] = []
        for hit in rows:
            accession = str(hit.get("acesssionNumber"))
            docket_nums = [d for d in (hit.get("docketNumbers") or []) if d]
            description = html.unescape(str(hit.get("description") or "")).strip()
            class_types = (hit.get("classTypes") or [{}])[0]
            document_class = class_types.get("documentClass")
            document_type = class_types.get("documentType")
            affiliations = hit.get("affiliations") or []
            authors = [
                a.get("affiliation")
                for a in affiliations
                if a.get("afType") == "AUTHOR" and a.get("affiliation")
            ]
            filer = authors[0] if authors else (affiliations[0].get("affiliation") if affiliations else None)
            filed_date = _parse_mdy(hit.get("filedDate"))
            recs.append(
                {
                    "source_record_id": accession,
                    "source_url": FILELIST_URL.format(accession=accession),
                    "doc_type": _doc_type(hit.get("category"), document_class),
                    "title": description or None,
                    "published_date": pd.Timestamp(filed_date, tz="UTC") if filed_date else None,
                    "accession_number": accession,
                    "docket_refs": "|".join(docket_nums),
                    "filer": filer,
                    "affiliations": "|".join(
                        f"{a.get('afType')}:{a.get('affiliation')}"
                        for a in affiliations
                        if a.get("affiliation")
                    ),
                    "document_class": document_class,
                    "document_type": document_type,
                    "project_name_hint": _project_name_hint(description) if description else None,
                    "state_hint": _state_hint(description) if description else None,
                    "identifiers": json.dumps(
                        {
                            "accession_number": accession,
                            "docket_numbers": docket_nums,
                            "document_id": hit.get("documentId"),
                        }
                    ),
                    "lifecycle_state": "filed",
                    "status_raw": None,
                    "status_rule": "ferc_elibrary.filed",
                    "capacity_mw": None,
                    "proposed_cod": None,
                }
            )
        df = pd.DataFrame(recs, columns=list(recs[0]) if recs else ["source_record_id"])
        return self.finalize(df, rows, raw)
