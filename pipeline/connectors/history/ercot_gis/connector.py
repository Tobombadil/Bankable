"""us.iso.ercot.gen_queue.history — ERCOT's monthly Generation Interconnection Status (GIS) reports,
one document per report month (`docs/24-trends.md` §9).

**The archive.** The public MIS document list for the GIS report (`IceDocListJsonWS?
reportTypeId=15933`, the route `us.iso.ercot.gen_queue` reads) keeps every monthly report from
December 2018 on (173 documents on 2026-10-23, the co-located battery reports among them). Where
ERCOT re-issued a month (`_revised`, `_Correction`, `_CORRECTION2`, `_Corrected`), the latest
published document for that month is the month; a correction published after its month was read
replaces the month's document. A report already stored by `us.iso.ercot.gen_queue` (same DocID)
is read from the snapshot store instead of downloaded again.

**What a month holds.** The report's project-detail rows: "only those projects for which a Full
Interconnection Study has been requested are included" (the report's own note, every month read).
A document tallies them (projects counted, MW summed as listed) by technology, status, study
phase, CDR reporting zone and projected COD year. MW is as ERCOT lists it: a repowering is on "a
net change basis ... and thus may have zero or negative values" (the same note).

- *Technology* is read from ERCOT's fuel code first (`FUEL_TECHNOLOGY`): coal, biomass, fuel oil,
  petcoke, geothermal, hydrogen and nuclear name the plant whatever its prime mover, and the
  register's classifier, given gridstatus's `Fuel - Technology` label, reads a coal or biomass steam
  turbine as `gas_steam` and an oil combustion turbine as `gas_ct` (its prime-mover rules come
  first). Every other fuel (gas, solar, wind, water, other) is gridstatus's label
  (`pipeline/vendor/gridstatus/queues.py`) classified by `pipeline.normalize.classify_tech`, the
  register's reading. A code outside ERCOT's own list (the report's Acronyms sheet) is counted and
  warned (`unlisted_codes`).
- *Status* is the register's lifecycle rule (`pipeline/status_map.yaml` `ercot`), from ERCOT's own
  milestone dates, latest first: synchronised `built`, energised `under_construction`, IA signed
  `contracted`, none `studied`. ERCOT's `1-1-1900` ("an actual date ... is not available") counts as
  reached, as the register reads it.
- *Phase* is ERCOT's own "GINR/GIM Study Phase" label, normalised to a token.

**Like for like** (`documents.py`). Until the report split its details into "Project Details -
Large Gen" and "Project Details - Small Gen", one "Project Details" sheet listed every size; the
small-generator sheet since has its own inclusion rule (projects whose owners authorised
publication, or model-ready). So the `projects` block's basis names the sheet layout
(`details-all-sizes` or `details-large-gen`), small generators are their own block, and the
report's note excluding inactive projects is in the basis where the report carries it. No change
is computed across a basis change.

source_url: the report's MIS download (`mirDownload?doclookupId=<DocID>`), the file itself.
Reuse: open (Website User Agreement clause 5) — raw-ok. Rate: 0.5 rps (clause 6). The MIS servlets
are read with the robots override, as the queue and price connectors read them (docs/29 §9.1).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import re
from collections.abc import Iterator, Mapping, Sequence
from typing import Any, ClassVar

from pipeline.connectors.base import ConnectorError, ParseError
from pipeline.connectors.history.base import (
    BLANK_LABEL,
    XLSX_MAGIC,
    ArchiveItem,
    Block,
    HeldPeriod,
    HistoryConnector,
    MonthFile,
    cell,
    column,
    find_header,
    normalise_header,
    number,
    open_workbook,
    stored_snapshots,
    text,
    year_of,
)
from pipeline.connectors.history.documents import month_number, period_of
from pipeline.normalize import classify_tech
from pipeline.vendor.gridstatus.queues import _ERCOT_FUEL, _ERCOT_TECHNOLOGY

log = logging.getLogger(__name__)

SOURCE_ID = "us.iso.ercot.gen_queue.history"
SHARED_SOURCE_ID = "us.iso.ercot.gen_queue"
DOC_LIST = "https://www.ercot.com/misapp/servlets/IceDocListJsonWS?reportTypeId=15933"
DOWNLOAD = "https://www.ercot.com/misdownload/servlets/mirDownload?doclookupId={doc_id}"
REPORT_NAME = re.compile(r"GIS_Report_([A-Za-z]+)_?(\d{4})(?:_([A-Za-z0-9_]+))?\.xlsx$", re.I)
AS_OF = re.compile(r"as of\s+([A-Za-z]+)\.?\s+(\d{1,2}),?\s+(\d{4})", re.I)
INR = re.compile(r"^\d{2}INR\d{4}", re.I)
#: The report's inclusion notes, as worded across the archive ("a Full Interconnection Study has
#: been requested" in 2018, "a Full Interconnection Study (FIS) has been requested" in April 2019).
FIS_NOTE = re.compile(r"full interconnection study (\(fis\) )?has been requested")
INACTIVE_NOTE = re.compile(r"excludes projects that have a status of inactive")
SINGLE = "project details"
LARGE = "project details - large gen"
SMALL = "project details - small gen"
#: Text in a milestone cell that does not mean the milestone was reached.
NOT_REACHED = frozenset({"", "n/a", "na", "no", "none", "-", "tbd"})
BASIS_ROOT = "ercot-gis"
MEASURE = "capacity-mw"
#: ERCOT's fuel and technology codes as its Acronyms sheet lists them (every report read, December
#: 2018 to September 2026). A code outside these is read (below) and warned.
LISTED_FUELS = frozenset({"BIO", "COA", "GAS", "GEO", "HYD", "NUC", "OIL", "OTH", "PET", "SOL", "WAT", "WIN"})
LISTED_TECHNOLOGIES = frozenset(
    {"BA", "CC", "CE", "CP", "EN", "FC", "GT", "HY", "IC", "OT", "PV", "ST", "WT"}
)
#: Fuels that name the technology whatever the prime mover (module docstring). `COAL` (2020-03),
#: `BAT` (2024-10, 2024-11, 2025-07) and `MWH` (2025-09 to 2026-09) are not on ERCOT's list; every
#: `BAT` row is a battery, and every `MWH` row a project named "... Storage" (Montezuma Storage,
#: Falls County Storage, Kalina Storage, Mistflower Energy Center Storage, Lynx Energy Storage), so
#: both read as storage. Petcoke is petroleum, as EIA groups it and as the classifier reads EIA's
#: "Petroleum Coke".
FUEL_TECHNOLOGY: dict[str, str] = {
    "BAT": "storage",
    "BIO": "biomass",
    "COA": "coal",
    "COAL": "coal",
    "GEO": "geothermal",
    "HYD": "hydrogen",
    "MWH": "storage",
    "NUC": "nuclear",
    "OIL": "oil",
    "PET": "oil",
}
#: A fuel and technology pair the classifier misreads: concentrated solar power is solar thermal
#: (EIA's "Solar Thermal" generators read `solar_thermal` too); the label reads `solar`.
PAIR_TECHNOLOGY: dict[tuple[str, str], str] = {("SOL", "CP"): "solar_thermal"}


def report_period(name: str) -> tuple[str, str | None] | None:
    """`(YYYY-MM, variant)` of a GIS report name (`..GIS_Report_June_2020_Correction.xlsx`); None
    for any other document (the co-located battery reports)."""
    match = REPORT_NAME.search(name)
    if match is None:
        return None
    month = month_number(match.group(1))
    if month is None:
        return None
    return period_of(int(match.group(2)), month), (match.group(3) or None)


def gis_documents(listing: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """The month's document for every month the list holds: the latest published GIS report of
    that month (a correction supersedes what it corrects)."""
    try:
        docs = [d["Document"] for d in listing["ListDocsByRptTypeRes"]["DocumentList"]]
    except (KeyError, TypeError) as exc:
        raise ParseError(f"ERCOT document list unreadable: {exc!r}") from exc
    out: dict[str, dict[str, Any]] = {}
    for doc in docs:
        name = str(doc.get("ConstructedName") or "")
        if str(doc.get("Extension") or "").lower() != "xlsx" or "GIS_Report" not in name:
            continue
        found = report_period(name)
        if found is None:
            continue
        period, variant = found
        entry = {
            "period": period,
            "variant": variant,
            "doc_id": str(doc.get("DocID")),
            "name": name,
            "friendly_name": doc.get("FriendlyName"),
            "publish_date": str(doc.get("PublishDate") or ""),
            "bytes": doc.get("ContentSize"),
        }
        current = out.get(period)
        if current is None or _published(entry) > _published(current):
            out[period] = entry
    return dict(sorted(out.items()))


_EPOCH = dt.datetime(1970, 1, 1, tzinfo=dt.UTC)


def _published(entry: Mapping[str, Any]) -> tuple[dt.datetime, str]:
    """A listing entry's publication time as an instant: ERCOT writes `PublishDate` in Central
    time with its offset (`-05:00` in CDT, `-06:00` in CST), so two strings of one day compare
    wrongly as text. An unreadable date sorts first; the DocID breaks a tie."""
    when = _time(str(entry.get("publish_date") or "")) or _EPOCH
    return when, str(entry.get("doc_id") or "").zfill(20)


def reached(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, (dt.datetime, dt.date, int, float)) and not isinstance(value, bool):
        return True
    return str(value).strip().lower() not in NOT_REACHED


def lifecycle(sync: Any, energised: Any, ia: Any) -> str:
    """The register's ERCOT lifecycle rule (`pipeline/status_map.yaml` `ercot`), latest milestone first."""
    if reached(sync):
        return "built"
    if reached(energised):
        return "under_construction"
    if reached(ia):
        return "contracted"
    return "studied"


