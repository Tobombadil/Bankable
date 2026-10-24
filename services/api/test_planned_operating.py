"""`GET /v1/stats/planned-vs-operating` (`services/api/planned_operating.py`; docs/24-trends.md §11).

The contract held here:

- both power-plant sides come from one EIA-860M month, the newest the trends series serve, and every
  figure is the sum of the named lines at that month of the `GET /v1/trends` series it names, asked
  through the real app;
- planned splits by stage under the stated mapping (TS, construction complete, is not under
  construction), the stages add up to the total (a code outside the vocabulary is its own row),
  under a state or technology filter too, from the month's paired tallies (`US-TX|U`); a month
  without them serves no filtered figure rather than an unsplit one;
- a technology the served month does not tally is refused, never read as a zero;
- the power plants are served only while the EIA archive's source is visible at the tier, and the
  note says which reason holds when they are not;
- the completion rate is the one fixed, cited line;
- data centres are counted by power with the capacity endpoint's own machinery, in the 50 states and
  DC: each figure is the `include=count` total of its list, nothing a list would not show is counted,
  a withheld register's value is counted as served, and records no register lists are never counted
  as planned; national demand is never set against one state's planned MW; only the connectors
  pinned here write the `load` records counted;
- the responses validate against `api/openapi.yaml`.
"""

from __future__ import annotations

import datetime as dt
import pathlib
import re
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012
from sqlalchemy import select
from sqlalchemy.orm import Session

from pipeline.connectors.history.documents import DOCUMENT_KIND, SCHEMA_VERSION, MonthlyStore
from pipeline.connectors.history.eia860m.connector import PAIRED, split_pair
from services.api import planned_operating as po
from services.api.conftest import make_open_licence, make_public_source, make_visible_proposal
from services.api.ratelimit import default_limiter
from services.db.models import Licence, Proposal, ProposalSource, Source
from services.ingest.loader import specialised_loader
from services.trends import series as trends

UTC = dt.UTC
PATH = po.PATH
EIA = trends.EIA
SPEC_PATH = pathlib.Path(__file__).resolve().parents[2] / "api" / "openapi.yaml"
ACTIVE = "announced,filed,studied,permitted,contracted,under_construction"


# --------------------------------------------------------------------------------- the documents
def _t(count: int, mw: float) -> dict[str, Any]:
    return {
        "count": count,
        "mw": mw,
        "mw_stated": count,
        "nameplate_mw": mw * 1.05,
        "nameplate_mw_stated": count,
    }


OPERATING = {
    "basis": "eia860m/operating/net-summer-mw/50-states-dc",
    "total": _t(35, 1750.5),
    "by_technology": {"gas_cc": _t(10, 1000.0), "solar": _t(20, 500.0), "wind": _t(5, 250.5)},
    "by_state": {"US-TX": _t(15, 1100.0), "US-CA": _t(20, 650.5)},
    "by_status": {"OP": _t(30, 1600.0), "SB": _t(5, 150.5)},
    # Each generator once, with its state or technology and its status (the connector's `PAIRED`).
    "by_state_status": {"US-TX|OP": _t(15, 1100.0), "US-CA|OP": _t(15, 500.0), "US-CA|SB": _t(5, 150.5)},
    "by_technology_status": {
        "gas_cc|OP": _t(10, 1000.0),
        "solar|OP": _t(16, 350.0),
        "solar|SB": _t(4, 150.0),
        "wind|OP": _t(4, 250.0),
        "wind|SB": _t(1, 0.5),
    },
    "excluded": {},
}
PLANNED = {
    "basis": "eia860m/planned/net-summer-mw/50-states-dc",
    "total": _t(14, 910.0),
    "by_technology": {
        "solar": _t(8, 400.0),
        "storage": _t(4, 300.0),
        "gas_cc": _t(1, 200.0),
        "unknown": _t(1, 10.0),
    },
    "by_state": {"US-TX": _t(10, 700.0), "US-CA": _t(3, 200.0), "US-VA": _t(1, 10.0)},
    # An `OP` row on the Planned sheet (EIA lists some: 2015-11, 2018-11, 2026-06) is outside the stages.
    "by_status": {
        "U": _t(3, 300.0),
        "V": _t(2, 150.0),
        "TS": _t(1, 50.0),
        "T": _t(2, 100.0),
        "L": _t(3, 160.0),
        "P": _t(2, 140.0),
        "OP": _t(1, 10.0),
    },
    "by_state_status": {
        "US-TX|U": _t(2, 200.0),
        "US-TX|V": _t(2, 150.0),
        "US-TX|TS": _t(1, 50.0),
        "US-TX|T": _t(1, 50.0),
        "US-TX|L": _t(2, 110.0),
        "US-TX|P": _t(2, 140.0),
        "US-CA|U": _t(1, 100.0),
        "US-CA|T": _t(1, 50.0),
        "US-CA|L": _t(1, 50.0),
        "US-VA|OP": _t(1, 10.0),
    },
    "by_technology_status": {
        "gas_cc|U": _t(1, 200.0),
        "unknown|OP": _t(1, 10.0),
        "storage|V": _t(2, 150.0),
        "storage|TS": _t(1, 50.0),
        "storage|L": _t(1, 100.0),
        "solar|U": _t(2, 100.0),
        "solar|T": _t(2, 100.0),
        "solar|L": _t(2, 60.0),
        "solar|P": _t(2, 140.0),
    },
    "by_expected_year": {"2027": _t(14, 910.0)},
    "excluded": {},
}


