"""Ember's connector on the recorded fixtures, offline (synthetic values in Ember's published shapes;
`testing.py` serves them through `FakeSession`, so no test reaches a host).

Parsers: the API answer and both CSV layouts read to the same normalised rows; every row carries the
provenance quartet and its source row; areas key to `world`, a region slug or an ISO alpha-2 code; Ember's
series names map to the platform's sources, unknown ones are counted (vocabulary drift) and demand or net
imports are skipped; a file missing a column fails with the column's name; the key never survives in a URL
or a body.

Runs: a run with a key writes three datasets with provenance and no trace of the key; a repeat is
`unchanged`; a revised value is counted, never an event; without a key the run is `blocked` and asks no
host; a refused key, a server error or a check that holds keep the last good output; the inbox CSV is read
once; the CSV host is never contacted; the CLI prints the result line the scheduler reads.

Review of 2026-10-23, held in place: a yearly file whose periods are full dates is read as yearly, from its
name, sidecar or structure, and a "monthly" file that looks yearly is held; a file without a country code
column fails naming it, and a dataset with no country (or far fewer than the other route's load) is held;
flagged countries without a code warn; the monthly window rolls, so the January runs neither hold nor count
the month that left the window as gone; the inbox is read whatever the API answers; a download without a
recorded time is held, its placed-at time kept apart; the first download of a dataset waits for a person's
check, and `check` prints what that person reads.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import io
import json
import pathlib
from typing import Any
from urllib.parse import urlsplit

import pandas as pd
import pytest
import yaml

from pipeline.connectors.base import ParseError, RawSnapshot
from pipeline.connectors.global_ember_electricity import SOURCE_ID, ember
from pipeline.connectors.global_ember_electricity.__main__ import main, trim
from pipeline.connectors.global_ember_electricity.areas import ISO3_TO_ISO2, area_for, region_slug
from pipeline.connectors.global_ember_electricity.runner import ENV_KEY, EmberGateRefused, EmberRun, RunResult
from pipeline.connectors.global_ember_electricity.testing import (
    CAPACITY_JSON,
    LONG_CSV,
    MONTHLY_JSON,
    PATH_FIXTURES,
    TEST_KEY,
    WIDE_CSV,
    YEARLY_JSON,
    FakeSession,
    csv_bytes,
    dated_wide_csv,
    fixed_clock,
    fixture_bytes,
    wide_rows,
    with_data,
)
from pipeline.connectors.http import HttpFailed
from pipeline.connectors.registry import SOURCES_YAML, Registry
from pipeline.connectors.store import Store

TODAY = dt.date(2026, 10, 23)
WHEN = dt.datetime(2026, 10, 23, 9, 49, tzinfo=dt.UTC)


def _raw(name: str, url: str = "https://example.org") -> RawSnapshot:
    return RawSnapshot(
        content=fixture_bytes(name), content_type="", url=url, retrieved_at=WHEN, http_status=200, ext="json"
    )


def _requests() -> dict[str, ember.EmberRequest]:
    return {r.key: r for r in ember.plan(TODAY, key_present=True)}


def _normalised(name: str, key: str):  # type: ignore[no-untyped-def]
    req = _requests()[key]
    raw = _raw(name, req.url)
    obs, report = ember.parse_api(raw, req)
    df = ember.normalize(
        obs,
        dataset=req.dataset,
        origin="api",
        source_url=req.url,
        retrieved_at=raw.retrieved_at_iso,
        licence_id="global.ember.electricity#0123456789",
        report=report,
    )
    return df, report


def test_the_plan_asks_three_endpoints_with_a_key_and_none_without() -> None:
    reqs = ember.plan(TODAY, key_present=True)
    assert [r.path for r in reqs] == [
        "/v1/electricity-generation/yearly",
        "/v1/electricity-generation/monthly",
        "/v1/installed-capacity/monthly",
    ]
    assert reqs[0].params == {"start_date": "2000"}
    assert reqs[1].params == {"start_date": "2023-11"}  # a rolling 36 months, the current one included
    assert all("api_key" not in r.url for r in reqs)
    assert ember.plan(TODAY, key_present=False) == []


def test_yearly_api_answer_normalises_with_provenance_on_every_row() -> None:
    df, report = _normalised(YEARLY_JSON, "yearly_generation")
    assert not report.unmapped and report.bad_rows == 0
    assert set(df["metric"]) == {"generation_twh", "share_of_generation_pct"}
    for col in ("source_id", "source_url", "retrieved_at", "licence_id", "raw"):
        assert df[col].notna().all() and (df[col].astype(str) != "").all(), col
    assert set(df["source_id"]) == {SOURCE_ID}
    assert set(df["area_id"]) == {"world", "europe", "US", "CN", "DE", "IN", "BR"}
    assert dict(df.groupby("area_id")["area_kind"].first()) == {
        "world": "world",
        "europe": "region",
        "US": "country",
        "CN": "country",
        "DE": "country",
        "IN": "country",
        "BR": "country",
    }
    assert df["record_id"].is_unique
    us_solar = df[
        (df.area_id == "US")
        & (df.period == "2025")
        & (df.source == "solar")
        & (df.metric == "generation_twh")
    ]
    assert us_solar["value"].tolist() == [300.0]
    assert json.loads(us_solar["raw"].iloc[0])["entity_code"] == "USA"
    # Ember's aggregates are kept, flagged; the nine generation types are not aggregates.
    flags = dict(df.groupby("source")["is_aggregate"].first())
    assert flags["total_generation"] and flags["clean"] and not flags["solar"]


def test_monthly_answers_carry_the_month_and_capacity_answers_only_capacity() -> None:
    df, _ = _normalised(MONTHLY_JSON, "monthly_generation")
    assert set(df["period"]) == {"2025-07", "2025-08", "2026-07", "2026-08"}
    assert df["month"].notna().all()
    cap, _ = _normalised(CAPACITY_JSON, "monthly_capacity")
    assert set(cap["metric"]) == {"capacity_gw"} and set(cap["source"]) == {"solar", "wind"}
    assert set(cap["unit"]) == {"GW"}


def test_an_unknown_series_is_counted_and_left_out() -> None:
    payload = json.loads(fixture_bytes(YEARLY_JSON))
    payload["data"][0]["series"] = "Fusion"
    req = _requests()["yearly_generation"]
    raw = RawSnapshot(
        content=json.dumps(payload).encode(),
        content_type="",
        url=req.url,
        retrieved_at=WHEN,
        http_status=200,
        ext="json",
    )
    obs, report = ember.parse_api(raw, req)
    df = ember.normalize(
        obs,
        dataset="yearly",
        origin="api",
        source_url=req.url,
        retrieved_at="t",
        licence_id="x",
        report=report,
    )
    assert report.unmapped == {"Fusion": 1}
    assert "Fusion" not in set(df["source_label"])


def test_a_body_that_is_not_the_documented_shape_fails() -> None:
    req = _requests()["yearly_generation"]
    for body in (b"not json", b'{"stats": {}}'):
        raw = RawSnapshot(
            content=body, content_type="", url=req.url, retrieved_at=WHEN, http_status=200, ext="json"
        )
        with pytest.raises(ParseError):
            ember.parse_api(raw, req)


def test_a_cut_off_answer_fails_the_run_and_keeps_the_last_good_output(tmp_path: pathlib.Path) -> None:
    store = Store(tmp_path)
    good = _run(store)
    payload = json.loads(fixture_bytes(YEARLY_JSON))
    payload["data"] = payload["data"][:-40]  # `stats.number_of_records` still states the whole answer
    bodies = {path: fixture_bytes(name) for path, name in PATH_FIXTURES.items()}
    bodies[YEARLY] = json.dumps(payload).encode("utf-8")
    cut = _run(store, FakeSession(bodies), when=T0 + dt.timedelta(days=14))
    assert cut.record["status"] == "failed" and cut.record["error_class"] == "ParseError"
    assert "states 280 records and holds 240" in cut.record["error"]
    assert sorted(p.stem for p in (tmp_path / "normalized" / SOURCE_ID).iterdir()) == [good.ts]


@pytest.mark.parametrize("name, layout", [(WIDE_CSV, "wide"), (LONG_CSV, "long")])
def test_both_csv_layouts_read_capacity_generation_and_share(name: str, layout: str) -> None:
    raw = RawSnapshot(
        content=fixture_bytes(name),
        content_type="",
        url="inbox",
        retrieved_at=WHEN,
        http_status=200,
        ext="csv",
    )
    obs, report = ember.parse_csv(raw)
    assert report.layout == layout
    kind = ember.classify_file(obs, name="download.csv")  # no hint in the name: the structure decides
    assert (kind.dataset, kind.basis, kind.dated_years) == ("yearly", "structure", False)
    df = ember.normalize(
        obs,
        dataset="yearly",
        origin="file",
        source_url=ember.YEARLY_PAGE,
        retrieved_at="t",
        licence_id="x",
        report=report,
    )
    assert set(df["metric"]) == {"generation_twh", "share_of_generation_pct", "capacity_gw"}
    assert not report.unmapped
    world_solar = df[(df.area_id == "world") & (df.period == "2025") & (df.source == "solar")]
    assert dict(zip(world_solar.metric, world_solar.value, strict=True))["capacity_gw"] == 2200.0
    # Emissions (long layout) and demand and net imports (wide layout) are not read as sources.
    assert "Demand" not in set(df["source_label"]) and "mtCO2" not in set(df["unit"])
    if layout == "wide":
        assert report.non_source_rows == 14  # Demand and Net imports for seven areas


def test_the_wide_csv_and_the_api_agree_on_every_shared_value() -> None:
    api, _ = _normalised(YEARLY_JSON, "yearly_generation")
    raw = RawSnapshot(
        content=fixture_bytes(WIDE_CSV),
        content_type="",
        url="inbox",
        retrieved_at=WHEN,
        http_status=200,
        ext="csv",
    )
    obs, report = ember.parse_csv(raw)
    csv_df = ember.normalize(
        obs, dataset="yearly", origin="file", source_url="u", retrieved_at="t", licence_id="x", report=report
    )
    key = ["area_id", "period", "source", "metric"]
    merged = api.merge(csv_df, on=key, suffixes=("_api", "_file"))
    assert len(merged) == len(api)
    assert (merged["value_api"] == merged["value_file"]).all()


def test_a_csv_missing_a_column_names_it() -> None:
    body = b"Area,Year,Electricity source\nWorld,2025,Solar\n"
    raw = RawSnapshot(
        content=body, content_type="", url="inbox", retrieved_at=WHEN, http_status=200, ext="csv"
    )
    with pytest.raises(ParseError, match="generation_twh"):
        ember.parse_csv(raw)


def test_areas_key_to_world_region_slugs_and_alpha2() -> None:
    assert area_for("World", None, is_aggregate=True).id == "world"
    assert area_for("United States", "USA", is_aggregate=False).id == "US"
    assert (
        area_for("Latin America and Caribbean", None, is_aggregate=True).id == "latin-america-and-caribbean"
    )
    assert area_for("EU", None, is_aggregate=True).id == "eu"
    assert area_for("Kosovo", "XKX", is_aggregate=None).id == "XK"
    unknown = area_for("Somewhere", "ZZZ", is_aggregate=False)
    assert unknown.id == "ZZZ" and unknown.kind == "country"  # kept as alpha-3, never an alpha-2 clash
    assert region_slug("Asia & Pacific") == "asia-and-pacific"
    assert len(ISO3_TO_ISO2) == 250 and len(set(ISO3_TO_ISO2.values())) == 250


def test_series_names_in_any_case_map_to_one_vocabulary() -> None:
    assert ember.source_for("Other Fossil") == ("other_fossil", False)
    assert ember.source_for("other fossil") == ("other_fossil", False)
    assert ember.source_for("Wind & Solar") == ("wind_and_solar", True)
    assert ember.source_for("Total generation") == ("total_generation", True)
    assert ember.source_for("Storage") is None  # Ember's global data has none
    assert ember.is_non_source("Net imports") and ember.is_non_source("Demand")
    assert ember.CLEAN_SOURCES == {"solar", "wind", "hydro", "nuclear", "bioenergy", "other_renewables"}


def test_the_key_is_removed_from_text_and_bytes() -> None:
    assert (
        ember.redact("GET /v1/x?api_key=secret-key-123", ["secret-key-123"]) == "GET /v1/x?api_key=[redacted]"
    )
    assert b"secret-key-123" not in ember.redact_bytes(b'{"api_key": "secret-key-123"}', ["secret-key-123"])
    assert ember.redact("short", ["abc"]) == "short"


def test_the_digest_ignores_the_stats_block() -> None:
    a = json.loads(fixture_bytes(YEARLY_JSON))
    b = dict(a, stats={**a["stats"], "timestamp": "2027-01-01T00:00:00Z", "response_time_in_seconds": 9})
    assert ember.data_digest(json.dumps(a).encode()) == ember.data_digest(json.dumps(b).encode())


def test_trim_keeps_the_named_areas_and_years() -> None:
    body = trim(fixture_bytes(YEARLY_JSON), areas={"World", "USA"}, years=(2025, 2025))
    data = json.loads(body)["data"]
    assert {d["entity"] for d in data} == {"World", "United States"} and {d["date"] for d in data} == {"2025"}
    csv_body = trim(fixture_bytes(WIDE_CSV), areas={"DEU"}, years=(2024, 2025)).decode()
    lines = csv_body.strip().splitlines()
    assert lines[0].startswith("Area,ISO 3 code") and all(",DEU," in line for line in lines[1:])


def test_the_manifest_row_carries_the_terms_the_connector_relies_on() -> None:
    entry = Registry().get(SOURCE_ID)
    assert entry.reuse == "attribution" and entry.publication == "raw_ok"
    assert "CC-BY-4.0" in entry.license and "Ember is cited as the data source" in entry.license
    assert entry.licence_url == "https://creativecommons.org/licenses/by/4.0/"
    assert "Ember" in entry.attribution and entry.changes_statement
    assert entry.raw.get("runner") == "global"
    assert entry.max_rps == 0.5
    assert entry.verified.get("date") == dt.date(2026, 10, 24)


# -------------------------------------------------------------------------------------------- runs
T0 = dt.datetime(2026, 10, 8, 9, 49, tzinfo=dt.UTC)
YEARLY = "/v1/electricity-generation/yearly"


def _run(
    store: Store, http: FakeSession | None = None, *, key: str | None = TEST_KEY, when: dt.datetime = T0
) -> RunResult:
    env = {ENV_KEY: key} if key else {}
    return EmberRun(store, http=http or FakeSession(), env=env, clock=fixed_clock(when)).run()  # type: ignore[arg-type]


def _all_bytes(root: pathlib.Path) -> bytes:
    return b"".join(p.read_bytes() for p in root.rglob("*") if p.is_file())


def test_a_run_with_a_key_writes_three_datasets_with_provenance_and_no_key(tmp_path: pathlib.Path) -> None:
    store = Store(tmp_path)
    http = FakeSession()
    result = _run(store, http)
    record = result.record
    assert record["status"] == "ok", record.get("error")
    assert record["outputs"]["datasets"] == ["monthly/api", "monthly_capacity/api", "yearly/api"]
    df = pd.read_parquet(tmp_path / "normalized" / SOURCE_ID / f"{result.ts}.parquet")
    for col in ("source_id", "source_url", "retrieved_at", "licence_id", "raw"):
        assert df[col].notna().all(), col
    assert set(df["licence_id"]) == {Registry().get(SOURCE_ID).licence_id}
    assert record["rows_new"] == len(df) and record["events_emitted"] == 0
    # The key travelled in the query string only: nothing stored, no URL recorded, carries it.
    assert all(c["params"]["api_key"] == TEST_KEY for c in http.calls)
    assert TEST_KEY.encode() not in _all_bytes(tmp_path)
    assert all("api_key" not in r["url"] for r in record["requests"])
    # Only Ember's API host was asked: never the CSV host, whose robots.txt forbids robots.
    assert {c["host"] for c in http.calls} == {"api.ember-energy.org"}
    assert all(c["honour_robots"] is True for c in http.calls)
    snaps = sorted(p.name for p in (tmp_path / "snapshots" / SOURCE_ID).iterdir())
    assert snaps == [
        f"{result.ts}.{k}.json" for k in ("monthly_capacity", "monthly_generation", "yearly_generation")
    ]


def test_a_repeat_is_unchanged_and_writes_nothing_new(tmp_path: pathlib.Path) -> None:
    store = Store(tmp_path)
    first = _run(store)
    second = _run(store, when=T0 + dt.timedelta(days=14))
    assert second.record["status"] == "unchanged"
    assert all(r["unchanged"] for r in second.record["requests"])
    assert not (tmp_path / "normalized" / SOURCE_ID / f"{second.ts}.parquet").exists()
    assert (tmp_path / "normalized" / SOURCE_ID / f"{first.ts}.parquet").exists()


def test_a_revised_value_is_counted_and_kept_never_an_event(tmp_path: pathlib.Path) -> None:
    store = Store(tmp_path)
    _run(store)

    def revise(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        for row in rows:
            if row["entity"] == "United States" and row["date"] == "2024" and row["series"] == "Solar":
                row["generation_twh"] = 231.0
        return rows

    bodies = {path: fixture_bytes(name) for path, name in PATH_FIXTURES.items()}
    bodies[YEARLY] = with_data(YEARLY_JSON, revise)
    later = _run(store, FakeSession(bodies), when=T0 + dt.timedelta(days=14))
    record = later.record
    assert record["status"] == "ok"
    assert record["outputs"]["datasets"] == ["yearly/api"]  # the other two answers were unchanged
    assert record["datasets"]["yearly/api"]["revisions"] == {"new": 0, "changed": 1, "gone": 0}
    assert record["rows_changed"] == 1 and record["events_emitted"] == 0
    revisions = pd.read_parquet(tmp_path / "revisions" / SOURCE_ID / f"{later.ts}.parquet")
    assert revisions["record_id"].tolist() == [f"{SOURCE_ID}:api|yearly|US|2024|solar|generation_twh"]
    assert revisions["value_after"].tolist() == [231.0]
    assert not (tmp_path / "events").exists()


def test_without_a_key_the_run_is_blocked_and_asks_nobody(tmp_path: pathlib.Path) -> None:
    store = Store(tmp_path)
    http = FakeSession()
    result = _run(store, http, key=None)
    assert result.record["status"] == "blocked"
    assert result.record["error_class"] == "MissingApiKey" and ENV_KEY in result.record["error"]
    assert http.calls == []
    assert not (tmp_path / "normalized").exists()


def test_a_refused_key_or_a_server_error_keeps_the_last_good_output(tmp_path: pathlib.Path) -> None:
    store = Store(tmp_path)
    good = _run(store)
    refused = _run(store, FakeSession(statuses={YEARLY: 403}), when=T0 + dt.timedelta(days=14))
    assert refused.record["status"] == "blocked" and refused.record["error_class"] == "KeyRefused"
    error = HttpFailed(f"GET https://api.ember-energy.org{YEARLY}?api_key={TEST_KEY} failed after 4 attempts")
    failed = _run(store, FakeSession(errors={YEARLY: error}), when=T0 + dt.timedelta(days=15))
    assert failed.record["status"] == "failed" and failed.record["error_class"] == "HttpFailed"
    assert TEST_KEY not in failed.record["error"] and "[redacted]" in failed.record["error"]
    written = sorted(p.stem for p in (tmp_path / "normalized" / SOURCE_ID).iterdir())
    assert written == [good.ts]
    # Nothing from a failed run is stored, not even the answers that came back before the failure.
    assert not any(p.name.startswith(failed.ts) for p in (tmp_path / "snapshots" / SOURCE_ID).iterdir())


def test_a_check_that_holds_keeps_the_dataset_out(tmp_path: pathlib.Path) -> None:
    store = Store(tmp_path)
    _run(store)
    keep = {"World", "United States"}  # five of seven areas vanish: rows fall by more than 30%
    bodies = {path: fixture_bytes(name) for path, name in PATH_FIXTURES.items()}
    bodies[YEARLY] = with_data(YEARLY_JSON, lambda rows: [r for r in rows if r["entity"] in keep])
    held = _run(store, FakeSession(bodies), when=T0 + dt.timedelta(days=14))
    assert held.record["status"] == "partial"
    assert held.record["datasets"]["yearly/api"]["written"] is False
    levels = {
        c["check"]: c["level"] for c in held.record["dq"]["checks"] if c["data"]["dataset"] == "yearly/api"
    }
    assert levels["row_count"] == "hold" and levels["country_count"] == "hold"
    assert not (tmp_path / "normalized" / SOURCE_ID / f"{held.ts}.parquet").exists()
    # The held answer is not the new baseline: the next good answer compares with the last written one.
    again = _run(store, when=T0 + dt.timedelta(days=15))
    assert again.record["status"] == "unchanged"


def test_unknown_series_on_many_rows_hold_the_dataset(tmp_path: pathlib.Path) -> None:
    def rename(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        for row in rows:
            if row["series"] in ("Solar", "Wind"):
                row["series"] = "Solar PV" if row["series"] == "Solar" else "Wind power"
        return rows

    bodies = {path: fixture_bytes(name) for path, name in PATH_FIXTURES.items()}
    bodies[YEARLY] = with_data(YEARLY_JSON, rename)
    result = _run(Store(tmp_path), FakeSession(bodies))
    vocab = next(
        c
        for c in result.record["dq"]["checks"]
        if c["check"] == "vocabulary" and c["data"]["dataset"] == "yearly/api"
    )
    assert vocab["level"] == "hold" and set(vocab["data"]["unmapped"]) == {"Solar PV", "Wind power"}
    assert result.record["status"] == "partial"


def _drop(
    tmp_path: pathlib.Path,
    name: str,
    body: bytes,
    downloaded_at: str | None = None,
    *,
    reviewed: bool = True,
    dataset: str | None = None,
) -> pathlib.Path:
    """A person's download and the sidecar they write (`runner` module docstring)."""
    inbox = tmp_path / "inbox" / SOURCE_ID
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / name).write_bytes(body)
    sidecar: dict[str, Any] = {}
    if downloaded_at:
        sidecar["downloaded_at"] = downloaded_at
    if reviewed:
        sidecar["reviewed_sha256"] = hashlib.sha256(body).hexdigest()
    if dataset:
        sidecar["dataset"] = dataset
    if sidecar:
        (inbox / name).with_suffix(".json").write_text(json.dumps(sidecar))
    return inbox / name