def phase_token(value: Any) -> str | None:
    label = text(value)
    if label is None:
        return None
    return re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_") or None


def technology(fuel: Any, tech: Any) -> str:
    """The technology of an ERCOT row (module docstring): the fuel where it names the plant
    (`FUEL_TECHNOLOGY`), a pair the classifier misreads (`PAIR_TECHNOLOGY`), else gridstatus's
    `Fuel - Technology` label classified as the register classifies it."""
    f = (text(fuel) or "").upper()
    t = (text(tech) or "").upper()
    if not f and not t:
        return "unknown"
    if (f, t) in PAIR_TECHNOLOGY:
        return PAIR_TECHNOLOGY[(f, t)]
    if f in FUEL_TECHNOLOGY:
        return FUEL_TECHNOLOGY[f]
    label = f"{_ERCOT_FUEL.get(f, f)} - {_ERCOT_TECHNOLOGY.get(t, t)}"
    return classify_tech(label)[0]


def unlisted_codes(fuel: Any, tech: Any) -> list[str]:
    """The codes of a row that ERCOT's own Acronyms sheet does not list (`fuel:COAL`, `tech:BL`)."""
    out = []
    f = (text(fuel) or "").upper()
    t = (text(tech) or "").upper()
    if f and f not in LISTED_FUELS:
        out.append(f"fuel:{f}")
    if t and t not in LISTED_TECHNOLOGIES:
        out.append(f"technology:{t}")
    return out