def test_the_fixtures_pairs_hold_each_generator_once() -> None:
    """The paired tallies summed onto either key are the single tallies, as the connector writes
    them (`pipeline/connectors/history/eia860m/test_connector.py`), so the figures below are true."""
    for block in (OPERATING, PLANNED):
        assert {k[3:] for k in block if k.endswith("_status") and k != "by_status"} == set(PAIRED)
        for paired, first in PAIRED.items():
            for side, single in ((0, first), (1, "status")):
                sums: dict[str, tuple[int, float]] = {}
                for key, tally in block[f"by_{paired}"].items():
                    part = split_pair(key)[side]
                    count, mw = sums.get(part, (0, 0.0))
                    sums[part] = (count + tally["count"], round(mw + tally["mw"], 3))
                assert sums == {k: (t["count"], t["mw"]) for k, t in block[f"by_{single}"].items()}


def _doc(period: str, *, scale: float = 1.0, held: bool = False) -> dict[str, Any]:
    def scaled(block: dict[str, Any]) -> dict[str, Any]:
        out = dict(block)
        if scale != 1.0:
            out["total"] = _t(block["total"]["count"], block["total"]["mw"] * scale)
        return out

    return {
        "schema_version": SCHEMA_VERSION,
        "kind": DOCUMENT_KIND,
        "source_id": EIA,
        "period": period,
        "source_url": f"https://www.eia.gov/electricity/data/eia860m/xls/{period}_generator.xlsx",
        "archive_url": f"https://www.eia.gov/electricity/data/eia860m/archive/xls/{period}_generator.xlsx",
        "retrieved_at": "2026-10-24T06:28:00Z",
        "parser_version": "1.0.0+test",
        "blocks": {"operating": scaled(OPERATING), "planned": scaled(PLANNED)},
        "dq": {
            "status": "held" if held else "warn",
            "checks": [{"block": "planned", "check": "technology_blank", "level": "warn"}]
            + ([{"block": "planned", "check": "rows_drift", "level": "hold"}] if held else []),
        },
    }


