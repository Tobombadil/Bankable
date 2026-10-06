"""us.eia.860m.retirements -- EIA-860M generator inventory, "Operating" and "Retired" sheets.

The same monthly workbook `us.eia.860m` fetches for its "Planned" sheet (lane R1, owner request
2026-10-06: retired and retiring plants as a powered-land signal). A second source id rather than a
second sheet on that connector because one connector emits one kind of record: `us.eia.860m` emits
proposals, this one emits existing generators, and each needs its own diff so a planned-retirement
change is an event of its own.

Fetch: inherited unchanged from `pipeline/connectors/us_eia_860m/connector.py` -- the index page,
`/archive/` links dropped before any request (eia.gov robots.txt `Disallow: /*archive/`), the first
candidate whose bytes start with the zip magic wins and placeholder months answering HTML are
recorded and skipped.

Parse (`pipeline/context/retirements.py::parse_generator_sheets`): both sheets, header on the third
row; rows whose `Plant ID` is not a number are dropped (the trailing note rows, and twelve pre-2002
nuclear retirements EIA appends with a blank plant id). Each row keeps its own sheet's columns and
gains `_sheet` (`Operating` | `Retired`) so the state can be recomputed from `raw` alone.

Normalise (kind `document`, the least-wrong existing kind, as `us.eia.860` and `us.epa.ghgrp` use it:
a generator is neither a proposal nor an opportunity, and the generic loader must refuse it):
* `source_record_id` = `<Plant ID>-<Generator ID>`, the EIA identity `us.eia.860m` also uses. A
  generator moving from the Operating to the Retired sheet keeps it, so retirement is a
  `status_change`, never a removal plus an addition.
* `lifecycle_state` = generator state through `asset_status_map.yaml` (`operating`, `standby`,
  `retiring`, `retired`); `status_raw` = the Status column, or `(RE) Retired`.
* `capacity_mw` = nameplate MW. `proposed_cod` = the retirement date (Retired) or the planned one
  (retiring), first of the stated month, or 1 January when EIA states the year alone; null
  otherwise. The runner's diff therefore emits `cod_change` when a planned retirement date moves.
* `identifiers` = `{eia_plant_id, eia_generator_id, technology, retirement}`, `retirement` being the
  date at the precision EIA stated (`YYYY-MM` or `YYYY`).

Restatement (lane FX1): `restate_status` recomputes each stored row's state from its own `raw`
under the current map, so a change to the map is a reclassification on the run, never an event.

The connector's records are loaded by `services/ingest/retirements.py` (assets and their events),
never by the generic proposal loader (`services/ingest/loader.py::SPECIALISED_LOADERS`).
Reuse: US federal work, public domain.
"""

from __future__ import annotations

import json
import pathlib
from typing import Any, ClassVar

import pandas as pd

from pipeline.connectors.base import Kind, ParseError, RawSnapshot
from pipeline.connectors.us_eia_860m.connector import Connector as PlannedSheetConnector
from pipeline.context.retirements import (
    OPERATING_SHEET,
    RETIRED_SHEET,
    RETIRED_STATUS_RAW,
    date_of,
    generator_key,
    generator_state,
    parse_generator_sheets,
    year_month,
)

XLSX_MAGIC = b"PK"


def _plant_id(value: Any) -> str:
    return str(int(float(value)))


def _text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, float) and pd.isna(value):
        return None
    text = str(value).strip()
    return text or None