#: What a listing entry says about its report, carried on every route into the document.
LISTING_FIELDS = ("doc_id", "name", "publish_date", "variant", "friendly_name")


class Connector(HistoryConnector):
    source_id: ClassVar[str] = SOURCE_ID
    shared_source_id: ClassVar[str | None] = SHARED_SOURCE_ID
    parser_version: ClassVar[str] = "1.0.0"
    honour_robots: ClassVar[bool] = False  # MIS servlets are an API (docs/29 §9.1)

    # ---------------------------------------------------------------------------------- plan
    def plan(self, mode: str, held: Mapping[str, HeldPeriod]) -> list[ArchiveItem]:
        self._held = held
        self.plan_errors = []
        restates = {item.periods[0]: item for item in self.restate_items(held)}
        listing = self.http.get(DOC_LIST, honour_robots=False, timeout=60)
        if listing.status_code != 200:
            raise ConnectorError(f"GET {DOC_LIST} -> HTTP {listing.status_code}")
        try:
            months = gis_documents(json.loads(listing.content.decode("utf-8")))
        except ValueError as exc:
            raise ParseError(f"ERCOT document list is not JSON: {exc!r}") from exc
        self.listing = months
        shared = {
            str(snap.meta.get("doc_id")): snap
            for snap in stored_snapshots(self.store, SHARED_SOURCE_ID)
            if snap.meta.get("doc_id")
        }
        items: list[ArchiveItem] = []
        for period, doc in months.items():
            h = held.get(period)
            if h is not None and h.file_id == doc["doc_id"]:
                continue  # the same report; a parser change is a restatement (below)
            if h is not None and h.pending_file_id == doc["doc_id"]:
                continue  # this report is held beside the month's document until it is released
            restates.pop(period, None)
            meta = {k: doc[k] for k in LISTING_FIELDS}
            snap = shared.get(doc["doc_id"])
            if snap is not None:
                items.append(
                    ArchiveItem(
                        key=f"shared:{doc['doc_id']}",
                        route="shared",
                        url=snap.key,
                        periods=(period,),
                        meta={**meta, "sha256": snap.sha256, "retrieved_at": snap.retrieved_at},
                    )
                )
            else:
                items.append(
                    ArchiveItem(
                        key=f"doc:{doc['doc_id']}",
                        route="origin",
                        url=DOWNLOAD.format(doc_id=doc["doc_id"]),
                        periods=(period,),
                        meta=meta,
                    )
                )
        return [*restates.values(), *items]

    # --------------------------------------------------------------------------------- fetch
    def fetch(self, item: ArchiveItem) -> Iterator[MonthFile]:
        period = item.periods[0] if item.periods else None
        if item.route == "restate":
            held = self._held.get(period) if period else None
            # What the listing says about the report this month holds (its name, publication date,
            # variant), so a restated document keeps them: the bytes alone do not say.
            listed = (getattr(self, "listing", {}) or {}).get(period) if period else None
            known = (
                {k: listed[k] for k in LISTING_FIELDS}
                if held and listed and listed.get("doc_id") == held.file_id
                else {"doc_id": held.file_id if held else None}
            )
            try:
                month = self.fetch_restated(
                    item,
                    source_url=held.source_url if held else "",
                    retrieved_at=held.retrieved_at if held else self.now().isoformat(),
                )
            except ConnectorError:
                if not (held and held.file_id):
                    raise
                # Neither the stored copy nor another with its sha256 is left: the same report again
                # from the MIS, by its DocID (it is still listed), read as an ordinary download.
                yield from self.fetch(
                    ArchiveItem(
                        key=f"doc:{held.file_id}",
                        route="origin",
                        url=DOWNLOAD.format(doc_id=held.file_id),
                        periods=(period,) if period else (),
                        meta=known,
                    )
                )
                return
            month.via.update(known)
            yield month
            return
        via = {k: item.meta.get(k) for k in LISTING_FIELDS}
        source_url = DOWNLOAD.format(doc_id=item.meta.get("doc_id"))
        if item.route == "shared":
            content = self.store.backend.get(item.url)
            if hashlib.sha256(content).hexdigest() != item.meta.get("sha256"):
                raise ConnectorError(f"stored {SHARED_SOURCE_ID} snapshot {item.url} fails its sha256")
            retrieved = _time(str(item.meta.get("retrieved_at") or "")) or self.now()
            yield MonthFile(
                content=content,
                source_url=source_url,
                retrieved_at=retrieved,
                route="shared",
                item_key=item.key,
                period_hint=period,
                snapshot_key=item.url,
                via={**via, "source_id": SHARED_SOURCE_ID},
            )
            return
        if item.route != "origin":
            raise ConnectorError(f"unknown route {item.route!r}")
        r = self.http.get(item.url, honour_robots=False, timeout=180)
        content = bytes(r.content)
        if r.status_code != 200:
            raise ConnectorError(f"GET {item.url} -> HTTP {r.status_code}")
        if not content.startswith(XLSX_MAGIC):
            raise ParseError(f"{item.url} answered something other than an xlsx")
        yield MonthFile(
            content=content,
            source_url=source_url,
            retrieved_at=self.now(),
            route="origin",
            item_key=item.key,
            period_hint=period,
            via=via,
        )

    # ----------------------------------------------------------------------------- summarise
    def summarise(self, month: MonthFile) -> dict[str, Any]:
        if month.period_hint is None:
            raise ParseError("a GIS report's month comes from its name; none was given")
        period = month.period_hint
        wb = open_workbook(month.content)
        try:
            titles = [ws.title for ws in wb.worksheets]
            by_name = {ws.title.strip().lower(): ws for ws in wb.worksheets}
            if LARGE in by_name:
                sheets = {"projects": (by_name[LARGE], "details-large-gen")}
                if SMALL in by_name:
                    sheets["small_generators"] = (by_name[SMALL], "details-small-gen")
                arrangement = "split"
            elif SINGLE in by_name:
                sheets = {"projects": (by_name[SINGLE], "details-all-sizes")}
                arrangement = "single"
            else:
                raise ParseError(f"no project details sheet in {titles}")
            as_of = _summary_as_of(by_name.get("summary"))
            blocks: dict[str, Block] = {}
            dq: dict[str, Any] = {"rows": {}, "duplicate_keys": {}, "capacity_missing": {}}
            layout: dict[str, Any] = {
                "sheets": titles,
                "arrangement": arrangement,
                "header_rows": {},
                "notes": {},
            }
            labels: dict[str, str] = {}
            unlisted: dict[str, int] = {}
            for block_name, (ws, sheet_basis) in sheets.items():
                rows = ws.iter_rows(values_only=True)
                header_index, header, before = find_header(rows, ("INR", "Capacity (MW)"))
                notes = " ".join(normalise_header(v) for row in before for v in row if v).lower()
                flags = []
                if FIS_NOTE.search(notes):
                    flags.append("fis-requested")
                if INACTIVE_NOTE.search(notes):
                    flags.append("excl-inactive")
                layout["header_rows"][block_name] = header_index
                layout["notes"][block_name] = flags
                basis = "/".join([BASIS_ROOT, sheet_basis, MEASURE, *flags])
                block, summary = self._tally(block_name, basis, header, rows)
                blocks[block_name] = block
                dq["rows"][block_name] = summary["rows"]
                dq["duplicate_keys"][block_name] = summary["duplicates"]
                dq["capacity_missing"][block_name] = summary["capacity_missing"]
                labels.update(summary["labels"])
                for code, count in summary["unlisted"].items():
                    unlisted[code] = unlisted.get(code, 0) + count
        finally:
            wb.close()
        layout["as_of"] = as_of
        if as_of and as_of[:7] != period:
            # The month is the name's, so a name the report's own date disputes holds the month
            # (`dq.py` `period_mismatch`): a report posted under another month's name is never served.
            dq["period_hint_mismatch"] = {
                "report_name": period,
                "summary_as_of": as_of,
                "period_from": "name",
            }
        dq["unknown_status"] = {}
        dq["unlisted_codes"] = dict(sorted(unlisted.items()))
        dq["unmapped_technology"] = sorted(label for label, bucket in labels.items() if bucket == "unknown")
        return {
            "period": period,
            "source_url": month.source_url,
            "file_id": month.via.get("doc_id"),
            "published": month.via.get("publish_date"),
            "layout": layout,
            "blocks": {name: block.as_dict() for name, block in sorted(blocks.items())},
            "technology_labels": dict(sorted(labels.items())),
            "dq_facts": dq,
        }

    def _tally(
        self, block_name: str, basis: str, header: Sequence[str], rows: Iterator[Sequence[Any]]
    ) -> tuple[Block, dict[str, Any]]:
        idx = {
            "inr": column(header, "INR"),
            "phase": column(header, "GIM Study Phase", "GINR Study Phase"),
            "zone": column(header, "CDR Reporting Zone"),
            "cod": column(header, "Projected COD"),
            "fuel": column(header, "Fuel"),
            "technology": column(header, "Technology"),
            "mw": column(header, "Capacity (MW)"),
            "ia": column(header, "IA Signed"),
            "energised": column(header, "Approved for Energization"),
            "sync": column(header, "Approved for Synchronization"),
        }
        if idx["fuel"] is None or idx["technology"] is None or idx["mw"] is None:
            raise ParseError(f"{block_name} sheet lacks a column the tallies read: {list(header)[:20]}")
        dims: tuple[str, ...] = ("technology", "status", "phase", "zone", "cod_year")
        if block_name == "small_generators":
            dims = ("technology", "status", "zone", "cod_year")  # the small sheet has no study phase
        block = Block(basis=basis, dimensions=dims)
        seen: set[str] = set()
        summary: dict[str, Any] = {
            "rows": 0,
            "duplicates": 0,
            "capacity_missing": 0,
            "labels": {},
            "unlisted": {},
        }
        for row in rows:
            inr = text(cell(row, idx["inr"]))
            if inr is None or INR.match(inr) is None:
                continue  # sub-header and note rows carry no INR
            summary["rows"] += 1
            if inr.upper() in seen:
                summary["duplicates"] += 1
            seen.add(inr.upper())
            mw = number(cell(row, idx["mw"]))
            if mw is None:
                summary["capacity_missing"] += 1
            fuel, tech = cell(row, idx["fuel"]), cell(row, idx["technology"])
            token = technology(fuel, tech)
            summary["labels"][f"{text(fuel) or ''} {text(tech) or ''}".strip() or BLANK_LABEL] = token
            for code in unlisted_codes(fuel, tech):
                summary["unlisted"][code] = summary["unlisted"].get(code, 0) + 1
            cod = year_of(cell(row, idx["cod"]))
            keys: dict[str, str | None] = {
                "technology": token,
                "status": lifecycle(
                    cell(row, idx["sync"]), cell(row, idx["energised"]), cell(row, idx["ia"])
                ),
                "phase": phase_token(cell(row, idx["phase"])),
                "zone": (text(cell(row, idx["zone"])) or "").upper() or None,
                "cod_year": str(cod) if cod is not None else None,
            }
            block.add(keys, mw)
        return block, summary


def _summary_as_of(ws: Any) -> str | None:
    """The report's own date ("Currently tracking ... as of Sep 30, 2026") from its Summary sheet."""
    if ws is None:
        return None
    for index, row in enumerate(ws.iter_rows(values_only=True)):
        if index > 20:
            break
        for value in row:
            match = AS_OF.search(normalise_header(value))
            if match:
                month = month_number(match.group(1))
                if month:
                    try:
                        return dt.date(int(match.group(3)), month, int(match.group(2))).isoformat()
                    except ValueError:
                        return None
    return None


def _time(value: str) -> dt.datetime | None:
    try:
        when = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo is not None else when.replace(tzinfo=dt.UTC)


__all__ = [
    "DOC_LIST",
    "DOWNLOAD",
    "SHARED_SOURCE_ID",
    "SOURCE_ID",
    "Connector",
    "gis_documents",
    "lifecycle",
    "phase_token",
    "reached",
    "report_period",
    "technology",
    "unlisted_codes",
]