@pytest.fixture(autouse=True)
def data_root(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    monkeypatch.setenv("INFRAQUE_DATA_DIR", str(tmp_path))
    trends.clear_cache()
    store = MonthlyStore(tmp_path)
    store.write(_doc("2026-08", scale=0.9))
    store.write(_doc("2026-09"))
    store.write(_doc("2026-10", scale=5.0, held=True))  # held by its checks: never served
    return tmp_path


def _eia_source(db: Session, *, publish_state: str = "public", reuse_class: str = "open") -> Source:
    licence = make_open_licence(db, "us.eia.860m.history#0123456789")
    licence.reuse_class = reuse_class
    source = make_public_source(db, licence, id_=EIA)
    source.name = "EIA-860M monthly archive"
    source.operator = "US Energy Information Administration"
    source.publish_state = publish_state
    source.attribution_text = "Source: U.S. Energy Information Administration"
    db.commit()
    return source


@pytest.fixture(scope="module")
def spec() -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load(SPEC_PATH.read_text())
    return loaded


def assert_valid(spec: dict[str, Any], instance: object) -> None:
    """Against `components.schemas.PlannedVsOperatingResponse` of the committed spec."""
    base = "https://spec.local/openapi.yaml"
    registry = Registry().with_resource(uri=base, resource=Resource(contents=spec, specification=DRAFT202012))
    validator = Draft202012Validator(
        {"$ref": f"{base}#/components/schemas/PlannedVsOperatingResponse"}, registry=registry
    )
    errors = sorted(validator.iter_errors(instance), key=str)
    assert not errors, "\n".join(f"{e.message} at {list(e.absolute_path)}" for e in errors)


def _get(client: TestClient, params: dict[str, str] | None = None) -> dict[str, Any]:
    default_limiter.reset()
    resp = client.get(PATH, params=params or {})
    assert resp.status_code == 200, resp.text
    body: dict[str, Any] = resp.json()
    return body


def _series_value(client: TestClient, ref: dict[str, Any], lines: list[str], period: str) -> float:
    """The sum of `lines` at `period` in the series `ref` names, asked through the app; a line the
    series does not have is zero (module docstring of `planned_operating`)."""
    default_limiter.reset()
    assert ref["path"] == "/v1/trends"
    resp = client.get(ref["path"], params=ref["query"])
    assert resp.status_code == 200, resp.text
    total = 0.0
    found = {line["key"]: line for line in resp.json()["data"]["lines"]}
    for key in lines:
        points = [p for p in (found[key]["points"] if key in found else []) if p["period"] == period]
        assert len(points) <= 1
        total += points[0]["value"] if points else 0.0
    return round(total, 3)


def _assert_reproduces(
    client: TestClient, figure: dict[str, Any], series: dict[str, Any], period: str
) -> None:
    assert _series_value(client, series["mw"], figure["lines"], period) == pytest.approx(
        figure["mw"], abs=1e-6
    )
    assert _series_value(client, series["generators"], figure["lines"], period) == figure["generators"]


# --------------------------------------------------------------------------------- power plants
def test_both_sides_come_from_the_newest_served_month(client: TestClient, db: Session) -> None:
    _eia_source(db)
    body = _get(client)
    plants = body["data"]["power_plants"]
    assert plants["as_of"] == "2026-09"  # 2026-10 is held by its checks
    assert [w["period"] for w in plants["newer_months_withheld"]] == ["2026-10"]
    assert plants["held_reading"] is None
    assert plants["unit"] == "MW" and plants["measure"] == "net summer capacity"
    assert (plants["operating"]["generators"], plants["operating"]["mw"]) == (35, 1750.5)
    assert (plants["planned"]["generators"], plants["planned"]["mw"]) == (14, 910.0)
    assert plants["planned_share_of_operating_pct"] == 52.0
    assert plants["planned"]["dq_warnings"] == ["technology_blank"]
    assert plants["operating"]["dq_warnings"] == []
    source = plants["source"]
    assert source["source_id"] == EIA and source["url"].endswith("2026-09_generator.xlsx")
    assert source["retrieved_at"] == "2026-10-24T06:28:00Z"
    # EIA asks for the publication date: September's data was published in October.
    assert source["credit"] == "Source: U.S. Energy Information Administration (Oct 2026)"
    assert source["licence"]["reuse_class"] == "open"
    [row] = [s for s in body["licence_summary"]["sources"] if s["source_id"] == EIA]
    assert row["record_count"] == 1  # one month read
    assert body["data"]["power_plants_note"] is None


def test_planned_splits_by_stage_under_the_stated_mapping(client: TestClient, db: Session) -> None:
    _eia_source(db)
    planned = _get(client)["data"]["power_plants"]["planned"]
    rows = planned["by_stage"]["rows"]
    assert [(r["stage"], r["codes"], r["generators"], r["mw"]) for r in rows] == [
        ("construction_complete", ["TS"], 1, 50.0),
        ("under_construction", ["U", "V"], 5, 450.0),
        ("approvals_received", ["T"], 2, 100.0),
        ("approvals_pending", ["L"], 3, 160.0),
        ("early_or_other", ["P", "OT"], 2, 140.0),
        ("unmapped", ["OP"], 1, 10.0),
    ]
    assert sum(r["mw"] for r in rows) == planned["mw"]
    assert sum(r["generators"] for r in rows) == planned["generators"]
    assert rows[1]["share_of_planned_pct"] == 49.5
    assert [r["lines"] for r in rows][1] == ["U", "V"]  # without a filter, the status lines
    mapping = _get(client)["data"]["power_plants"]["stage_mapping"]
    assert [m["stage"] for m in mapping] == [s.stage for s in po.STAGES]
    codes = {c["code"]: c["label"] for m in mapping for c in m["codes"]}
    assert codes["TS"] == "Construction complete, but not yet in commercial operation"
    assert set(codes) == {"U", "V", "TS", "T", "L", "P", "OT"}


def test_built_but_not_yet_operating_is_not_counted_as_under_construction(
    client: TestClient, db: Session
) -> None:
    """EIA labels TS "Construction complete, but not yet in commercial operation", so the stage that
    says under construction holds U and V only (review, 2026-10-24: 17,155.2 MW of September 2026's
    96,236.0 MW "under construction" was TS)."""
    _eia_source(db)
    rows = {r["stage"]: r for r in _get(client)["data"]["power_plants"]["planned"]["by_stage"]["rows"]}
    assert rows["under_construction"]["codes"] == ["U", "V"]
    assert rows["under_construction"]["label"] == "Under construction"
    assert rows["under_construction"]["mw"] == 450.0  # U 300 + V 150, not TS's 50
    assert rows["construction_complete"]["codes"] == ["TS"]
    assert "not yet in commercial operation" in rows["construction_complete"]["label"]


def test_a_month_with_no_unmapped_code_has_no_unmapped_row() -> None:
    block = {
        "total": (5, 500.0, None),
        "dims": {"status": {"U": (2, 200.0, None), "P": (3, 300.0, None)}},
    }
    rows = po._stages(block)
    assert [r["stage"] for r in rows] == [s.stage for s in po.STAGES]
    assert [r["mw"] for r in rows] == [0.0, 200.0, 0.0, 0.0, 300.0]


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"state": "US-TX"},
        {"state": "US-TX,US-CA,US-VA"},
        {"technology": "solar,storage"},
        {"technology": "unknown"},
    ],
)
def test_every_power_plant_figure_is_its_trends_series_point(
    params: dict[str, str], client: TestClient, db: Session
) -> None:
    _eia_source(db)
    plants = _get(client, params)["data"]["power_plants"]
    period = plants["as_of"]
    for side in ("operating", "planned"):
        _assert_reproduces(client, plants[side], plants[side]["series"], period)
    stages = plants["planned"]["by_stage"]
    statuses = plants["operating"]["by_status"]
    for split, side in ((stages, "planned"), (statuses, "operating")):
        for row in split["rows"]:
            _assert_reproduces(client, row, split["series"], period)
        # The split adds up to its side's figure, filtered or not.
        assert sum(r["generators"] for r in split["rows"]) == plants[side]["generators"]
        assert round(sum(r["mw"] for r in split["rows"]), 3) == plants[side]["mw"]
    for name in ("by_technology", "by_state"):
        breakdown = plants[name]
        if breakdown is None:
            continue
        assert breakdown["rows"]
        for row in breakdown["rows"]:
            for side in ("operating", "planned"):
                _assert_reproduces(client, row[side], breakdown["series"][side], period)


