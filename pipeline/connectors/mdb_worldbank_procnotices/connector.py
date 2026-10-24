"""mdb.worldbank.procnotices — World Bank Procurement Notices API, energy sectors.

Sector filter confirmed 2026-09-12: the notice document carries a nested `sector[]` with
`sector_code`/`sector_description`; the API filters on the dotted path
`sector.sector_code=<CODE>` with `^` as OR (`sector_exact=Energy`, `sector_code=`, comma and pipe
lists all return 0 or the unfiltered total). Energy codes used: LU solar, LH hydro, LW wind,
LB biomass, LI geothermal, LN non-renewable generation, LT transmission & distribution,
LZ other energy, LP public administration – energy, LE energy (33,950 notices on 2026-09-12).
Fetch: newest first (`srt=noticedate&order=desc`), 100 per page, until a page is older than the
window or the page cap is hit; `fl=` names the fields so contact_* personal data is never
requested (docs/13 §5.4). Window source -> `snapshot_mode = incremental`. The window starts at
the day before the last promoted run (`Connector.fetch_window`, 2026-10-07; audit 2026-09-30 F3),
14 days on a first run; a fetch that stops at its page cap before reaching the window start is
marked `truncated` and held. Row counts are compared on like-for-like weekdays by `open_at`
(the notice date; audit F6).
source_record_id: the notice `id` (`OP00468319`).
Reuse: CC BY 4.0 (World Bank Open Data), credit required.
"""

from __future__ import annotations

import json
import pathlib
import re
import time
from typing import Any, ClassVar

import pandas as pd

from pipeline.connectors.base import Connector as BaseConnector
from pipeline.connectors.base import ConnectorError, Kind, ParseError, RawSnapshot, SnapshotMode
from pipeline.connectors.canonical import harmonise_status
from pipeline.connectors.opportunity import (
    GRID_TECH,
    StatedFigure,
    StatedValue,
    classify_technologies,
    currency_code,
    deadline_passed,
    iso2,
    not_grid_context,
    parse_amount,
    stated_value_of,
    technologies_str,
    to_utc,
)

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
#: A sector code belongs to the World Bank *project*, not the notice, and so does the project name
#: (which this connector no longer classifies): in the public register of 2026-10-23, 81 of the 110
#: stored notices carried a technology although their own title names none ("Social Specialist",
#: "Microsoft 365 E3 Office (150 licenses)", "Adquisición de Útiles de oficina" as transmission;
#: a hydropower package as solar because the project also had a solar sector). So a sector adds its
#: technology only when the notice's own words point at it: these cues, or the keyword rules
#: (`classify_technologies`). For the grid, the weaker words of a transmission-and-distribution
#: project ("line installation contractors", "33KV/0.4 distribution network", "ABC", "village
#: electrification", "branchements") count (docs/28 B-6).
SECTOR_CUES: dict[str, re.Pattern[str]] = {
    "LU": re.compile(r"solar|solaire|photovolta|\bpv\b|\bshs\b", re.I),
    "LH": re.compile(r"hydro|\bs?hpp\b|\bdams?\b|barrage|hidroel", re.I),
    "LW": re.compile(r"\bwind|éolien|eólic", re.I),
    "LB": re.compile(r"biomas|biogas|biogaz|cookstove|foyers\s+améliorés|charcoal|bois[\s-]énergie", re.I),
    "LI": re.compile(r"geotherm|géotherm|geotérm", re.I),
    "LN": re.compile(r"thermal|thermique|diesel|\bhfo\b|gas[\s-]fired|power\s+plant|centrale", re.I),
    "LT": re.compile(
        r"\blines?\b|\bcables?\b|\bpoles?\b|\btowers?\b|electrifica\w*|électrifica\w*|branchements?|raccordement"
        r"|\bnetworks?\b|r[ée]seaux?|\bredes?\b|distribution\s+(?:networks?|lines?|grids?|systems?|substations?)"
        r"|\bconnections?\b|substation|\bkv\b|\d\s*kv\b|\babc\b",
        re.I,
    ),
}
#: World Bank country codes that are not ISO 3166-1 (docs/28 B-17). Read from the API's own
#: `project_ctry_name` on 2026-10-24 (`project_ctry_code=<code>`, one row each): ZR "Congo,
#: Democratic Republic of", TP "Timor-Leste", GZ "West Bank and Gaza", RY "Yemen, Republic of".
WB_COUNTRY_CODES: dict[str, str] = {"ZR": "CD", "TP": "TL", "GZ": "PS", "RY": "YE"}
#: World Bank region codes (a digit, then a letter) -> a region token, outside any country list.
#: Names are the API's `project_ctry_name` for each code, read the same day (`REGION_NAMES`).
WB_REGION_CODES: dict[str, str] = {
    "1W": "region:world",
    "3A": "region:africa",
    "3E": "region:eastern_southern_africa",
    "3S": "region:southern_africa",
    "3W": "region:western_central_africa",
    "4E": "region:east_asia_pacific",
    "4P": "region:pacific_islands",
    "5M": "region:middle_east_north_africa_afghanistan_pakistan",
    "6A": "region:andean_countries",
    "6C": "region:central_america",
    "6L": "region:latin_america_caribbean",
    "6R": "region:caribbean",
    "7B": "region:western_balkans",
    "7E": "region:europe_central_asia",
    "8S": "region:south_asia",
}
#: What each region token names, in the World Bank's words (for the labels lane).
REGION_NAMES: dict[str, str] = {
    "region:world": "World",
    "region:africa": "Africa",
    "region:eastern_southern_africa": "Eastern and Southern Africa",
    "region:southern_africa": "Southern Africa",
    "region:western_central_africa": "Western and Central Africa",
    "region:east_asia_pacific": "East Asia and Pacific",
    "region:pacific_islands": "Pacific Islands",
    "region:middle_east_north_africa_afghanistan_pakistan": (
        "Middle East, North Africa, Afghanistan and Pakistan"
    ),
    "region:andean_countries": "Andean Countries",
    "region:central_america": "Central America",
    "region:latin_america_caribbean": "Latin America and Caribbean",
    "region:caribbean": "Caribbean",
    "region:western_balkans": "Western Balkans",
    "region:europe_central_asia": "Europe and Central Asia",
    "region:south_asia": "South Asia",
}
_REGION_CODE = re.compile(r"^\d[A-Z]$")


