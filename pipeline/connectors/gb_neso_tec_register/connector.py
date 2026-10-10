"""gb.neso.tec_register — NESO Transmission Entry Capacity (TEC) Register.

Fetch: CKAN `package_show` names the current CSV resource (renamed each publication, e.g.
`tec-register-11-september-2026.csv`), which redirects to object storage.
Parse: CSV with a UTF-8 BOM; 15 columns including "Project ID", "Project Number", "Stage".
source_record_id (2026-10-10, review docs/51 §2.7 item 2): built only from fields that do not move
over a project's life, so a date slip or a status change is a `cod_change` or `status_change` on
the same record, never a `new` plus a `removed`.
  - `<Project ID>` for a project with one row: 2,082 projects, 1,959 of them single-row, on the
    2026-10-10 register. Until this change these rows were keyed `<Project ID>/<hash>` over
    `MW Effective From`, `Project Status`, `MW Connected` and `MW Increase / Decrease`: exactly the
    fields whose changes are the news.
  - `<Project ID>/<Stage>` for a staged row (123 projects with 2-5 stages, 266 rows). The stage is
    written as a number without trailing zeros (`stage_token`): the 2026-09-11 register wrote
    `1.00`, the 2026-10-10 one writes `1`, and the format change must not re-key the stages.
  - `<Project ID>#<n>` for an unstaged row of a project with several rows: n is the row's ordinal
    among that project's unstaged rows in register order. This is the case the hash was added for:
    on the 2026-09-11 register VPI Immingham (a0l4L0000005im7QAA) filed a built row and an
    unstaged +50 MW increase under one id; by 2026-10-10 NESO had staged them (stages 2 and 3), and
    no project has an unstaged row beside another row. The ordinal depends on nothing that changes;
    `#n` is the suffix `pipeline/connectors/dedupe.py::align_previous_keys` already matches by
    content, so a project growing from one unstaged row to several keeps its original row's
    identity when that row's name, customer, technology, capacity and site are unchanged. Going
    from unstaged to staged rows (as Immingham did) is a re-key the diff reports as removed + new.
    If NESO ever reordered two such rows, the diff would report field changes on both rather than
    a new and a removed record.
  The key change is a declared parser change (`parser_version` 2.0.0): the next run, or
  `make reparse`, restates the stored frame under the new keys and emits no events for it
  (`pipeline/connectors/runner.py`, "A parser change is a restatement, not news").
proposed_cod: `MW Effective From`, which NESO writes day-first (`31/10/2034`; ISO `2034-10-31` on
the 2026-09-11 register). `effective_date` reads exactly those two shapes and never guesses: the
shared `to_date` read `01/07/2033` month-first as 7 January, wrong on 182 of 1,857 dated rows of
the 2026-10-10 register (every row whose day is 12 or less and differs from its month).
capacity_mw: the row's own MW, `MW Connected` + `MW Increase / Decrease` (`stage_capacity_mw`). The
register's `Cumulative Total Capacity (MW)` is the project's running total up to that row, so using
it on every stage counted earlier stages again: on the 2026-09-13 register the 267 rows of the 123
multi-row projects summed to 136,499 MW against 85,292 MW real, and all of NESO to 731,514 MW
against 680,307 MW (audit 2026-09-30, data scientist F3). Per row, connected + increase equals the
cumulative figure on all 1,931 single-row projects, and summed over a project's rows it equals the
project's largest cumulative figure on all 123 multi-row projects to within 0.01 MW (three differ by
exactly 0.01 MW, the source's own rounding). The
cumulative figure is kept as its own attribute in `raw` (`Cumulative Total Capacity (MW)`), and
`restate_capacity` restates a stored frame under this rule so the correction is not published as
capacity changes.
Reuse: NESO Open Data Licence v1.0 — attribution "Supported by National Energy SO Open Data".
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import json
import pathlib
import re
import time
from collections import Counter
from typing import Any, ClassVar

import pandas as pd

from pipeline.connectors.base import Connector as BaseConnector
from pipeline.connectors.base import ConnectorError, Kind, ParseError, RawSnapshot, content_hash
from pipeline.connectors.canonical import (
    classify_tech,
    harmonise_status,
    norm_name,
    norm_org,
    to_float,
)

PACKAGE_URL = "https://api.neso.energy/api/3/action/package_show?id=transmission-entry-capacity-tec-register"
DATASET_PAGE = "https://www.neso.energy/data-portal/transmission-entry-capacity-tec-register"


class Connector(BaseConnector):
    source_id: ClassVar[str] = "gb.neso.tec_register"
    kind: ClassVar[Kind] = "proposal"
    #: 2.0.0 (2026-10-10): change-independent record keys and day-first effective dates.
    parser_version: ClassVar[str] = "2.0.0"
    ext: ClassVar[str] = "csv"
    honour_robots: ClassVar[bool] = False  # CKAN API + signed object-storage URL
    status_key: ClassVar[str] = "neso_tec"
    status_map_path: ClassVar[pathlib.Path | None] = pathlib.Path(__file__).with_name("status_map.yaml")
    dq_required_fields: ClassVar[tuple[str, ...]] = (
        "name_canonical",
        "sponsor_name",
        "capacity_mw",
        "technology_raw",
    )
    #: Every column a diffed field, the record id or the placement reads (audit 2026-09-30 F5:
    #: a renamed `Connection Site` nulled the 1,204 GB grid points, a renamed `Stage` re-keyed rows).
    key_source_columns: ClassVar[tuple[str, ...]] = (
        "Project ID",
        "Project Name",
        "Project Status",
        "Plant Type",
        "MW Connected",
        "MW Increase / Decrease",
        "Cumulative Total Capacity (MW)",
        "MW Effective From",
        "Stage",
        "Connection Site",
        "Customer Name",
        "Project Number",
    )

    def restate_capacity(self, df: pd.DataFrame) -> pd.Series | None:
        """`capacity_mw` of a stored frame recomputed from each row's own `raw` (base contract)."""
        return pd.Series([stage_capacity_mw(_raw_row(v)) for v in df["raw"]], index=df.index, dtype="Float64")

    def fetch(self) -> RawSnapshot:
        t0 = time.monotonic()
        pkg = self.http.get(PACKAGE_URL, honour_robots=False).json()
        resources = [
            r for r in pkg.get("result", {}).get("resources", []) if str(r.get("format", "")).upper() == "CSV"
        ]
        if not resources:
            raise ConnectorError("TEC register package lists no CSV resource")
        res = resources[0]
        r = self.http.get(res["url"], honour_robots=False, timeout=180)
        if r.status_code != 200:
            raise ConnectorError(f"GET {res['url']} -> HTTP {r.status_code}")
        return RawSnapshot(
            content=r.content,
            content_type=r.headers.get("Content-Type", ""),
            url=res["url"],
            retrieved_at=dt.datetime.now(dt.UTC),
            http_status=r.status_code,
            ext="csv",
            headers=dict(r.headers),
            elapsed_s=round(time.monotonic() - t0, 2),
            requests_made=2,
            meta={
                "resource_id": res.get("id"),
                "last_modified": res.get("last_modified"),
                "licence": pkg.get("result", {}).get("license_title"),
                "dataset_page": DATASET_PAGE,
            },
        )

    def parse(self, raw: RawSnapshot) -> list[dict[str, Any]]:
        text = raw.content.decode("utf-8-sig", errors="replace")
        if text.lstrip().lower().startswith("<html"):
            raise ParseError("TEC register download answered HTML (redirect not followed?)")
        rows = list(csv.DictReader(io.StringIO(text)))
        if not rows or "Project ID" not in rows[0]:
            raise ParseError(f"TEC register layout changed: {list(rows[0]) if rows else 'empty'}")
        return rows

    def normalize(self, rows: list[dict[str, Any]], raw: RawSnapshot) -> pd.DataFrame:
        n = len(rows)

        def g(key: str) -> list[Any]:
            return [r.get(key) for r in rows]

        tech = [classify_tech(v) for v in g("Plant Type")]
        harmonised = [
            harmonise_status(self.status_key, {"status_raw": s}, self.status_map) for s in g("Project Status")
        ]
        cap = [stage_capacity_mw(r) for r in rows]
        df = pd.DataFrame(
            {
                "source_record_id": record_keys(rows),
                "source_url": DATASET_PAGE,
                "kind": [k for _, k in tech],
                "name_canonical": g("Project Name"),
                "name_norm": [norm_name(v) for v in g("Project Name")],
                "sponsor_name": g("Customer Name"),
                "sponsor_norm": [norm_org(v) for v in g("Customer Name")],
                "technology": [t for t, _ in tech],
                "technology_raw": g("Plant Type"),
                "capacity_mw": pd.array(cap, dtype="Float64"),
                "storage_mwh": pd.array([None] * n, dtype="Float64"),
                "iso": "NESO",
                "state": None,
                "county": g("Connection Site"),
                "county_norm": None,
                "lifecycle_state": [s for s, _ in harmonised],
                "status_raw": g("Project Status"),
                "status_rule": [r for _, r in harmonised],
                "status_conflict": False,
                "queue_date": pd.NaT,
                "proposed_cod": [effective_date(v) for v in g("MW Effective From")],
                "queue_id": g("Project Number"),
                "eia_plant_id": None,
                "eia_generator_id": None,
                "cross_refs": "",
            }
        )
        return self.finalize(df, rows, raw)