def _stage_rows(split: dict[str, Any]) -> list[tuple[Any, ...]]:
    return [(r["stage"], r["generators"], r["mw"], r["lines"]) for r in split["rows"]]


def test_a_state_filter_splits_the_states_planned_capacity_by_stage(client: TestClient, db: Session) -> None:
    """Coordinator, 2026-10-24 (docs/00-PLAN.md, planned against operating, item 2): planned is
    always split by stage, so under a state filter the split is the state's own, read from the
    `state_status` lines (until then `by_stage` was null with a note)."""
    _eia_source(db)
    data = _get(client, {"state": "us-tx"})["data"]
    plants = data["power_plants"]
    assert data["geography"] == {"country": "US", "states": ["US-TX"], "technologies": None}
    assert (plants["operating"]["mw"], plants["planned"]["mw"]) == (1100.0, 700.0)
    assert plants["operating"]["lines"] == ["US-TX"]
    assert plants["planned"]["series"]["mw"]["query"]["by"] == "state"
    stages = plants["planned"]["by_stage"]
    assert stages["series"]["mw"]["query"] == {
        "metric": "us_planned_capacity_mw",
        "by": "state_status",
        "from": "2026-09",
        "to": "2026-09",
    }
    assert _stage_rows(stages) == [
        ("construction_complete", 1, 50.0, ["US-TX|TS"]),
        ("under_construction", 4, 350.0, ["US-TX|U", "US-TX|V"]),
        ("approvals_received", 1, 50.0, ["US-TX|T"]),
        ("approvals_pending", 2, 110.0, ["US-TX|L"]),
        ("early_or_other", 2, 140.0, ["US-TX|P"]),
    ]
    assert stages["rows"][1]["share_of_planned_pct"] == 50.0  # of Texas's 700 MW, not the nation's 910
    assert "by_stage_note" not in plants["planned"]
    statuses = plants["operating"]["by_status"]
    assert statuses["series"]["generators"]["query"]["by"] == "state_status"
    assert [(r["status"], r["generators"], r["mw"], r["lines"]) for r in statuses["rows"]] == [
        ("OP", 15, 1100.0, ["US-TX|OP"])
    ]
    assert plants["by_technology"] is None
    assert [r["state"] for r in plants["by_state"]["rows"]] == ["US-TX"]
    several = _get(client, {"state": "US-TX,US-VA"})["data"]["power_plants"]
    assert (several["operating"]["mw"], several["planned"]["mw"]) == (1100.0, 710.0)
    # A state the month's complete tally does not hold has none.
    assert [r["operating"]["mw"] for r in several["by_state"]["rows"]] == [1100.0, 0.0]
    # Virginia's one planned row is an `OP` code on the Planned sheet: its own row, so the sum holds.
    assert _stage_rows(several["planned"]["by_stage"])[-1] == ("unmapped", 1, 10.0, ["US-VA|OP"])
    assert sum(r["mw"] for r in several["planned"]["by_stage"]["rows"]) == 710.0


@pytest.mark.parametrize(
    "params",
    [{"state": s} for s in ("US-TX", "US-CA", "US-VA", "US-NY")]
    + [{"technology": t} for t in ("solar", "storage", "gas_cc", "unknown", "wind")],
    ids=str,
)
def test_the_stages_add_up_to_each_states_and_each_technologys_planned_total(
    params: dict[str, str], client: TestClient, db: Session
) -> None:
    _eia_source(db)
    plants = _get(client, params)["data"]["power_plants"]
    for side, split in (("planned", "by_stage"), ("operating", "by_status")):
        rows = plants[side][split]["rows"]
        assert sum(r["generators"] for r in rows) == plants[side]["generators"], side
        assert round(sum(r["mw"] for r in rows), 3) == plants[side]["mw"], side
    stages = plants["planned"]["by_stage"]["rows"]
    assert [r["stage"] for r in stages][:5] == [s.stage for s in po.STAGES]  # every stage, zeros too


def test_a_technology_filter_reads_the_technology_lines_and_leaves_data_centres_out(
    client: TestClient, db: Session
) -> None:
    _eia_source(db)
    data = _get(client, {"technology": "storage,solar"})["data"]
    plants = data["power_plants"]
    assert (plants["operating"]["mw"], plants["planned"]["mw"]) == (500.0, 700.0)
    assert [(r["technology"], r["label"]) for r in plants["by_technology"]["rows"]] == [
        ("solar", "Solar"),
        ("storage", "Storage"),
    ]
    storage = plants["by_technology"]["rows"][1]
    assert storage["planned_share_of_operating_pct"] is None  # no storage operates: no share of nothing
    assert plants["by_state"] is None
    stages = plants["planned"]["by_stage"]
    assert stages["series"]["mw"]["query"]["by"] == "technology_status"
    assert _stage_rows(stages) == [
        ("construction_complete", 1, 50.0, ["storage|TS"]),
        ("under_construction", 4, 250.0, ["solar|U", "storage|V"]),
        ("approvals_received", 2, 100.0, ["solar|T"]),
        ("approvals_pending", 3, 160.0, ["solar|L", "storage|L"]),
        ("early_or_other", 2, 140.0, ["solar|P"]),
    ]
    statuses = plants["operating"]["by_status"]["rows"]
    assert [(r["status"], r["mw"], r["lines"]) for r in statuses] == [
        ("OP", 350.0, ["solar|OP"]),
        ("SB", 150.0, ["solar|SB"]),
    ]
    assert data["data_centres"] is None and data["data_centres_note"] == po.DATA_CENTRES_WITH_TECHNOLOGY


