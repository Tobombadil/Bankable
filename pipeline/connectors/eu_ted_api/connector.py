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
    GRID_TECH,
    StatedFigure,
    StatedValue,
    classify_technologies,
    currency_code,
    deadline_passed,
    grid_evidence,
    iso2,
    not_grid_context,
    parse_amount,
    stated_value_of,
    technologies_str,
    ted_buyer_title,
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
#: CPV code prefixes -> technology. Every code checked against CPV 2008 as the Publications Office
#: publishes it (eForms SDK `codelists/cpv.gc`, read 2026-10-24). Fixed then (docs/28 B-3, B-5):
#: `093` caught 09310000 Electricity and 09344000 Radio-isotopes as solar; `09111` caught 09111400
#: Wood fuels as coal. And three codes named the wrong thing: 45251100 is any power plant (wind is
#: 45251160), 45251143 a compressed-air plant (not nuclear), and `31121` generating sets, wind
#: turbines (311213) among them (not gas). A code takes the token of its longest listed prefix
#: (`cpv_technologies`), so 09331100 is solar heat, not PV.
CPV_TECH = {
    "0933": "solar_pv",  # solar energy, panels, PV modules, solar installation
    "09331100": "solar_thermal",  # solar collectors for heat production (review of 2026-10-24)
    "0932": "heat",  # steam, hot water, district and long-distance heating (review of 2026-10-24)
    "31712331": "solar_pv",  # photovoltaic cells
    "45261215": "solar_pv",  # solar panel roof-covering work
    "45251160": "wind",  # wind-power installation works
    "311213": "wind",  # wind-energy generators, windmills, wind turbines, wind farm
    "45251120": "hydro",  # hydro-electric plant construction work
    "421122": "hydro",  # hydraulic turbines, water wheels
    "421132": "hydro",  # parts of hydraulic turbines
    "45251110": "nuclear",  # nuclear-power station construction work
    "45251111": "nuclear",  # construction work for nuclear reactors
    "4215": "nuclear",  # nuclear reactors and parts
    "09340000": "nuclear",  # nuclear fuels (not 09343/09344: radioactive materials, radio-isotopes)
    "09341": "nuclear",  # uranium
    "09342": "nuclear",  # plutonium
    "45251141": "geothermal",  # geothermal power station construction work
    "091114": "biomass",  # wood fuels
    "03413": "biomass",  # fuel wood
    "45251142": "biomass",  # wood-fired power station construction work
    "45251240": "biomass",  # landfill-gas electricity generating plant
    # The group code itself, "Coal and coal-based fuels" (exact, so its fossil-fuel and wood-fuel
    # children 09111300 and 09111400 stay out): a buyer citing the coal group buys coal, except
    # beside a wood-fuel code, where the group is that wood fuel's parent (`cpv_technologies`).
    # Read from the CPV list; it was checked against the labelled sample afterwards, so coal's
    # measured precision is in-sample (data/eval/opportunity_technology_tags.py).
    "09111000": "coal",
    "091111": "coal",  # coal
    "091112": "coal",  # coal-based fuels, hard coal, briquettes
    "091121": "coal",  # lignite
    "09120000": "gas",  # gaseous fuels
    "09121": "gas",  # coal gas, mains gas
    "09123": "gas",  # natural gas
    "4523122": "gas",  # gas pipelines, gas supply mains, gasholders, gas distribution
    "421123": "gas",  # gas turbines
    "421133": "gas",  # parts of gas turbines
    "31122100": "hydrogen",  # fuel cells
    "71314": "efficiency",  # energy and related services
}
#: Grid codes: the electricity network itself (docs/28 B-6). Substation equipment and works,
#: transformers, distribution transformers and systems, switchgear, medium- and high-voltage and
#: overhead equipment and cables, overhead line works.
GRID_CPV = (
    "31682540",
    "31170000",
    "31171",
    "31172",
    "31173",  # not 31174000, power-supply transformers for electronics
    "31213200",
    "31213400",
    "31214000",
    "31214110",
    "31214120",
    "31214200",
    "31214300",
    "31214400",
    "31214520",  # not 312141xx, the switches of a building (dimmer, toggle, limit switches)
    "31212100",
    "316823",
    "316824",
    "31321000",
    "31321100",
    "313212",  # low- and medium-voltage cable (31321200) and its children (review of 2026-10-24)
    "31321300",
    "45231400",
    "45232200",
    "45232210",
    "45232220",
    "45232221",
)
#: Construction of another kind: buildings, bridges and tunnels, roads, railways, airfields, sports
#: grounds, general, oil, gas, water and sewer pipelines, telecom lines, hydraulic works, power,
#: water and process plants, roofing, insulation, plumbing, fencing and building completion. A grid
#: code beside one of these is that project's own cabling or substation (docs/28 B-6 sample: cycle
#: paths, a railway, a school; the 2026-10-23 register: a sports hall, a water intake, flood
#: embankments), so it is not evidence of grid work; explicit grid words still are.
OTHER_WORKS_CPV = (
    "4521",
    "45221",
    "45222",
    "45233",
    "45234",
    "45235",
    "45236",
    "452311",
    "452312",
    "452313",
    "452321",
    "452323",
    "452324",
    "4524",
    "45251",
    "45252",
    "45253",
    "45255",
    "4526",
    "4532",
    "4533",
    "4534",
    "4535",
    "454",
)
#: Codes of commodity energy (fuels, electricity, heat, gas; utility services): a notice that has
#: only these buys energy, and the voltages it names are the supply's (docs/28 B-6).
ENERGY_COMMODITY_CPV = ("09", "65")
#: Fuels, petroleum products and lubricants: a notice that has only these buys a consumable, so a
#: grid word in its title is the consumable's ("transformer oil", "oils for ... transformers").
PETROLEUM_CPV = ("091", "092")
#: "Coal and coal-based fuels", the group that also holds wood fuels, and the wood-fuel codes.
COAL_GROUP_CPV = "09111000"
WOOD_FUEL_CPV = ("091114", "03413")


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