def test_a_persons_download_is_read_once_without_a_key(tmp_path: pathlib.Path) -> None:
    store = Store(tmp_path)
    _drop(tmp_path, "release_generation_yearly_global.csv", fixture_bytes(WIDE_CSV), "2026-10-20T10:00:00Z")
    first = _run(store, key=None)
    assert first.record["status"] == "ok", first.record
    assert first.record["outputs"]["datasets"] == ["yearly/file"]
    assert first.record["inbox"][0]["state"] == "loaded"
    df = pd.read_parquet(tmp_path / "normalized" / SOURCE_ID / f"{first.ts}.parquet")
    assert set(df["retrieved_at"]) == {"2026-10-20T10:00:00Z"}
    assert set(df["source_url"]) == {"https://ember-energy.org/data/yearly-electricity-data/"}
    assert "capacity_gw" in set(df["metric"])
    assert any(c["check"] == "api_key" and c["level"] == "warn" for c in first.record["dq"]["checks"])
    second = _run(store, key=None, when=T0 + dt.timedelta(days=14))
    assert second.record["inbox"][0]["state"] == "already_loaded"
    assert second.record["status"] == "blocked"  # nothing new, and the API cannot be asked


def test_a_download_in_an_unknown_layout_is_held_with_its_reason(tmp_path: pathlib.Path) -> None:
    _drop(tmp_path, "odd.csv", b"Country,When,What\nWorld,2025,Solar\n")
    result = _run(Store(tmp_path), key=None)
    assert result.record["status"] == "partial"
    assert result.record["inbox"][0]["state"] == "failed"
    assert "generation_twh" in result.record["inbox"][0]["error"]