@pytest.mark.parametrize("params", [{"state": "US-TX"}, {"technology": "solar"}], ids=str)
def test_a_month_without_the_paired_tallies_serves_no_filtered_figure_rather_than_an_unsplit_one(
    params: dict[str, str], client: TestClient, db: Session, data_root: pathlib.Path, spec: dict[str, Any]
) -> None:
    """A month an earlier parser wrote (before 1.1.0, until its restatement) has no `state_status` or
    `technology_status`: a filtered view of it would have no stage split, so it serves no figure and
    names the month; the national view, which reads `status`, is served as before."""
    _eia_source(db)
    newest = _doc("2026-11")
    for name in ("operating", "planned"):
        newest["blocks"][name] = {
            k: v for k, v in newest["blocks"][name].items() if not k.endswith("_status") or k == "by_status"
        }
    newest["parser_version"] = "1.0.0+8ad6632f"
    MonthlyStore(data_root).write(newest)
    trends.clear_cache()
    body = _get(client, params)
    assert body["data"]["power_plants"] is None
    note = body["data"]["power_plants_note"]
    assert note == po.POWER_PLANTS_NOT_RESTATED.format(
        period="2026-11", parser="1.0.0+8ad6632f", filter=next(iter(params))
    )
    assert "restated" in note and "2026-11" in note
    assert_valid(spec, body)
    national = _get(client)["data"]["power_plants"]
    assert national["as_of"] == "2026-11" and national["planned"]["by_stage"]["rows"]
    default_limiter.reset()
    refused = client.get(PATH, params={"technology": "gas"})  # a bad token is still a 400, not the note
    assert refused.status_code == 400


@pytest.mark.parametrize(
    "technology", ["gas", "solar,gas", "solar_pv", "bess", "load", "transmission", "coal"]
)
def test_a_technology_the_month_does_not_tally_is_refused_not_served_as_zero(
    technology: str, client: TestClient, db: Session
) -> None:
    """Review, 2026-10-24: `technology=gas` answered 200 with 0 MW on both sides against the real
    September 2026 document (295,138.4 MW of `gas_cc` operating), `solar_pv`, `bess`, `load` and
    `transmission` the same, and `solar,gas` quietly served solar's figures alone. The register's
    vocabulary holds tokens EIA-860M's classifier never produces; only the month's own keys are
    taken (this month: `gas_cc`, `solar`, `storage`, `unknown`, `wind`; no coal)."""
    _eia_source(db)
    default_limiter.reset()
    resp = client.get(PATH, params={"technology": technology})
    assert resp.status_code == 400, resp.text
    [error] = resp.json()["errors"]
    assert error["field"] == "technology"
    assert "2026-09" in error["message"] and "gas_cc, solar, storage, unknown, wind" in error["message"]


def test_a_technology_on_one_side_only_is_a_true_zero_on_the_other(client: TestClient, db: Session) -> None:
    _eia_source(db)
    plants = _get(client, {"technology": "unknown"})["data"]["power_plants"]
    assert (plants["operating"]["mw"], plants["planned"]["mw"]) == (0.0, 10.0)
    assert plants["operating"]["lines"] == ["unknown"]


def test_without_a_served_month_a_technology_filter_counts_nothing(client: TestClient) -> None:
    """With no month to check it against, the register's vocabulary is the only check, and no
    figure is served, so no zero can be false."""
    data = _get(client, {"technology": "gas"})["data"]
    assert data["power_plants"] is None and data["power_plants_note"] == po.POWER_PLANTS_NO_MONTH
    assert data["data_centres"] is None


def test_without_a_filter_both_breakdowns_list_every_key_largest_planned_first(
    client: TestClient, db: Session
) -> None:
    _eia_source(db)
    plants = _get(client)["data"]["power_plants"]
    assert [r["technology"] for r in plants["by_technology"]["rows"]] == [
        "solar",
        "storage",
        "gas_cc",
        "unknown",
        "wind",
    ]
    assert [r["state"] for r in plants["by_state"]["rows"]] == ["US-TX", "US-CA", "US-VA"]
    assert plants["by_state"]["rows"][0]["planned_share_of_operating_pct"] == 63.6


@pytest.mark.parametrize(
    ("params", "field"),
    [
        ({"state": "TX"}, "state"),
        ({"state": "US-PR"}, "state"),
        ({"state": "US-ZZ"}, "state"),
        ({"technology": "solr"}, "technology"),
        ({"state": "US-TX", "technology": "solar"}, "technology"),
    ],
)
def test_filters_the_document_cannot_answer_are_refused(
    params: dict[str, str], field: str, client: TestClient
) -> None:
    resp = client.get(PATH, params=params)
    assert resp.status_code == 400
    assert resp.json()["errors"][0]["field"] == field