def stage_capacity_mw(row: dict[str, Any]) -> float | None:
    """The MW this row itself adds: `MW Connected` + `MW Increase / Decrease` (module docstring).

    Not `Cumulative Total Capacity (MW)`, which repeats every earlier stage of the project, and not
    the increase alone, which is 0 on a built stage (Arecleoch stage 1: 114 MW connected, +0).
    Falls back to the cumulative figure only when the row states neither component. A row whose
    net is negative (a stage that only reduces TEC) has no capacity of its own: None."""
    connected = to_float(row.get("MW Connected"))
    increase = to_float(row.get("MW Increase / Decrease"))
    if connected is None and increase is None:
        return to_float(row.get("Cumulative Total Capacity (MW)"))
    mw = (connected or 0.0) + (increase or 0.0)
    return round(mw, 3) if mw >= 0 else None


def _raw_row(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


_DAY_FIRST = re.compile(r"^(?P<d>\d{1,2})/(?P<m>\d{1,2})/(?P<y>\d{4})$")
_ISO_DATE = re.compile(r"^(?P<y>\d{4})-(?P<m>\d{2})-(?P<d>\d{2})(?:[T ].*)?$")


def effective_date(value: Any) -> pd.Timestamp:
    """`MW Effective From` as a date: `DD/MM/YYYY` (the register's form since October 2026) or
    ISO `YYYY-MM-DD` (its form before). Anything else, an impossible date included, is NaT:
    a slash date is never read month-first (module docstring)."""
    text = str(value or "").strip()
    m = _DAY_FIRST.match(text) or _ISO_DATE.match(text)
    if m is None:
        return pd.NaT
    try:
        return pd.Timestamp(dt.date(int(m.group("y")), int(m.group("m")), int(m.group("d"))))
    except ValueError:
        return pd.NaT


def stage_token(value: Any) -> str:
    """The `Stage` cell as a key part: `1.00` and `1` are both `1`; blank is ''."""
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        number = float(text)
    except ValueError:
        return text
    return str(int(number)) if number.is_integer() else f"{number:g}"


def record_keys(rows: list[dict[str, Any]]) -> list[str]:
    """`source_record_id` for every row, in row order (module docstring): the project id alone
    for a single-row project, `/<stage>` for a staged row, `#<n>` for the n-th unstaged row of a
    project with several rows. A row without a project id (none so far) is keyed on its name,
    customer and connection site, which a status or date change does not touch."""
    ids = [str(r.get("Project ID") or "").strip() for r in rows]
    ids = [
        pid or "noid-" + content_hash(r.get("Project Name"), r.get("Customer Name"), r.get("Connection Site"))
        for pid, r in zip(ids, rows, strict=True)
    ]
    rows_per_project = Counter(ids)
    unstaged_seen: Counter[str] = Counter()
    keys: list[str] = []
    for pid, row in zip(ids, rows, strict=True):
        stage = stage_token(row.get("Stage"))
        if stage:
            keys.append(f"{pid}/{stage}")
        elif rows_per_project[pid] == 1:
            keys.append(pid)
        else:
            unstaged_seen[pid] += 1
            keys.append(f"{pid}#{unstaged_seen[pid]}")
    return keys
