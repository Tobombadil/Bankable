"""us.epa.class_vi — EPA UIC Class VI permit tracker (CO2 geologic sequestration applications).

Fetch: two steps, both measured 2026-09-22.
 1. GET the landing page `.../uic/current-class-vi-projects-under-review-epa` through the polite
    session (robots honoured; epa.gov's robots.txt disallows nothing under `/uic`). The page is
    scraped for three things: the Qlik Sense **app id** behind the embedded dashboard, the dated
    "UIC Class VI Permit Tracker (pdf)" link, and the six state primacy links. Only the app id is
    used to fetch; the other two are recorded in `meta` as evidence.
 2. Open the Qlik Sense Engine JSON API over a websocket at
    `wss://awsedap.epa.gov/public/app/<app id>` and read the app's own data table. EPA's EDAP
    `/public/` virtual proxy answers anonymously (`OnAuthenticationInformation` returns
    `mustAuthenticate: false`, `userId: anonymous…`), so no credential, no login and no challenge
    bypass is involved; `GetTablesAndKeys` names the single table `External` and its 143 fields,
    and a session hypercube over `FIELDS` pages the rows out. The PDF is *not* the data route:
    it is a single-page Gantt **image** run through Adobe's Paper Capture OCR plug-in, with
    project names truncated mid-word ("Carbon Storage Solutions, LLC: Front Range Storage .. .")
    and no per-project dates at all. It is recorded as a URL, never parsed.

Parse: the payload is the JSON document `fetch` assembled. The app's table is a spreadsheet
export, so it carries trailing blank rows (243 `RowOrder` values, 68 with a project on
2026-09-22); rows with no Project Name are dropped, exactly as the EIA-860M parser drops the
workbook's trailing note rows. The NOD (notice of deficiency) and RAI (request for additional
information) ladders — up to 4 and 11 rounds, each a sent/received date pair — are folded into
two nested lists per project, the same shape `us.permits_dashboard` uses for its milestones.

Grain: one row per **project**, not per well. Each Class VI well needs its own application and
permit; `# of Permit Applications for Project` carries the well count (235 applications across
the 68 projects on 2026-09-22), which is where the "175+ applications" in the old manifest note
came from.

source_record_id: `GSDT Project ID` (EPA's own Geologic Sequestration Data Tool project id, e.g.
`R05-IN-0001`) when the row carries one — 47 of 68 did on 2026-09-22 — else a content hash over
(Company Name, Project Name, State, County). The pair (Company Name, Project Name) was unique
across all 68 rows, and a hash over it is stable while the row's identity is; `dedupe_strategy`
stays `hold` so a genuine collision stops the run rather than being papered over.

Tribal land: EPA's `State` column is a UIC jurisdiction label, not always a state — two rows on
2026-09-22 read `Osage Nation` and `All Other Indian Tribes` (both Osage County, Oklahoma, both
flagged `Land Type: Tribal Land`). `norm_state` returns None for them rather than guessing, so
`state` is null on those two rows and `county` carries the nation's name as EPA wrote it.

technology: `pipeline.normalize.classify_tech` is deliberately **not** called here. Its
`\bstorage\b` rule would classify a CO2 geologic sequestration project as battery `storage`,
kind `generation`/`storage` — wrong in both fields. Every row in this dataset is the same thing
by construction of the source, so `kind` is the constant `ccs` (docs/02 §5 vocabulary) and
`technology` the constant `co2_geologic_sequestration`. `technology_raw` stays null because the
source has no technology column to quote; it is left out of `dq_required_fields` so a
permanently-null field cannot raise a spurious null spike.

capacity_mw / proposed_cod: null. The tracker carries neither an injection volume nor a
commercial-operation date. `Final Permit Decision- Estimated Date` is EPA's estimate of its own
decision, not a project COD, and is kept in `raw` rather than misfiled as one.

Personal data: the app's table has a `Primary Permit Writer` field (named EPA staff, 21 distinct
values). It is not in `FIELDS`, so it is never requested, never snapshotted and never stored
(`CLAUDE.md` "store the minimum personal data"; docs/13 §5). `Current Status` is EPA's own public
status note and can name the applicant company; it is published data on the dashboard and is kept.

Reuse: US federal government work, public domain (17 U.S.C. §105, under the EPA hedge recorded at
docs/13 §2.12). `publication: raw_ok`.
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib
import re
import time
from collections.abc import Callable
from typing import Any, ClassVar
from urllib.parse import urljoin

import pandas as pd

from pipeline.connectors.base import Connector as BaseConnector
from pipeline.connectors.base import ConnectorError, Kind, ParseError, RawSnapshot, content_hash
from pipeline.connectors.canonical import (
    harmonise_status,
    norm_county,
    norm_name,
    norm_org,
    norm_state,
    to_date,
)

PAGE_URL = "https://www.epa.gov/uic/current-class-vi-projects-under-review-epa"
ENGINE_HOST = "awsedap.epa.gov"
ENGINE_URL = "wss://awsedap.epa.gov/public/app/{app_id}"
DASHBOARD_URL = "https://awsedap.epa.gov/public/single/?appid={app_id}"

APP_ID_RE = re.compile(r"awsedap\.epa\.gov/public/single/\?appid=([0-9a-f-]{36})", re.I)
TRACKER_PDF_RE = re.compile(r'href="([^"]*permit-tracker[^"]*\.pdf)"', re.I)
#: The six states EPA has granted Class VI primacy to. Applications there are transferred to the
#: state and are *not* in this tracker — the page says so and links each state's own page.
#: Registered separately in `data/sources.yaml` (`us.<state>.class_vi`), never inferred from here.
PRIMACY_STATES = ("Arizona", "Louisiana", "North Dakota", "Texas", "West Virginia", "Wyoming")

#: The app's single data table, as `GetTablesAndKeys` names it.
DATA_TABLE = "External"

CORE_FIELDS: tuple[str, ...] = (
    # `RowOrder` is the app's 1..243 spreadsheet row position. It is requested first so the
    # hypercube keeps one output row per source row: a Qlik straight table returns *distinct*
    # dimension combinations, so without it the 175 all-blank padding rows collapse and vanish
    # and two identical projects would silently merge. It is never an identity — see `_record_id`.
    "RowOrder",
    "Company Name",
    "Project Name",
    "Company Name: Project Name",
    "State",
    "County/Parish/Tribe",
    "EPA Region",
    "Land Type",
    "# of Permit Applications for Project",
    "GSDT Project ID",
    "URL - Vlookup",
    "Last Updated",
    "Application Received Date",
    "Phase",
    "Current Status",
    "On Hold Status",
    "On Hold Start Date",
    "On Hold End Date",
    "Schedule Pending?",
    "Corrected Waiting on Applicant Response?",
    "Tribal Consultation Requested?",
    "Duration (# of Days)",
    "Total NOD Response Time (Days)",
    "Total RAI Response Time (Days)",
    "Days on Applicant Requested Hold",
    "24 Month Goal Date",
)
#: EPA spells the public-comment estimate "Estimate Date" and every other milestone "Estimated
#: Date". Both spellings are quoted verbatim; a silent rename shows up as a fetch-time field error.
MILESTONE_FIELDS: tuple[str, ...] = (
    "Administratively Complete Determination- Estimated Date",
    "Administratively Complete Determination- Actual Date",
    "Technical Review Completion- Estimated Date",
    "Technical Review Completion- Actual Date",
    "Draft Permit- Estimated Date",
    "Draft Permit- Actual Date",
    "Public Comment Period Complete- Estimate Date",
    "Public Comment Period Complete- Actual Date",
    "Final Permit Decision- Estimated Date",
    "Final Permit Decision- Actual Date",
    "Withdrawn or Permit Expired Date",
)
NOD_ROUNDS = 4
RAI_ROUNDS = 11
NOD_FIELDS: tuple[str, ...] = tuple(
    f"NOD {n} {suffix}" for n in range(1, NOD_ROUNDS + 1) for suffix in ("Sent", "Response Rec'd")
)
RAI_FIELDS: tuple[str, ...] = tuple(
    f"RAI {n} {suffix}" for n in range(1, RAI_ROUNDS + 1) for suffix in ("Sent", "Response Rec'd")
)
FIELDS: tuple[str, ...] = CORE_FIELDS + MILESTONE_FIELDS + NOD_FIELDS + RAI_FIELDS

#: Qlik pages a hypercube at most 10,000 cells at a time; 100 rows x 66 fields stays well inside.
PAGE_HEIGHT = 100
#: Guard against an unbounded read if the app ever grows or a page never shortens.
MAX_ROWS = 20_000


def find_app_id(html: str) -> str | None:
    """The Qlik Sense app id behind the embedded dashboard, or None if the embed changed."""
    m = APP_ID_RE.search(html)
    return m.group(1).lower() if m else None


def find_tracker_pdf(html: str, base: str = PAGE_URL) -> str | None:
    """The dated `permit-tracker_<m-d-yy>.pdf` link. Recorded as evidence, never parsed."""
    m = TRACKER_PDF_RE.search(html)
    return urljoin(base, m.group(1)) if m else None


class QlikEngine:
    """Minimal Qlik Sense Engine JSON API client: open a doc, page one session hypercube.

    The transport is injectable (`connect`) so the parser tests drive a recorded exchange and
    `fetch()` never runs in CI (docs/20 §3.1).
    """

    def __init__(self, app_id: str, connect: Callable[..., Any] | None = None, timeout: float = 90.0) -> None:
        self.app_id = app_id
        self.timeout = timeout
        self._connect = connect or _default_connect
        self._ws: Any = None
        self._id = 0
        self.doc_handle = -1
        self.messages_sent = 0

    def __enter__(self) -> QlikEngine:
        self._ws = self._connect(ENGINE_URL.format(app_id=self.app_id), self.timeout)
        # Two unsolicited frames precede any reply: OnAuthenticationInformation, OnConnected.
        self.greeting = json.loads(self._ws.recv())
        self.connected = json.loads(self._ws.recv())
        self.doc_handle = int(self.call("OpenDoc", -1, [self.app_id])["result"]["qReturn"]["qHandle"])
        return self

    def __exit__(self, *_exc: object) -> None:
        if self._ws is not None:
            self._ws.close()

    def call(self, method: str, handle: int, params: list[Any]) -> dict[str, Any]:
        self._id += 1
        request = {"jsonrpc": "2.0", "id": self._id, "method": method, "handle": handle, "params": params}
        self._ws.send(json.dumps(request))
        self.messages_sent += 1
        while True:
            message: dict[str, Any] = json.loads(self._ws.recv())
            if message.get("id") != self._id:
                continue  # unsolicited OnConnected/change notification
            if "error" in message:
                raise ConnectorError(f"Qlik {method} failed: {message['error']}")
            return message

    def field_names(self) -> list[str]:
        """Every field of the app's data tables, for the fetch-time schema check."""
        result = self.call(
            "GetTablesAndKeys",
            self.doc_handle,
            [{"qcx": 1000, "qcy": 1000}, {"qcx": 0, "qcy": 0}, 30, True, False],
        )["result"]
        return [f["qName"] for table in result.get("qtr", []) for f in table.get("qFields", [])]

    def rows(self, fields: tuple[str, ...], page_height: int = PAGE_HEIGHT) -> list[dict[str, Any]]:
        """Every row of a session hypercube over `fields`, in the app's own row order."""
        handle = int(
            self.call(
                "CreateSessionObject",
                self.doc_handle,
                [
                    {
                        "qInfo": {"qType": "bankable-table"},
                        "qHyperCubeDef": {
                            "qDimensions": [
                                {"qDef": {"qFieldDefs": [f"[{f}]"]}, "qNullSuppression": False}
                                for f in fields
                            ],
                            "qMeasures": [],
                            "qMode": "S",
                            "qInterColumnSortOrder": list(range(len(fields))),
                            "qSuppressZero": False,
                            "qSuppressMissing": False,
                        },
                    }
                ],
            )["result"]["qReturn"]["qHandle"]
        )
        out: list[dict[str, Any]] = []
        while len(out) < MAX_ROWS:
            pages = self.call(
                "GetHyperCubeData",
                handle,
                [
                    "/qHyperCubeDef",
                    [{"qLeft": 0, "qTop": len(out), "qWidth": len(fields), "qHeight": page_height}],
                ],
            )["result"]["qDataPages"]
            matrix = pages[0]["qMatrix"] if pages else []
            if not matrix:
                break
            for row in matrix:
                out.append({f: _cell(c) for f, c in zip(fields, row, strict=True)})
            if len(matrix) < page_height:
                break
        return out