def test_an_unknown_parameter_is_refused(client: TestClient) -> None:
    resp = client.get(PATH, params={"country": "US"})
    assert resp.status_code == 400 and resp.json()["code"] == "unknown_parameter"


@pytest.mark.parametrize(
    ("setup", "note"),
    [
        ("no_source_row", po.POWER_PLANTS_NO_MONTH),
        ("ingest_only", po.POWER_PLANTS_NOT_SERVED),
        ("restricted", po.POWER_PLANTS_NOT_SERVED),
        ("no_document", po.POWER_PLANTS_NO_MONTH),
        ("no_status_tally", po.POWER_PLANTS_INCOMPLETE.format(period="2026-11")),
    ],
    ids=lambda s: str(s)[:24],
)
def test_the_power_plants_follow_the_archive_sources_gate(
    setup: str,
    note: str,
    client: TestClient,
    db: Session,
    data_root: pathlib.Path,
    spec: dict[str, Any],
) -> None:
    """Each reason has its own note (review, 2026-10-24: a served month without a tally the figures
    read was reported as "not served at this tier, or no month read")."""
    if setup == "ingest_only":
        _eia_source(db, publish_state="ingest_only")
    elif setup == "restricted":
        _eia_source(db, reuse_class="restricted")
    elif setup == "no_document":
        _eia_source(db)
        for path in (data_root / "normalized" / "trends" / "monthly" / EIA).glob("*.json"):
            path.unlink()
        trends.clear_cache()
    elif setup == "no_status_tally":
        _eia_source(db)
        newest = _doc("2026-11")
        newest["blocks"]["planned"] = {k: v for k, v in PLANNED.items() if k != "by_status"}
        MonthlyStore(data_root).write(newest)
        trends.clear_cache()
    body = _get(client)
    assert body["data"]["power_plants"] is None
    assert body["data"]["power_plants_note"] == note
    assert EIA not in {s["source_id"] for s in body["licence_summary"]["sources"]}
    assert body["data"]["completion_rate"]["reached_operation_pct"] == 13
    assert_valid(spec, body)


def test_latest_month_skips_held_months_and_names_them(data_root: pathlib.Path) -> None:
    month = trends.latest_month(data_root, EIA)
    assert month is not None
    assert month.point.period == "2026-09"
    assert [w["period"] for w in month.newer_withheld] == ["2026-10"]
    assert month.held_reading is None
    assert trends.latest_month(data_root, trends.ERCOT) is None


def test_a_held_reading_of_the_served_month_is_named_and_never_read(
    client: TestClient, db: Session, data_root: pathlib.Path
) -> None:
    _eia_source(db)
    MonthlyStore(data_root).write_pending(_doc("2026-09", scale=2.0, held=True))
    trends.clear_cache()
    plants = _get(client)["data"]["power_plants"]
    assert plants["held_reading"]["period"] == "2026-09"
    assert plants["operating"]["mw"] == 1750.5  # the served document's, not the held reading's


def test_the_series_named_are_the_trends_catalogues_eia_series() -> None:
    """The figures read the blocks and tallies those series read, so a figure and its series point
    are one number."""
    for side, metrics in po.SERIES.items():
        for unit, metric in metrics.items():
            spec_ = trends.BY_METRIC[metric]
            assert (spec_.source_id, spec_.block, spec_.frequency) == (EIA, side, "monthly")
            assert spec_.value == {"mw": "mw", "generators": "count"}[unit]
            assert {"technology", "state", "status"} <= set(spec_.dimensions)


# ---------------------------------------------------------------------------- the fixed lines
def test_the_completion_rate_is_one_fixed_cited_line(client: TestClient) -> None:
    line = _get(client)["data"]["completion_rate"]
    assert (line["reached_operation_pct"], line["withdrawn_pct"], line["still_active_pct"]) == (13, 75, 10)
    assert line["cohort"] == "2000-2020" and line["as_of"] == "2025-12"
    assert "13%" in line["text"] and "75%" in line["text"]
    assert line["source"]["url"] == "https://emp.lbl.gov/publications/queued-2026-edition-characteristics"
    assert "not EIA-860M's planned generators" in line["applies_to"]


def test_average_data_centre_demand_is_cited_and_converted(client: TestClient) -> None:
    demand = _get(client)["data"]["data_centres"]["average_demand"]
    measured, projected = demand["measured_2023"], demand["projected_2028"]
    assert (measured["twh"], measured["average_gw"]) == (176, 20.1)  # 176,000 GWh / 8,760 h
    assert (projected["twh_low"], projected["twh_high"]) == (325, 580)
    assert (projected["average_gw_low"], projected["average_gw_high"]) == (37.0, 66.0)  # / 8,784 h
    assert measured["source"]["url"].startswith("https://www.energy.gov/articles/doe-releases-new-report")
    assert demand["geography"] == "US"


# ---------------------------------------------------------------------------------- data centres
def _licence(db: Session, id_: str, reuse_class: str) -> Licence:
    lic = make_open_licence(db, id_)
    lic.reuse_class = reuse_class
    db.flush()
    return lic