class Connector(PlannedSheetConnector):
    source_id: ClassVar[str] = "us.eia.860m.retirements"
    kind: ClassVar[Kind] = "document"
    ext: ClassVar[str] = "xlsx"
    status_key: ClassVar[str] = "eia860m_generators"
    status_map_path: ClassVar[pathlib.Path | None] = pathlib.Path(__file__).with_name("asset_status_map.yaml")
    parser_version: ClassVar[str] = "1.0.0"
    key_source_columns: ClassVar[tuple[str, ...]] = (
        "Plant ID",
        "Generator ID",
        "Plant Name",
        "Plant State",
        "Technology",
        "Nameplate Capacity (MW)",
        "Status",
        "Planned Retirement Year",
        "Planned Retirement Month",
        "Retirement Year",
        "Retirement Month",
    )
    dq_required_fields: ClassVar[tuple[str, ...]] = ("title", "capacity_mw", "state_hint")

    def parse(self, raw: RawSnapshot) -> list[dict[str, Any]]:
        if not raw.content.startswith(XLSX_MAGIC):
            raise ParseError(f"{raw.url} answered HTML/other instead of an xlsx (placeholder month)")
        sheets = parse_generator_sheets(raw.content)
        rows: list[dict[str, Any]] = []
        for sheet, df in ((OPERATING_SHEET, sheets.operating), (RETIRED_SHEET, sheets.retired)):
            for record in df.to_dict("records"):
                row = {str(k): v for k, v in record.items()}
                row["_sheet"] = sheet
                rows.append(row)
        if sheets.as_of:
            for row in rows:
                row["_as_of"] = sheets.as_of
        return rows

    def _state(self, row: dict[str, Any]) -> tuple[str, str]:
        return generator_state(str(row.get("_sheet") or OPERATING_SHEET), row, self.status_map)

    def normalize(self, rows: list[dict[str, Any]], raw: RawSnapshot) -> pd.DataFrame:
        records: list[dict[str, Any]] = []
        for row in rows:
            sheet = str(row.get("_sheet") or OPERATING_SHEET)
            state, rule = self._state(row)
            status_raw: str | None
            if sheet == RETIRED_SHEET:
                when = year_month(row.get("Retirement Year"), row.get("Retirement Month"))
                status_raw = RETIRED_STATUS_RAW
            else:
                when = (
                    year_month(row.get("Planned Retirement Year"), row.get("Planned Retirement Month"))
                    if state == "retiring"
                    else None
                )
                status_raw = _text(row.get("Status"))
            plant_id = _plant_id(row["Plant ID"])
            generator_id = generator_key(row.get("Generator ID"))
            name = _text(row.get("Plant Name")) or f"EIA plant {plant_id}"
            mw = pd.to_numeric(row.get("Nameplate Capacity (MW)"), errors="coerce")
            state_code = _text(row.get("Plant State"))
            records.append(
                {
                    "source_record_id": f"{plant_id}-{generator_id}",
                    "doc_type": None,
                    "title": f"{name} unit {generator_id}",
                    "filer": _text(row.get("Entity Name")),
                    "document_class": "generator",
                    "document_type": sheet,
                    "project_name_hint": name,
                    "state_hint": state_code.upper() if state_code else None,
                    "identifiers": json.dumps(
                        {
                            "eia_plant_id": plant_id,
                            "eia_generator_id": generator_id,
                            "technology": _text(row.get("Technology")),
                            "retirement": when,
                        },
                        sort_keys=True,
                    ),
                    "lifecycle_state": state,
                    "status_raw": status_raw,
                    "status_rule": rule,
                    "capacity_mw": None if pd.isna(mw) or float(mw) <= 0 else float(mw),
                    "proposed_cod": date_of(when),
                }
            )
        return self.finalize(pd.DataFrame.from_records(records), rows, raw)

    def restate_status(self, df: pd.DataFrame) -> pd.DataFrame | None:
        """Each stored row's state recomputed from its own `raw` under the current map (FX1)."""
        states: list[str] = []
        rules: list[str] = []
        for payload in df["raw"].tolist():
            try:
                row = json.loads(str(payload)) if payload is not None else {}
            except (TypeError, ValueError):
                row = {}
            state, rule = self._state(row) if row else ("unknown", f"{self.status_key}.blank")
            states.append(state)
            rules.append(rule)
        return pd.DataFrame({"lifecycle_state": states, "status_rule": rules}, index=df.index)