def _cell(cell: dict[str, Any]) -> str | None:
    """Qlik renders a null cell as the literal `-`; everything else is the displayed text."""
    text = cell.get("qText")
    if text is None:
        return None
    text = str(text).strip()
    return None if text in ("", "-") else text


def _default_connect(url: str, timeout: float) -> Any:
    """Real websocket transport. Imported lazily so the parser tests need no network stack."""
    from websocket import create_connection

    from pipeline.connectors.http import USER_AGENT

    return create_connection(
        url,
        header=[f"User-Agent: {USER_AGENT}"],
        origin=f"https://{ENGINE_HOST}",
        timeout=timeout,
    )


class Connector(BaseConnector):
    source_id: ClassVar[str] = "us.epa.class_vi"
    kind: ClassVar[Kind] = "proposal"
    ext: ClassVar[str] = "json"
    status_key: ClassVar[str] = "epa_class_vi"
    status_map_path: ClassVar[pathlib.Path | None] = pathlib.Path(__file__).with_name("status_map.yaml")
    dq_required_fields: ClassVar[tuple[str, ...]] = ("name_canonical", "sponsor_name", "state")
    key_source_columns: ClassVar[tuple[str, ...]] = (
        "Company Name",
        "Project Name",
        "State",
        "County/Parish/Tribe",
        "Phase",
        "Application Received Date",
        "# of Permit Applications for Project",
    )

    #: injectable transport for `fetch`; tests never use it (docs/20 §3.1)
    connect: Callable[..., Any] | None = None

    def fetch(self) -> RawSnapshot:
        t0 = time.monotonic()
        page = self.http.get(PAGE_URL)
        if page.status_code != 200:
            raise ConnectorError(f"GET {PAGE_URL} -> HTTP {page.status_code}")
        app_id = find_app_id(page.text)
        if not app_id:
            raise ConnectorError(f"{PAGE_URL} no longer embeds a Qlik dashboard (appid not found)")
        tracker_pdf = find_tracker_pdf(page.text)

        with QlikEngine(app_id, connect=self.connect) as engine:
            available = engine.field_names()
            missing = [f for f in FIELDS if f not in available]
            if missing:
                raise ConnectorError(
                    f"Class VI dashboard dropped {len(missing)} field(s): {missing[:6]} "
                    f"({len(available)} fields present)"
                )
            rows = engine.rows(FIELDS)
            messages = engine.messages_sent
            anonymous = bool(engine.greeting.get("params", {}).get("mustAuthenticate") is False)

        payload = {
            "source_id": self.source_id,
            "page_url": PAGE_URL,
            "app_id": app_id,
            "dashboard_url": DASHBOARD_URL.format(app_id=app_id),
            "tracker_pdf_url": tracker_pdf,
            "primacy_states": list(PRIMACY_STATES),
            "anonymous_access": anonymous,
            "field_count": len(available),
            "fields": list(FIELDS),
            "rows": rows,
        }
        content = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        return RawSnapshot(
            content=content,
            content_type="application/json",
            url=DASHBOARD_URL.format(app_id=app_id),
            retrieved_at=dt.datetime.now(dt.UTC),
            http_status=200,
            ext="json",
            headers=dict(page.headers),
            elapsed_s=round(time.monotonic() - t0, 2),
            requests_made=1 + messages,
            meta={
                "page_url": PAGE_URL,
                "app_id": app_id,
                "tracker_pdf_url": tracker_pdf,
                "engine_url": ENGINE_URL.format(app_id=app_id),
                "anonymous_access": anonymous,
                "rows_returned": len(rows),
            },
        )

    def parse(self, raw: RawSnapshot) -> list[dict[str, Any]]:
        try:
            payload = json.loads(raw.content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ParseError(f"{raw.url} is not the Class VI JSON payload: {exc}") from exc
        rows = payload.get("rows")
        if not isinstance(rows, list):
            raise ParseError(f"Class VI payload has no rows list: {sorted(payload)[:8]}")
        if rows and "Project Name" not in rows[0]:
            raise ParseError(f"Class VI table layout changed: {sorted(rows[0])[:8]}")

        out: list[dict[str, Any]] = []
        for row in rows:
            # The app's table is a spreadsheet export: the tail is blank padding rows.
            if not (row.get("Project Name") or "").strip():
                continue
            project = {f: row.get(f) for f in CORE_FIELDS + MILESTONE_FIELDS}
            project["nods"] = _ladder(row, "NOD", NOD_ROUNDS)
            project["rais"] = _ladder(row, "RAI", RAI_ROUNDS)
            project["dashboard_url"] = payload.get("dashboard_url")
            out.append(project)
        if not out:
            raise ParseError(f"Class VI payload carried {len(rows)} rows but no project rows")
        return out

    def normalize(self, rows: list[dict[str, Any]], raw: RawSnapshot) -> pd.DataFrame:
        def g(key: str) -> list[Any]:
            return [r.get(key) for r in rows]

        harmonised = [
            harmonise_status(self.status_key, {"status_raw": p}, self.status_map) for p in g("Phase")
        ]
        df = pd.DataFrame(
            {
                "source_record_id": [_record_id(r) for r in rows],
                "source_url": [u or PAGE_URL for u in g("URL - Vlookup")],
                "kind": "ccs",
                "name_canonical": g("Project Name"),
                "name_norm": [norm_name(v) for v in g("Project Name")],
                "sponsor_name": g("Company Name"),
                "sponsor_norm": [norm_org(v) for v in g("Company Name")],
                "technology": "co2_geologic_sequestration",
                "technology_raw": None,
                "capacity_mw": pd.array([None] * len(rows), dtype="Float64"),
                "storage_mwh": pd.array([None] * len(rows), dtype="Float64"),
                "iso": None,
                "state": [norm_state(v) for v in g("State")],
                "county": g("County/Parish/Tribe"),
                "county_norm": [norm_county(v) for v in g("County/Parish/Tribe")],
                "lifecycle_state": [s for s, _ in harmonised],
                "status_raw": g("Phase"),
                "status_rule": [r for _, r in harmonised],
                "status_conflict": False,
                "queue_date": [to_date(v) for v in g("Application Received Date")],
                "proposed_cod": pd.NaT,
                "queue_id": g("GSDT Project ID"),
                "eia_plant_id": None,
                "eia_generator_id": None,
                "cross_refs": [_cross_refs(r) for r in rows],
            }
        )
        return self.finalize(df, rows, raw)


def _ladder(row: dict[str, Any], prefix: str, rounds: int) -> list[dict[str, Any]]:
    """The NOD/RAI rounds a project actually has, as `[{round, sent, response_received}, …]`.

    A round with neither a sent nor a received date is dropped: the app carries the full 4/11
    columns for every project and most are empty (`RAI 11 Response Rec'd` was empty for all 68
    projects on 2026-09-22).
    """
    out: list[dict[str, Any]] = []
    for n in range(1, rounds + 1):
        sent = row.get(f"{prefix} {n} Sent")
        received = row.get(f"{prefix} {n} Response Rec'd")
        if sent or received:
            out.append({"round": n, "sent": sent, "response_received": received})
    return out


def _record_id(row: dict[str, Any]) -> str:
    """EPA's own GSDT project id when present, else a hash over the identifying columns."""
    gsdt = (row.get("GSDT Project ID") or "").strip()
    if gsdt:
        return gsdt
    return content_hash(
        row.get("Company Name"), row.get("Project Name"), row.get("State"), row.get("County/Parish/Tribe")
    )


def _cross_refs(row: dict[str, Any]) -> str:
    """`GSDT:<id>` when EPA carries one — the key that joins this row to a GSDT submission."""
    gsdt = (row.get("GSDT Project ID") or "").strip()
    return f"GSDT:{gsdt.upper()}" if gsdt else ""