def _load(
    db: Session,
    source: Source,
    n: int,
    *,
    state: str = "announced",
    capacity: float | None = 300.0,
    jurisdiction: str = "US-VA",
    kind: str = "load",
) -> Proposal:
    prop = make_visible_proposal(
        db, source, public_id_suffix=str(n), lifecycle_state=state, technology=kind, jurisdiction=jurisdiction
    )
    prop.kind = kind
    prop.capacity_mw = capacity
    db.flush()
    return prop


@pytest.fixture()
def loads(db: Session) -> dict[str, Proposal]:
    now = dt.datetime.now(UTC)
    pub = make_public_source(db, make_open_licence(db), id_="us.test.data_centres")
    restricted = make_public_source(
        db, _licence(db, "restricted-lic", "restricted"), id_="us.test.restricted"
    )
    staged = make_public_source(db, make_open_licence(db, "staged-lic"), id_="us.test.staged")
    staged.publish_state = "ingest_only"
    out = {
        "va": _load(db, pub, 1),
        "tx_uc": _load(db, pub, 2, state="under_construction", capacity=500.0, jurisdiction="US-TX"),
        "tx_nomw": _load(db, pub, 3, state="permitted", capacity=None, jurisdiction="US-TX"),
        "va_built": _load(db, pub, 4, state="built", capacity=100.0),
        "tx_delisted": _load(db, pub, 5, capacity=250.0, jurisdiction="US-TX"),
        "restricted": _load(db, restricted, 6, capacity=9000.0, jurisdiction="US-TX"),
        "future": _load(db, pub, 7, capacity=9000.0),
        "gb": _load(db, pub, 8, capacity=80.0, jurisdiction="GB"),
        # Outside the 50 states and DC, the EIA side's geography: never counted (review, 2026-10-24).
        "pr": _load(db, pub, 11, capacity=70.0, jurisdiction="US-PR"),
        "us_only": _load(db, pub, 12, capacity=60.0, jurisdiction="US"),
        "solar": _load(db, pub, 9, capacity=200.0, jurisdiction="US-TX", kind="generation"),
        "mixed": _load(db, pub, 10, capacity=999.0, jurisdiction="US-TX"),
    }
    out["future"].public_at = now + dt.timedelta(days=1)
    gone = db.scalars(select(ProposalSource).where(ProposalSource.proposal_id == out["tx_delisted"].id)).one()
    gone.gone_at = now - dt.timedelta(days=3)
    # Served through the public register at 40 MW; the stored 999 MW came from the off-surface one.
    mixed = out["mixed"]
    link = db.scalars(select(ProposalSource).where(ProposalSource.proposal_id == mixed.id)).one()
    link.normalised = {
        "capacity_mw": 40.0,
        "lifecycle_state": "announced",
        "technology": "load",
        "kind": "load",
        "jurisdiction": "US-TX",
    }
    db.add(
        ProposalSource(
            proposal_id=mixed.id,
            source_id=staged.id,
            source_record_id="S-mixed",
            source_url=staged.url,
            retrieved_at=now - dt.timedelta(days=1),
            licence_id=staged.licence_id,
            normalised={"capacity_mw": 999.0},
            first_seen=now - dt.timedelta(days=2),
            last_seen=now - dt.timedelta(days=1),
        )
    )
    mixed.field_provenance = {"capacity_mw": {"source_id": staged.id, "licence_id": staged.licence_id}}
    mixed.source_count = 2
    db.commit()
    return out