def jurisdiction(code: Any) -> str:
    """The notice's jurisdiction from `project_ctry_code`: ISO 3166-1 alpha-2 for a country (the
    Bank's own codes mapped, `WB_COUNTRY_CODES`), a `region:` token for a regional project
    (`WB_REGION_CODES`; one the table does not name is `region:wb_<code>`), `global` when the
    notice states none."""
    text = str(code or "").strip().upper()
    if not text:
        return "global"
    if text in WB_REGION_CODES:
        return WB_REGION_CODES[text]
    if _REGION_CODE.match(text):
        return f"region:wb_{text.lower()}"
    if text in WB_COUNTRY_CODES:
        return WB_COUNTRY_CODES[text]
    return iso2(text) or "global"


def technologies(title: str | None, sectors: list[Any]) -> list[str]:
    """The notice's own technology words (`classify_technologies` over its title), then each of its
    project's sector technologies the title points at (`SECTOR_CUES`)."""
    techs = classify_technologies(title)
    for code in sectors:
        tok = SECTOR_TECH.get(str(code))
        cue = SECTOR_CUES.get(str(code))
        if not tok or tok in techs or cue is None or not cue.search(title or ""):
            continue
        if tok == GRID_TECH and not_grid_context(title):
            continue
        techs.append(tok)
    return techs


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


def stated_value(row: dict[str, Any]) -> StatedValue | None:
    """`bid_estimate_amount` in `bid_currency_code` (lane rfp-value). The field is the package's
    estimate on every notice type, a contract award included, so it is labelled an estimate there
    too: the award amount is not a field this connector reads."""
    amount = parse_amount(row.get("bid_estimate_amount"))
    if amount is None:
        return None
    figure = StatedFigure(
        "estimated_value", amount, currency_code(row.get("bid_currency_code")), "bid_estimate_amount"
    )
    return stated_value_of([figure])


class Connector(BaseConnector):
    source_id: ClassVar[str] = "mdb.worldbank.procnotices"
    kind: ClassVar[Kind] = "opportunity"
    ext: ClassVar[str] = "json"
    honour_robots: ClassVar[bool] = False
    snapshot_mode: ClassVar[SnapshotMode] = "incremental"
    window_days: ClassVar[int] = WINDOW_DAYS
    window_date_column: ClassVar[str | None] = "open_at"
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
        now = self.now()
        window = self.fetch_window(now)
        cutoff = window.start
        cap = self.page_cap(MAX_PAGES, window)
        pages: list[dict[str, Any]] = []
        truncated = False
        for page_no in range(cap):
            r = self.http.get(API_URL, honour_robots=False, params=self.params(page_no * PAGE), timeout=120)
            if r.status_code != 200:
                raise ConnectorError(f"GET {API_URL} -> HTTP {r.status_code}")
            page = r.json()
            pages.append(page)
            notices = _notices(page)
            if not notices:
                break
            dates = [d for d in (to_utc(n.get("noticedate"), "%d-%b-%Y") for n in notices) if d is not None]
            oldest = min(dates) if dates else None
            if oldest is not None and oldest < pd.Timestamp(cutoff):
                break
            if len(notices) < PAGE:
                break
        else:
            truncated = True
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
                "page_cap": cap,
                "truncated": truncated,
                **window.meta(),
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
            techs = technologies(title, sectors)
            value = stated_value(r)
            identifiers: dict[str, Any] = {
                "wb_notice_id": r.get("id"),
                "wb_project_id": r.get("project_id"),
                "bid_reference_no": r.get("bid_reference_no"),
                "sector_codes": sectors,
            }
            if value is not None:
                identifiers["value"] = value.identifiers()
            recs.append(
                {
                    "source_record_id": str(r.get("id")),
                    "source_url": NOTICE_URL.format(notice_id=r.get("id")),
                    "kind": KIND_BY_TYPE.get(ntype, "tender"),
                    "issuer": r.get("agency_name") or r.get("contact_organization"),
                    "title": title,
                    "summary": None,
                    "jurisdiction": jurisdiction(r.get("project_ctry_code")),
                    "technologies": technologies_str(techs),
                    "capacity_sought_mw": None,
                    "budget_amount": value.amount if value else None,
                    # The notice's own code as before when it states no amount (a currency alone
                    # is not a value, but the column always carried it).
                    "budget_currency": value.currency if value else r.get("bid_currency_code"),
                    "open_at": to_utc(r.get("noticedate"), "%d-%b-%Y"),
                    "due_at": due,
                    "status": state,
                    "status_raw": ntype,
                    "status_rule": rule,
                    "identifiers": json.dumps(identifiers),
                }
            )
        df = pd.DataFrame(recs, columns=list(recs[0]) if recs else ["source_record_id"])
        return self.finalize(df, rows, raw)


def _notices(page: dict[str, Any]) -> list[dict[str, Any]]:
    pn = page.get("procnotices")
    if isinstance(pn, dict):
        return [v for v in pn.values() if isinstance(v, dict)]
    return list(pn or [])