def test_a_row_the_terms_hold_back_is_refused(tmp_path: pathlib.Path) -> None:
    doc = yaml.safe_load(SOURCES_YAML.read_text(encoding="utf-8"))
    for entry in doc["sources"]:
        if entry["id"] == SOURCE_ID:
            entry["status"] = "pending"
    path = tmp_path / "sources.yaml"
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    with pytest.raises(EmberGateRefused):
        EmberRun(
            Store(tmp_path / "data"), registry=Registry(path), http=FakeSession(), env={ENV_KEY: TEST_KEY}
        ).run()  # type: ignore[arg-type]


def test_the_cli_prints_the_result_line_the_scheduler_reads(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from infra.scheduler.jobs import parse_result_line

    monkeypatch.delenv(ENV_KEY, raising=False)
    monkeypatch.setenv("SNAPSHOT_STORE", "local")
    code = main(["run", "--data-dir", str(tmp_path), "--trigger", "schedule"])
    assert code == 1
    line = parse_result_line(capsys.readouterr().out)
    assert line is not None and line["status"] == "blocked" and line["source_id"] == SOURCE_ID
    record = json.loads(pathlib.Path(line["run_path"]).read_text())
    assert record["trigger"] == "schedule" and record["error_class"] == "MissingApiKey"


def test_the_fixture_command_trims_a_file(tmp_path: pathlib.Path) -> None:
    target = tmp_path / "small.json"
    src = tmp_path / "big.json"
    src.write_bytes(fixture_bytes(MONTHLY_JSON))
    assert main(["fixture", str(src), str(target), "--areas", "USA", "--years", "2026"]) == 0
    data = json.loads(target.read_text())["data"]
    assert data and {d["entity_code"] for d in data} == {"USA"}
    assert all(d["date"].startswith("2026") for d in data)
    assert json.load(io.BytesIO(fixture_bytes(CAPACITY_JSON)))["_fixture"].startswith("SYNTHETIC")


# ------------------------------------------------------------------- review of 2026-10-23
YEARLY_NAME = "release_generation_yearly_global.csv"
MONTHLY = "/v1/electricity-generation/monthly"


_wide, _csv, _dated = wide_rows, csv_bytes, dated_wide_csv


def _parse(body: bytes) -> tuple[list[ember.Observation], ember.ParseReport]:
    raw = RawSnapshot(
        content=body, content_type="", url="inbox", retrieved_at=WHEN, http_status=200, ext="csv"
    )
    return ember.parse_csv(raw)


def _checks(result: RunResult, dataset: str | None = None) -> dict[str, dict[str, Any]]:
    return {
        c["check"]: c
        for c in result.record["dq"]["checks"]
        if dataset is None or c["data"].get("dataset") == dataset
    }


@pytest.mark.parametrize("name", [YEARLY_NAME, "ember_download.csv"])
def test_a_yearly_file_with_full_dates_loads_as_yearly(tmp_path: pathlib.Path, name: str) -> None:
    _drop(tmp_path, name, _dated(), "2026-10-20T10:00:00Z")
    result = _run(Store(tmp_path), key=None)
    assert result.record["status"] == "ok", result.record["dq"]["checks"]
    assert result.record["outputs"]["datasets"] == ["yearly/file"]
    item = result.record["inbox"][0]
    assert item["dataset"] == "yearly" and item["dataset_basis"] == (
        "name" if name == YEARLY_NAME else "structure"
    )
    df = pd.read_parquet(tmp_path / "normalized" / SOURCE_ID / f"{result.ts}.parquet")
    assert set(df["period"]) == {"2023", "2024", "2025"} and df["month"].isna().all()
    world_solar = df[(df.area_id == "world") & (df.period == "2025") & (df.source == "solar")]
    assert dict(zip(world_solar.metric, world_solar.value, strict=True)) == {
        "generation_twh": 2700.0,
        "share_of_generation_pct": 8.65,
        "capacity_gw": 2200.0,
    }


def test_a_file_whose_name_and_structure_disagree_is_held() -> None:
    dated, _ = _parse(_dated())
    kind = ember.classify_file(dated, name="release_generation_monthly_global.csv")
    assert kind.dataset is None and "every period is January" in str(kind.hold)
    assert ember.classify_file(dated, name="x.csv", declared="monthly").basis == "sidecar"
    # Two months a year with capacity values: not Ember's monthly file, whatever it is called.
    fields, rows = _wide()
    twice = [{**r, "Date": f"{r['Year']}-{m:02d}-01"} for r in rows for m in (1, 2)]
    body = _csv(["Date" if f == "Year" else f for f in fields], twice)
    monthly, _ = _parse(body)
    held = ember.classify_file(monthly, name="release_generation_monthly_global.csv")
    assert held.dataset is None and "Capacity (GW)" in str(held.hold)
    named_yearly = ember.classify_file(monthly, name=YEARLY_NAME)
    assert named_yearly.dataset is None and "2 periods a year" in str(named_yearly.hold)
    no_capacity = [{k: v for k, v in r.items() if k != "Capacity (GW)"} for r in twice]
    fields_nc = [f for f in ["Date" if f == "Year" else f for f in fields] if f != "Capacity (GW)"]
    plain, _ = _parse(_csv(fields_nc, no_capacity))
    assert ember.classify_file(plain, name="ember.csv").dataset == "monthly"
    july, _ = _parse(_dated(month=7))
    ambiguous = ember.classify_file(july, name="ember.csv")
    assert ambiguous.dataset is None and "cannot be told" in str(ambiguous.hold)
    assert ember.classify_file(july, name="ember.csv", declared="yearly").dataset == "yearly"


def test_a_monthly_named_download_that_is_yearly_data_is_held(tmp_path: pathlib.Path) -> None:
    _drop(tmp_path, "release_generation_monthly_global.csv", _dated(), "2026-10-20T10:00:00Z")
    result = _run(Store(tmp_path), key=None)
    assert result.record["status"] == "partial" and result.record["inbox"][0]["state"] == "held"
    assert _checks(result)["inbox_dataset"]["level"] == "hold"
    assert not (tmp_path / "normalized").exists()


def test_a_file_without_a_country_code_column_fails_naming_it(tmp_path: pathlib.Path) -> None:
    fields, rows = _wide()
    keep = [f for f in fields if f not in ("ISO 3 code", "Area type")]
    body = _csv(keep, rows)
    with pytest.raises(ParseError, match="country code column"):
        _parse(body)
    _drop(tmp_path, YEARLY_NAME, body, "2026-10-20T10:00:00Z")
    result = _run(Store(tmp_path), key=None)
    assert result.record["inbox"][0]["state"] == "failed" and result.record["status"] == "partial"
    assert not (tmp_path / "normalized").exists()


@pytest.mark.parametrize("header", ["ISO code", "Code", "Country code"])
def test_other_code_headers_key_countries(header: str) -> None:
    fields, rows = _wide()
    for row in rows:
        row[header] = row.pop("ISO 3 code")
    obs, report = _parse(_csv([header if f == "ISO 3 code" else f for f in fields], rows))
    kinds = {o.area.id: o.area.kind for o in obs}
    assert kinds["US"] == kinds["DE"] == "country" and kinds["world"] == "world"
    assert not report.uncoded_countries


def test_an_alpha2_code_keys_a_country_and_an_unassigned_one_does_not() -> None:
    assert area_for("United States", "US", is_aggregate=None).id == "US"
    assert area_for("United States", "US", is_aggregate=None).iso3 == "USA"
    assert area_for("EU", "EU", is_aggregate=None).kind == "region"


def test_an_answer_with_no_country_is_held_and_uncoded_countries_warn(tmp_path: pathlib.Path) -> None:
    def uncode(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [{**r, "entity_code": None} for r in rows]

    bodies = {path: fixture_bytes(name) for path, name in PATH_FIXTURES.items()}
    bodies[YEARLY] = with_data(YEARLY_JSON, uncode)
    result = _run(Store(tmp_path), FakeSession(bodies))
    found = _checks(result, "yearly/api")
    assert found["country_count"]["level"] == "hold" and found["country_count"]["data"]["now"] == 0
    assert found["entity_code_missing"]["level"] == "warn"
    assert set(found["entity_code_missing"]["data"]["areas"]) == {
        "United States",
        "China",
        "Germany",
        "India",
        "Brazil",
    }
    assert (
        result.record["status"] == "partial" and result.record["datasets"]["yearly/api"]["written"] is False
    )


def test_a_first_download_far_short_of_the_apis_countries_is_held(tmp_path: pathlib.Path) -> None:
    store = Store(tmp_path)
    _run(store)  # the API's yearly answer: five countries
    fields, rows = _wide()
    few = [r for r in rows if r["Area"] in ("World", "Europe", "United States")]
    _drop(tmp_path, YEARLY_NAME, _csv(fields, few), "2026-10-20T10:00:00Z")
    later = _run(store, when=T0 + dt.timedelta(days=14))
    count = _checks(later, "yearly/file")["country_count"]
    assert count["level"] == "hold" and count["data"]["baseline"] == "yearly/api"
    assert (count["data"]["now"], count["data"]["previous"]) == (1, 5)
    assert later.record["inbox"][0]["state"] == "held"


def _months(start: str, end: str) -> list[str]:
    y, m = int(start[:4]), int(start[5:7])
    out = []
    while f"{y:04d}-{m:02d}" <= end:
        out.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


_MONTHLY_AREAS = (("World", None, True), ("United States", "USA", False), ("Germany", "DEU", False))
_NINE = ("Solar", "Wind", "Hydro", "Nuclear", "Gas", "Coal", "Other Renewables", "Bioenergy", "Other Fossil")


class _RollingSession(FakeSession):
    """The monthly answer for exactly the window a request asks for (`start_date` to `latest`), with each
    value fixed by its area, month and source, as Ember's would be between releases."""

    def __init__(self, latest: str) -> None:
        super().__init__()
        self.latest = latest

    def get(self, url: str, *, params: Any = None, **kwargs: Any) -> Any:
        if urlsplit(url).path == MONTHLY and params:
            data = [
                {
                    "entity": name,
                    "entity_code": code,
                    "is_aggregate_entity": aggregate,
                    "date": f"{period}-01",
                    "series": series,
                    "is_aggregate_series": False,
                    "generation_twh": float(10 * (n + 1) + int(period[5:7])),
                    "share_of_generation_pct": 5.0,
                }
                for period in _months(str(params["start_date"]), self.latest)
                for name, code, aggregate in _MONTHLY_AREAS
                for n, series in enumerate(_NINE)
            ]
            self.bodies[MONTHLY] = json.dumps(
                {"stats": {"number_of_records": len(data)}, "data": data}
            ).encode()
        return super().get(url, params=params, **kwargs)


def test_the_monthly_window_rolls_so_january_runs_neither_hold_nor_count_gone(tmp_path: pathlib.Path) -> None:
    assert ember.monthly_start(dt.date(2026, 12, 22)) == "2024-01"
    assert ember.monthly_start(dt.date(2027, 1, 8)) == "2024-02"
    store = Store(tmp_path)
    runs = [
        (dt.datetime(2026, 12, 22, 9, 49, tzinfo=dt.UTC), "2026-09"),
        (dt.datetime(2027, 1, 8, 9, 49, tzinfo=dt.UTC), "2026-10"),
        (dt.datetime(2027, 1, 22, 9, 49, tzinfo=dt.UTC), "2026-11"),
        (dt.datetime(2027, 2, 8, 9, 49, tzinfo=dt.UTC), "2026-12"),
    ]
    first = _run(store, _RollingSession(runs[0][1]), when=runs[0][0])
    assert first.record["datasets"]["monthly/api"]["written"] is True
    for when, latest in runs[1:]:
        result = _run(store, _RollingSession(latest), when=when)
        entry = result.record["datasets"]["monthly/api"]
        assert entry["written"] is True, (when, _checks(result, "monthly/api")["row_count"])
        assert _checks(result, "monthly/api")["row_count"]["level"] == "pass"
        # The month that left the window is not `gone`: only the new month is new.
        assert entry["revisions"] == {"new": 3 * 9 * 2, "changed": 0, "gone": 0}
        assert result.record["status"] == "ok"


def test_the_inbox_is_read_whatever_the_api_answers(tmp_path: pathlib.Path) -> None:
    store = Store(tmp_path)
    _drop(tmp_path, YEARLY_NAME, fixture_bytes(WIDE_CSV), "2026-10-20T10:00:00Z")
    refused = _run(store, FakeSession(statuses={YEARLY: 403}))
    assert refused.record["status"] == "blocked" and refused.record["error_class"] == "KeyRefused"
    assert refused.record["outputs"]["datasets"] == ["yearly/file"]
    assert refused.record["inbox"][0]["state"] == "loaded"
    assert refused.record["requests"] == []  # no API answer kept
    fields, rows = _wide()
    rows[0]["Generation (TWh)"] = "1600.0"  # the next release, revised
    _drop(tmp_path, "second_yearly.csv", _csv(fields, rows), "2026-10-21T10:00:00Z")
    error = HttpFailed(
        "GET https://api.ember-energy.org/v1/electricity-generation/yearly failed after 4 attempts"
    )
    failed = _run(store, FakeSession(errors={YEARLY: error}), when=T0 + dt.timedelta(days=1))
    assert failed.record["status"] == "failed" and failed.record["outputs"]["datasets"] == ["yearly/file"]


def test_a_download_without_a_recorded_time_is_held_and_its_placed_at_time_kept_apart(
    tmp_path: pathlib.Path,
) -> None:
    store = Store(tmp_path)
    path = _drop(tmp_path, YEARLY_NAME, fixture_bytes(WIDE_CSV))  # a sidecar with the review only
    held = _run(store, key=None)
    item = held.record["inbox"][0]
    assert item["state"] == "held" and item["downloaded_at"] is None and item["placed_at"].endswith("Z")
    assert _checks(held)["inbox_download_time"]["level"] == "hold"
    assert not (tmp_path / "normalized").exists()
    sidecar = json.loads(path.with_suffix(".json").read_text())
    path.with_suffix(".json").write_text(json.dumps({**sidecar, "downloaded_at": "2026-10-19T08:00:00Z"}))
    loaded = _run(store, key=None, when=T0 + dt.timedelta(days=1))
    assert loaded.record["inbox"][0]["state"] == "loaded"
    df = pd.read_parquet(tmp_path / "normalized" / SOURCE_ID / f"{loaded.ts}.parquet")
    assert set(df["retrieved_at"]) == {"2026-10-19T08:00:00Z"}


def test_the_first_download_of_a_dataset_waits_for_a_persons_check(tmp_path: pathlib.Path) -> None:
    store = Store(tmp_path)
    body = fixture_bytes(WIDE_CSV)
    path = _drop(tmp_path, YEARLY_NAME, body, "2026-10-20T10:00:00Z", reviewed=False)
    held = _run(store, key=None)
    review = _checks(held, "yearly/file")["first_file_review"]
    sha = hashlib.sha256(body).hexdigest()
    assert review["level"] == "hold" and review["data"]["sha256"] == sha
    summary = review["data"]["summary"]
    assert summary["dataset"] == "yearly" and summary["countries"] == 5 and summary["latest_period"] == "2025"
    assert summary["world_latest"]["period"] == "2025"
    assert summary["world_latest"]["generation_twh"]["solar"] == 2700.0
    assert review["data"]["sidecar"]["reviewed_sha256"] == sha
    assert held.record["datasets"]["yearly/file"]["profile"]["countries"] == 5  # what the person reads
    assert not (tmp_path / "normalized").exists()
    path.with_suffix(".json").write_text(json.dumps(review["data"]["sidecar"]))
    loaded = _run(store, key=None, when=T0 + dt.timedelta(days=1))
    assert loaded.record["status"] == "ok" and loaded.record["outputs"]["datasets"] == ["yearly/file"]
    # Later downloads of the same dataset need no review: the checks compare them with this one.
    fields, rows = _wide()
    rows[0]["Generation (TWh)"] = "1600.0"
    _drop(
        tmp_path,
        "release_generation_yearly_global_2.csv",
        _csv(fields, rows),
        "2026-10-22T10:00:00Z",
        reviewed=False,
    )
    later = _run(store, key=None, when=T0 + dt.timedelta(days=2))
    assert later.record["status"] == "ok" and "first_file_review" not in _checks(later)


def test_the_check_command_prints_what_a_person_reads(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    good = tmp_path / YEARLY_NAME
    good.write_bytes(_dated())
    assert main(["check", str(good), "--downloaded-at", "2026-10-20T10:00:00Z"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True and out["summary"]["dated_years"] is True
    assert out["sidecar"] == {
        "downloaded_at": "2026-10-20T10:00:00Z",
        "dataset": "yearly",
        "reviewed_sha256": hashlib.sha256(_dated()).hexdigest(),
    }
    odd = tmp_path / "release_generation_monthly_global.csv"
    odd.write_bytes(_dated())
    assert main(["check", str(odd)]) == 1
    assert "every period is January" in json.loads(capsys.readouterr().out)["error"]


def test_a_yearly_answer_dating_its_years_is_still_yearly(tmp_path: pathlib.Path) -> None:
    bodies = {path: fixture_bytes(name) for path, name in PATH_FIXTURES.items()}
    bodies[YEARLY] = with_data(YEARLY_JSON, lambda rows: [{**r, "date": f"{r['date']}-01-01"} for r in rows])
    result = _run(Store(tmp_path), FakeSession(bodies))
    assert result.record["status"] == "ok" and result.record["datasets"]["yearly/api"]["written"] is True
    df = pd.read_parquet(tmp_path / "normalized" / SOURCE_ID / f"{result.ts}.parquet")
    yearly = df[df["dataset"] == "yearly"]
    assert set(yearly["period"]) == {"2023", "2024", "2025"} and yearly["month"].isna().all()
    assert len(yearly) == result.record["datasets"]["yearly/api"]["rows"] == 560


def test_an_older_download_never_replaces_a_newer_one_loaded(tmp_path: pathlib.Path) -> None:
    store = Store(tmp_path)
    fields, rows = _wide()
    old = _csv(fields, rows)
    rows[0]["Generation (TWh)"] = "1600.0"  # the newer release
    new = _csv(fields, rows)
    _drop(tmp_path, "release_generation_yearly_global_a.csv", old, "2026-10-20T10:00:00Z")
    _drop(tmp_path, "release_generation_yearly_global_b.csv", new, "2026-10-21T10:00:00Z")
    first = _run(store, key=None)
    assert [i["state"] for i in first.record["inbox"]] == ["superseded", "loaded"]
    # The next run must not take the superseded (older) file as the dataset's newest download.
    second = _run(store, key=None, when=T0 + dt.timedelta(days=14))
    assert [i["state"] for i in second.record["inbox"]] == ["older_than_loaded", "already_loaded"]
    assert second.record["status"] == "blocked" and not second.record["outputs"].get("datasets")
    # An old file copied in again later, under a new name, is skipped the same way.
    _drop(tmp_path, "copied_yearly_again.csv", old + b"\n", "2026-10-19T09:00:00Z")
    third = _run(store, key=None, when=T0 + dt.timedelta(days=15))
    assert third.record["inbox"][0]["state"] == "older_than_loaded"
    assert sorted(p.stem for p in (tmp_path / "normalized" / SOURCE_ID).iterdir()) == [first.ts]