def _list_total(client: TestClient, query: dict[str, str]) -> tuple[int, float]:
    """`include=count` total of a list and the MW its rows serve."""
    default_limiter.reset()
    resp = client.get("/v1/proposals", params={**query, "include": "count", "limit": "200"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert not body["page"]["has_more"]
    return body["meta"]["total"], round(sum(r["capacity_mw"] or 0.0 for r in body["data"]), 3)


def test_data_centres_compare_by_power_with_each_figure_its_list(
    client: TestClient, loads: dict[str, Proposal], spec: dict[str, Any]
) -> None:
    body = _get(client)
    assert_valid(spec, body)
    centres = body["data"]["data_centres"]
    planned = centres["planned"]
    # Listed, the 50 states and DC, public, in an active state: Virginia's 300, Texas's 500 and the
    # served 40; one states no MW. Puerto Rico's 70 and the record placed only to the US are not counted.
    assert (planned["records"], planned["demand_mw"], planned["demand_records"], planned["mw_missing"]) == (
        4,
        840.0,
        3,
        1,
    )
    assert planned["capacity_mw"] is None
    assert centres["geography"] == "the 50 states and the District of Columbia"
    assert planned["list_query"] == {
        "path": "/v1/proposals",
        "query": {
            "kind": "load",
            "jurisdiction": ",".join(po.NATIONAL_JURISDICTIONS),
            "lifecycle_state": ACTIVE,
            "listed": "true",
        },
    }
    assert len(po.NATIONAL_JURISDICTIONS) == 51 and "US-DC" in po.NATIONAL_JURISDICTIONS
    assert "US-PR" not in po.NATIONAL_JURISDICTIONS
    assert _list_total(client, planned["list_query"]["query"]) == (4, 840.0)
    assert centres["planned_not_listed"]["records"] == 1  # the delisted 250 MW, never counted as planned
    assert centres["served_not_stored"]["records"] == 1
    sites = centres["sites_we_track"]
    assert sites["total"]["records"] == 5 and "not a census" in sites["coverage"]
    assert sum(_list_total(client, ref["query"])[0] for ref in sites["total"]["list_queries"]) == 5
    states = {row["lifecycle_state"]: row["records"] for row in sites["by_lifecycle_state"]}
    assert states == {"announced": 2, "permitted": 1, "under_construction": 1, "built": 1}
    assert [s["source_id"] for s in centres["sources"]] == ["us.test.data_centres"]
    assert centres["retrieved_at_max"] == centres["sources"][0]["retrieved_at_max"]
    assert centres["as_of"] == centres["retrieved_at_max"][:7]
    credited = {s["source_id"] for s in body["licence_summary"]["sources"]}
    assert credited == {"us.test.data_centres"}  # no EIA source row in this store: no power plants
    assert "us.test.restricted" not in credited


def test_a_state_filter_narrows_the_data_centres_by_jurisdiction(
    client: TestClient, loads: dict[str, Proposal]
) -> None:
    texas = _get(client, {"state": "US-TX"})["data"]["data_centres"]
    assert texas["geography"] == ["US-TX"]
    assert (texas["planned"]["records"], texas["planned"]["demand_mw"], texas["planned"]["mw_missing"]) == (
        3,
        540.0,
        1,
    )
    assert texas["planned"]["list_query"]["query"]["jurisdiction"] == "US-TX"
    assert _list_total(client, texas["planned"]["list_query"]["query"]) == (3, 540.0)
    virginia = _get(client, {"state": "US-VA"})["data"]["data_centres"]
    assert (virginia["planned"]["demand_mw"], virginia["sites_we_track"]["total"]["records"]) == (300.0, 2)


def test_under_a_state_filter_national_demand_is_not_set_against_a_states_planned_mw(
    client: TestClient, loads: dict[str, Proposal], spec: dict[str, Any]
) -> None:
    """Review, 2026-10-24: `state=US-TX` served Texas's planned MW beside all-US demand (176 TWh,
    20.1 GW). The DOE and Berkeley Lab figures have no state split, so a state view carries none."""
    body = _get(client, {"state": "US-TX"})
    assert_valid(spec, body)
    texas = body["data"]["data_centres"]
    assert texas["average_demand"] is None
    assert texas["average_demand_note"] == po.AVERAGE_DEMAND_NATIONAL_ONLY
    national = _get(client)["data"]["data_centres"]
    assert national["average_demand"]["measured_2023"]["average_gw"] == 20.1
    assert national["average_demand_note"] is None


#: The connectors that write `kind = load`, and what their records are. The data-centre side counts
#: every `load` record and sets it against data-centre demand, so a new emitter must be read first.
LOAD_EMITTERS = {
    "us_epa_echo_icis_air": "data centres (ICIS-Air facilities selected as data centres)",
    "us_va_deq_data_center_air_sites": "data centres (sites Virginia DEQ flags as data centres)",
    "us_iso_ercot_large_load_queue": "catalogue products, never written as proposals",
    "us_epa_echo_icis_npdes_construction": "data centres (construction coverages selected, docs/08)",
    "us_faa_oeaaa_part77_cases": "data centres (Part 77 cases selected as data-centre sites, docs/08)",
    "us_va_loudoun_data_centers": "data centres (Loudoun County's own data-centre pipeline, docs/08)",
}
_KIND_LOAD = re.compile(r"""["']kind["']\s*:\s*["']load["']|\bkind\s*=\s*["']load["']""")


def test_only_data_centre_registers_feed_the_data_centre_side() -> None:
    """Review, 2026-10-24: `kind=load` is data centres and other large loads, and ERCOT's large-load
    queue connector emits `load` rows, which would then be set against DOE data-centre demand. They
    never reach the register: its loader is the catalogue watch, which writes no proposal. Any other
    connector that starts writing `load` records fails here until someone decides whether they are
    data centres (docs/24 §11.6) and, if not, narrows `data_centres` by `source_id`."""
    connectors = pathlib.Path(__file__).resolve().parents[2] / "pipeline" / "connectors"
    emitters = {
        path.parent.name
        for path in connectors.glob("*/connector.py")
        if _KIND_LOAD.search(path.read_text(encoding="utf-8"))
    }
    assert emitters == set(LOAD_EMITTERS), (
        f"connectors writing kind=load changed: {sorted(emitters ^ set(LOAD_EMITTERS))}"
    )
    loader = specialised_loader("us.iso.ercot.large_load_queue")
    assert loader is not None and loader.__name__ == "load_catalogue_watch_run"


def test_everything_validates_with_power_plants_and_data_centres(
    client: TestClient, db: Session, loads: dict[str, Proposal], spec: dict[str, Any]
) -> None:
    _eia_source(db)
    for params in ({}, {"state": "US-TX"}, {"technology": "solar"}):
        body = _get(client, params)
        assert_valid(spec, body)
    sources = {s["source_id"] for s in _get(client)["licence_summary"]["sources"]}
    assert sources == {EIA, "us.test.data_centres"}