#: eForms forms whose `total-value` is what was awarded (BT-161 "Notice Value", which exists only
#: in a result notice) or the value of one contract awarded directly or modified; on every other
#: form (competition, planning, consultation) the only value a buyer can state is BT-27
#: "Estimated Value". TED's search API documents no field definitions (docs.ted.europa.eu
#: /api/latest/search.html, read 2026-10-22), so the kind follows the notice's own form type.
RESULT_FORMS = frozenset({"result"})
CONTRACT_FORMS = frozenset({"dir-awa-pre", "cont-modif"})


def stated_value(row: dict[str, Any]) -> StatedValue | None:
    """The notice's value as it states it (lane rfp-value): `total-value` labelled by the form
    type (above), and `estimated-value-glo` as an estimate. On a result or contract form the
    total comes first; otherwise the estimate does. Before 2026-10-22 the estimate was read only
    when the API returned it as a number, and it returns a one-element list of strings, so both
    notices of the 2026-10-20 store that stated one had no budget."""
    form = first_text(row.get("form-type")) or ""
    figures: list[StatedFigure] = []
    estimate = parse_amount(row.get("estimated-value-glo"))
    if estimate is not None:
        figures.append(
            StatedFigure(
                "estimated_value",
                estimate,
                currency_code(row.get("estimated-value-cur-glo")),
                "estimated-value-glo",
            )
        )
    total = parse_amount(row.get("total-value"))
    if total is not None:
        kind = (
            "awarded_value"
            if form in RESULT_FORMS
            else "contract_value"
            if form in CONTRACT_FORMS
            else "estimated_value"
        )
        figure = StatedFigure(kind, total, currency_code(row.get("total-value-cur")), "total-value")
        if form in RESULT_FORMS | CONTRACT_FORMS:
            figures.insert(0, figure)
        else:
            figures.append(figure)
    return stated_value_of(figures)


def cpv_technologies(codes: list[str]) -> list[str]:
    """Technology tokens the CPV codes name (`CPV_TECH`, each code by its longest listed prefix),
    in first-seen order, and the grid tag for a grid code (`GRID_CPV`) in a notice with no
    construction of another kind (`OTHER_WORKS_CPV`)."""
    out: list[str] = []
    other_works = any(c.startswith(OTHER_WORKS_CPV) for c in codes)
    wood = any(c.startswith(WOOD_FUEL_CPV) for c in codes)
    for c in codes:
        if c == COAL_GROUP_CPV and wood:
            continue  # the wood fuel's parent here, not coal
        prefixes = [p for p in CPV_TECH if c.startswith(p)]
        if prefixes:
            tok = CPV_TECH[max(prefixes, key=len)]
            if tok not in out:
                out.append(tok)
        if not other_works and c.startswith(GRID_CPV) and GRID_TECH not in out:
            out.append(GRID_TECH)
    return out


def technologies(title: Any, codes: list[str]) -> list[str]:
    """A notice's technology tokens: the buyer's own title (`ted_buyer_title`, never the CPV label
    TED puts in front of it, docs/28 B-4) and its CPV codes. Grid work needs grid words or a grid
    code (docs/28 B-6): a voltage in a notice that only buys energy is the supply's, and a grid code
    in a railway, road, lighting, fibre, building or charging notice is that project's cabling
    (`not_grid_context`)."""
    text = ted_buyer_title(title)
    techs = classify_technologies(text)
    buys_energy = bool(codes) and all(c.startswith(ENERGY_COMMODITY_CPV) for c in codes)
    buys_petroleum = bool(codes) and all(c.startswith(PETROLEUM_CPV) for c in codes)
    if GRID_TECH in techs and (
        buys_petroleum or (buys_energy and grid_evidence(text, buys_energy=True) is None)
    ):
        techs.remove(GRID_TECH)
    for t in cpv_technologies(codes):
        if t == GRID_TECH and (t in techs or not_grid_context(text)):
            continue
        if t not in techs:
            techs.append(t)
    return techs


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
            techs = technologies(title, cpv)
            value = stated_value(r)
            identifiers: dict[str, Any] = {
                "ted_notice_id": r.get("publication-number"),
                "cpv": sorted(set(cpv)),
                "procedure_type": r.get("procedure-type"),
                "contract_nature": sorted(set(r.get("contract-nature") or [])),
            }
            if value is not None:
                identifiers["value"] = value.identifiers()
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
                    "budget_amount": value.amount if value else None,
                    # The notice's own code, as before, when it states no amount.
                    "budget_currency": value.currency
                    if value
                    else first_text(r.get("estimated-value-cur-glo") or r.get("total-value-cur")),
                    "open_at": to_utc(str(r.get("publication-date") or "")[:10]),
                    "due_at": due,
                    "status": state,
                    "status_raw": ntype,
                    "status_rule": rule,
                    "identifiers": json.dumps(identifiers),
                }
            )
        df = pd.DataFrame(recs, columns=list(recs[0]) if recs else ["source_record_id"])
        return self.finalize(df, rows, raw)
