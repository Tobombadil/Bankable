"""us.eia.860m.history: the parser on two real monthly workbooks (July 2015, the oldest layout, and
September 2026, the current one) and every route a month can come by, with recorded fixtures only."""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import io
import json
import shutil
import zipfile

import pytest

from pipeline.connectors.history import connector_class_for
from pipeline.connectors.history import runner as runner_module
from pipeline.connectors.history.base import MonthFile
from pipeline.connectors.history.conftest import (
    EIA_FIXTURES,
    RECORDED_AT,
    FakeHttp,
    clock,
    fixture_bytes,
    stored_run,
    zip_of,
)
from pipeline.connectors.history.documents import MonthlyStore
from pipeline.connectors.history.dq import check_month
from pipeline.connectors.history.eia860m.connector import (
    archive_url,
    find_xlsx_links,
    period_from_name,
    period_from_url,
    status_code,
    technology_token,
)
from pipeline.connectors.history.runner import MAX_ITEM_FAILURES, effective_mode, run
from pipeline.connectors.registry import Registry

SID = "us.eia.860m.history"
CURRENT = EIA_FIXTURES / "september_generator2026_sample.xlsx"
OLD = EIA_FIXTURES / "eia860m-2015-07_sample.xlsx"
INDEX = EIA_FIXTURES / "eia860m_index_sample.html"
XLS = "https://www.eia.gov/electricity/data/eia860m/xls/"


def _month(content: bytes, hint: str | None = None, route: str = "inbox") -> MonthFile:
    return MonthFile(
        content=content,
        source_url="https://example.invalid/file.xlsx",
        retrieved_at=RECORDED_AT,
        route=route,
        item_key="test",
        period_hint=hint,
    )


@pytest.fixture()
def connector(registry, store):
    cls = connector_class_for(SID)
    return cls(registry.get(SID), FakeHttp({}), store=store, clock=clock())


def _placeholder(**_: object) -> tuple[int, bytes, dict[str, str]]:
    return 200, b"<html>coming soon</html>", {"Content-Type": "text/html"}


def _origin_routes(etag: str = '"v1"') -> dict[str, object]:
    def current(headers: dict[str, str], **_: object) -> tuple[int, bytes, dict[str, str]]:
        if headers.get("If-None-Match") == etag:
            return 304, b"", {"ETag": etag}
        return 200, fixture_bytes(CURRENT), {"ETag": etag, "Last-Modified": "Fri, 23 Oct 2026 15:32:18 GMT"}

    return {
        "september_generator2026.xlsx": current,
        "generator2026.xlsx": _placeholder,
        "eia860m/": str(INDEX),
    }


# --------------------------------------------------------------------------------- the parser
def test_the_current_layout_tallies_every_sheet(connector) -> None:
    out = connector.summarise(_month(fixture_bytes(CURRENT)))
    assert out["period"] == "2026-09"
    layout = out["layout"]
    assert layout["header_rows"] == {"operating": 2, "planned": 2, "retired": 2, "canceled_postponed": 2}
    assert layout["nameplate_stated"] is True and layout["puerto_rico_sheets"] is True
    assert layout["title"] == "Inventory of Operating Generators as of September 2026"
    blocks = out["blocks"]
    assert set(blocks) == {"operating", "planned", "retired", "canceled_postponed", "planned_retirements"}
    operating = blocks["operating"]
    assert operating["basis"] == "eia860m/operating/net-summer-mw/50-states-dc"
    assert operating["total"] == {
        "count": 93,
        "mw": 14103.9,
        "mw_stated": 90,
        "nameplate_mw": 15570.4,
        "nameplate_mw_stated": 93,
    }
    assert set(operating["by_status"]) == {"OP", "SB", "OS", "OA"}
    # every row of the 50 states and DC is in exactly one state bucket
    assert sum(t["count"] for t in operating["by_state"].values()) == operating["total"]["count"]
    assert blocks["retired"]["basis"].endswith("/retired-since-2002")
    planned = blocks["planned"]
    assert set(planned["by_status"]) <= {"P", "L", "T", "U", "V", "TS", "OT"}
    assert "2026" in planned["by_expected_year"] and "_unstated" not in planned["by_expected_year"]
    # Puerto Rico rows on a main sheet are left out of the national figure, and counted
    assert blocks["canceled_postponed"]["excluded"] == {
        "puerto_rico": {"count": 1, "mw": 55.0, "mw_stated": 1}
    }
    assert out["dq_facts"]["capacity_missing"]["operating"] == 3
    assert out["dq_facts"]["unknown_status"] == {}
    # one Planned row (Willow Rock, an air-storage plant) states no Technology: `unknown`, and warned
    assert out["dq_facts"]["unmapped_technology"] == ["(blank)"]
    assert out["dq_facts"]["technology_blank"] == {
        "operating": 0,
        "planned": 1,
        "retired": 0,
        "canceled_postponed": 0,
    }
    assert blocks["planned"]["by_technology"]["unknown"]["count"] == 1
    assert out["dq_facts"]["technology_blank_mw"]["planned"] == 125.0
    warned = [(c["check"], c["block"]) for c in check_month(out, None)["checks"]]
    assert warned == [("technology_blank", "planned")]  # 125 MW: enough to move a technology line
    # the Retired sheet since March 2017 lists every retirement since 2002: a list that only grows
    assert out["dq_facts"]["cumulative"] == {"retired": {"dimension": "retirement_year", "open_from": "2025"}}


def test_the_oldest_layout_reads_the_same_measure(connector) -> None:
    out = connector.summarise(_month(fixture_bytes(OLD), hint="2015-07"))
    assert out["period"] == "2015-07"
    assert out["layout"]["header_rows"]["operating"] == 1
    assert out["layout"]["nameplate_stated"] is False
    operating = out["blocks"]["operating"]
    # the same basis as 2026: net summer MW, 50 states and DC, so the two months compare
    assert operating["basis"] == "eia860m/operating/net-summer-mw/50-states-dc"
    assert operating["total"] == {"count": 99, "mw": 15456.9, "mw_stated": 95}
    assert "nameplate_mw" not in operating["total"]  # absent, never zero
    # before March 2017 the Retired sheet lists recent retirements only
    assert out["blocks"]["retired"]["basis"].endswith("/retired-partial")
    assert out["blocks"]["canceled_postponed"]["excluded"]["outside_50_states_dc"]["count"] == 1
    assert out["source_url"] == archive_url("2015-07")


#: The Retired sheet's header and three rows of the real August 2026 workbook (`august_generator2026.xlsx`,
#: rows 3, Indian Point 1 and Shoreham, verbatim) and its trailing blank and note rows.
RETIRED_HEADER = [
    "Entity ID", "Entity Name", "Plant ID", "Plant Name", "Google Map", "Bing Map", "Plant State", "County",
    "Balancing Authority Code", "Sector", "Generator ID", "Unit Code", "Nameplate Capacity (MW)",
    "Net Summer Capacity (MW)", "Net Winter Capacity (MW)", "Technology", "Energy Source Code",
    "Prime Mover Code", "Operating Month", "Operating Year", "Retirement Month", "Retirement Year",
    "Nameplate Energy Capacity (MWh)", "DC Net Capacity (MW)", "Latitude", "Longitude",
]  # fmt: skip
RETIRED_ROWS = [
    (5860, "Empire District Electric Co", 1239, "Riverton", "Map", "Map", "KS", "Cherokee", "SWPP",
     "Electric Utility", "10", "", 16.3, 13, 16, "Natural Gas Fired Combustion Turbine", "NG", "GT", 12,
     1988, 8, 2026, " ", " ", 37.072616, -94.6987),
    (" ", "Entergy Nuclear", " ", "Indian Point 1", "", "", "NY", "Westchester", "NYIS", "IPP Non-CHP", "1",
     "", 277, 257, 257, "Nuclear", "NUC", "ST", 10, 1962, 10, 1974, " ", " ", " ", " "),
    (" ", "Long Island Power Authority", " ", "Shoreham", "", "", "NY", "Suffolk", "", "", "1", "", 849,
     820, 820, "Nuclear", "NUC", "ST", " ", " ", 6, 1989, " ", " ", " ", " "),
    ("",) + (None,) * 25,
    ("NOTES:\nCapacity from facilities with a total generator nameplate capacity less than 1 MW are "
     "excluded from this report.",) + (None,) * 25,
]  # fmt: skip


def test_generators_listed_without_a_plant_id_are_left_out_and_counted(connector) -> None:
    """EIA lists 12 early nuclear units on the August 2026 Retired sheet with a blank Plant ID. They
    have no key to count by, so they stay out of the tallies, but they are counted under `excluded`
    rather than dropped like the trailing note rows."""
    summary = connector._tally_sheet("retired", RETIRED_HEADER, iter(RETIRED_ROWS))
    block = summary.blocks["retired"].as_dict()
    assert summary.rows == 1 and block["total"]["count"] == 1
    assert block["excluded"] == {"no_plant_id": {"count": 2, "mw": 1077.0, "mw_stated": 2}}


def test_eia_labels_the_shared_classifier_misreads_are_read_by_label() -> None:
    assert technology_token("Solar Thermal without Energy Storage") == "solar_thermal"
    assert technology_token("Coal Integrated Gasification Combined Cycle") == "coal"
    assert technology_token("Flywheels") == "storage"
    assert technology_token("Solar Photovoltaic") == "solar"
    assert technology_token(None) == "unknown"


def test_status_codes_and_periods() -> None:
    assert status_code("(OP) Operating") == "OP"
    assert status_code("(TS) Construction complete, but not yet in commercial operation") == "TS"
    assert status_code(" ") is None
    assert period_from_url(XLS + "september_generator2026.xlsx") == "2026-09"
    assert period_from_name("eia860m-2015-07.xlsx") == "2015-07"
    assert period_from_name("july_generator2015.xlsx") == "2015-07"
    assert period_from_name("notes.xlsx") is None
    assert archive_url("2015-07").endswith("/archive/xls/july_generator2015.xlsx")


def test_a_workbook_without_its_sheets_is_a_parse_error(connector) -> None:
    from pipeline.connectors.base import ParseError

    with pytest.raises(ParseError):
        connector.summarise(_month(b"<html>placeholder</html>"))


def test_the_index_links_drop_archive_paths() -> None:
    links = find_xlsx_links(INDEX.read_text())
    assert links[:3] == [
        XLS + "december_generator2026.xlsx",
        XLS + "october_generator2026.xlsx",
        XLS + "september_generator2026.xlsx",
    ]
    assert not [u for u in links if "/archive/" in u]


# ---------------------------------------------------------------------------------- routes
def test_origin_reads_the_newest_month_and_asks_conditionally_next_time(registry, store) -> None:
    http = FakeHttp(_origin_routes())
    first = run(SID, "monthly", store=store, http=http, registry=registry, now=clock())
    assert first.record["status"] == "ok"
    assert first.record["documents"]["written"] == ["2026-09"]
    assert first.record["origin"]["etag"] == '"v1"'
    doc = MonthlyStore(store.base_root).load(SID, "2026-09")
    assert doc["source_url"] == XLS + "september_generator2026.xlsx"
    assert doc["retrieved_from"]["route"] == "origin"
    assert doc["licence_id"] == registry.get(SID).licence_id
    assert doc["credit"] == "Source: U.S. Energy Information Administration"
    assert store.backend.exists(doc["raw"]["snapshot"])
    # placeholders answered HTML first; nothing under /archive/ was asked (FakeHttp fails on it)
    assert [c["url"].rsplit("/", 1)[-1] for c in http.calls] == [
        "",
        "december_generator2026.xlsx",
        "october_generator2026.xlsx",
        "september_generator2026.xlsx",
    ]

    again = FakeHttp(_origin_routes())
    second = run(SID, "monthly", store=store, http=again, registry=registry, now=clock())
    assert second.record["status"] == "unchanged"
    asked = [c for c in again.calls if c["url"].endswith("september_generator2026.xlsx")]
    assert asked[0]["headers"].get("If-None-Match") == '"v1"'
    assert second.record["bytes"] == 0  # the 304 read the stored snapshot


def test_shared_snapshots_of_the_register_connector_are_read_without_a_request(registry, store) -> None:
    key = stored_run(store, "us.eia.860m", fixture_bytes(OLD), url=XLS + "july_generator2015.xlsx")
    http = FakeHttp({"generator2026.xlsx": _placeholder, "eia860m/": str(INDEX)})
    result = run(SID, "monthly", store=store, http=http, registry=registry, now=clock())
    assert "2015-07" in result.record["documents"]["written"]
    doc = MonthlyStore(store.base_root).load(SID, "2015-07")
    assert doc["retrieved_from"]["route"] == "shared"
    assert doc["raw"]["snapshot"] == key  # the register's object, not a copy
    assert not [c for c in http.calls if "july" in c["url"]]


def test_a_persons_download_in_the_inbox_is_read_once(registry, store) -> None:
    inbox = store.base_root / "inbox" / SID
    inbox.mkdir(parents=True)
    shutil.copyfile(OLD, inbox / "july_generator2015.xlsx")
    http = FakeHttp({"generator2026.xlsx": _placeholder, "eia860m/": str(INDEX)})
    first = run(SID, "monthly", store=store, http=http, registry=registry, now=clock())
    assert "2015-07" in first.record["documents"]["written"]
    doc = MonthlyStore(store.base_root).load(SID, "2015-07")
    assert doc["retrieved_from"]["route"] == "inbox"
    assert doc["source_url"] == archive_url("2015-07")
    second = run(SID, "monthly", store=store, http=FakeHttp({}), registry=registry, now=clock())
    assert not [i for i in second.record["items"] if i["route"] == "inbox"]


@pytest.fixture()
def mirror_on() -> Registry:
    """The manifest with its mirror block switched on (`enabled: true`), as shipped since 2026-10-24."""
    return _with_mirror(enabled=True)


@pytest.fixture()
def mirror_off() -> Registry:
    """The manifest with its mirror block switched off, as it shipped until 2026-10-24."""
    return _with_mirror(enabled=False)


def _with_mirror(*, enabled: bool) -> Registry:
    fresh = Registry()
    entry = fresh.get(SID)
    mirror = {**entry.raw["mirror"], "enabled": enabled}
    fresh.sources[SID] = dataclasses.replace(entry, raw={**entry.raw, "mirror": mirror})
    return fresh


def test_the_mirror_is_on_as_shipped_and_switching_it_on_backfills_again(
    registry, store, mirror_on, mirror_off
) -> None:
    """The owner recorded the mirror's use on 2026-10-24 ("Load EIA history back to 2015 from the public
    Zenodo copy: yes"), so the shipped manifest has it on. With it off, a backfill ends on the standing
    routes and never asks Zenodo; switching it on makes the next `auto` run a backfill again (its
    `backfill_key` names the record), so nobody has to run one by hand."""
    assert registry.get(SID).raw["mirror"]["enabled"] is True
    registry = mirror_off
    zipped = zip_of({"eia860m-2015-07.xlsx": fixture_bytes(OLD)})

    def routes() -> FakeHttp:
        return FakeHttp({**_origin_routes(), **_mirror_routes(zipped)})

    http = routes()
    first = run(SID, "auto", store=store, http=http, registry=registry, now=clock())
    assert first.record["mode"] == "backfill" and first.record["backfill_key"] is None
    assert not [c for c in http.calls if "zenodo" in c["url"]]
    assert first.record["status"] == "ok" and first.record["documents"]["written"] == ["2026-09"]
    again = run(SID, "auto", store=store, http=routes(), registry=registry, now=clock())
    assert again.record["mode"] == "monthly"
    on = run(SID, "auto", store=store, http=routes(), registry=mirror_on, now=clock())
    assert on.record["mode"] == "backfill"
    assert on.record["backfill_key"] == "mirror:10.5281/zenodo.22995197"
    assert on.record["documents"]["written"] == ["2015-07"]
    after = run(SID, "auto", store=store, http=routes(), registry=mirror_on, now=clock())
    assert after.record["mode"] == "backfill"  # the 2016 zip still answers 404: partial, so it tries again
    done = [{**after.record, "status": "ok"}]
    assert effective_mode("auto", done, "mirror:10.5281/zenodo.22995197") == "monthly"


def _mirror_routes(zip_bytes: bytes) -> dict[str, object]:
    package = json.loads(fixture_bytes(EIA_FIXTURES / "zenodo_datapackage.json"))
    for resource in package["resources"]:
        if resource["name"] == "eia860m-2015.zip":
            resource["hash"] = hashlib.md5(zip_bytes).hexdigest()  # noqa: S324 -- the mirror's own checksum
            resource["parts"]["year_month"] = ["2015-07"]
    package["resources"] = [
        r for r in package["resources"] if r["name"] in ("eia860m-2015.zip", "eia860m-2016.zip")
    ]
    return {
        "datapackage.json": json.dumps(package).encode(),
        "eia860m-2015.zip": zip_bytes,
        "generator2026.xlsx": _placeholder,
        "eia860m/": str(INDEX),
    }


def test_the_mirror_backfills_and_a_missing_year_keeps_what_was_read(mirror_on, store) -> None:
    """The mirror's 2015 zip (a container built here around the recorded July 2015 workbook) is read;
    its 2016 zip answers 404, so that item fails, the run is partial and the month read stays."""
    zipped = zip_of({"eia860m-2015-07.xlsx": fixture_bytes(OLD)})
    http = FakeHttp(_mirror_routes(zipped))
    result = run(SID, "backfill", store=store, http=http, registry=mirror_on, now=clock())
    assert result.record["status"] == "partial"
    assert result.record["documents"]["written"] == ["2015-07"]
    items = {i["key"]: i["status"] for i in result.record["items"]}
    assert items["mirror:eia860m-2015.zip"] == "ok"
    assert items["mirror:eia860m-2016.zip"] == "failed"
    doc = MonthlyStore(store.base_root).load(SID, "2015-07")
    assert doc["retrieved_from"]["route"] == "mirror"
    assert doc["retrieved_from"]["doi"] == "10.5281/zenodo.22995197"
    assert doc["retrieved_from"]["member"] == "eia860m-2015-07.xlsx"
    assert doc["source_url"] == archive_url("2015-07")  # EIA's address, not the mirror's
    downloads = [c for c in http.calls if c["url"].endswith(".zip?download=1")]
    assert len(downloads) == 2
    # a monthly run never reads the mirror
    later = run(
        SID, "monthly", store=store, http=FakeHttp(_mirror_routes(zipped)), registry=mirror_on, now=clock()
    )
    assert not [i for i in later.record["items"] if i["route"] == "mirror"]


def test_a_mirror_copy_never_replaces_the_publishers_own(mirror_on, store) -> None:
    stored_run(store, "us.eia.860m", fixture_bytes(OLD), url=XLS + "july_generator2015.xlsx")
    run(SID, "monthly", store=store, http=FakeHttp({"eia860m/": str(INDEX)}), registry=mirror_on, now=clock())
    other = fixture_bytes(OLD) + b"\0"  # different bytes, same month: a mirror copy that differs
    zipped = zip_of({"eia860m-2015-07.xlsx": other})
    routes = _mirror_routes(zipped)
    result = run(SID, "backfill", store=store, http=FakeHttp(routes), registry=mirror_on, now=clock())
    # the 2015 zip holds only months already held, so it is not even downloaded
    assert not [i for i in result.record["items"] if i["key"] == "mirror:eia860m-2015.zip"]
    assert MonthlyStore(store.base_root).load(SID, "2015-07")["retrieved_from"]["route"] == "shared"


def test_an_md5_mismatch_is_a_failed_item_and_writes_nothing(mirror_on, store) -> None:
    zipped = zip_of({"eia860m-2015-07.xlsx": fixture_bytes(OLD)})
    routes = _mirror_routes(zipped)
    routes["eia860m-2015.zip"] = zipped + b"tampered"
    result = run(SID, "backfill", store=store, http=FakeHttp(routes), registry=mirror_on, now=clock())
    assert "2015-07" not in result.record["documents"]["written"]
    failed = next(i for i in result.record["items"] if i["key"] == "mirror:eia860m-2015.zip")
    assert failed["status"] == "failed" and "md5" in failed["error"]


def _rezipped(content: bytes, compression: int, level: int | None = None) -> bytes:
    """The same workbook in other bytes (its parts recompressed): another copy of one file."""
    buf = io.BytesIO()
    with (
        zipfile.ZipFile(io.BytesIO(content)) as source,
        zipfile.ZipFile(buf, "w", compression=compression, compresslevel=level) as out,
    ):
        for info in source.infolist():
            out.writestr(info.filename, source.read(info))
    return buf.getvalue()


def test_an_older_copy_of_a_month_never_replaces_a_later_one(registry, store, monkeypatch) -> None:
    """EIA re-issues September: the register connector's copy from before this source's fetch is
    skipped unparsed every week, its copy of the re-issue replaces the month, and the origin's
    validators move on to the re-issue although that run wrote nothing from the origin."""
    first = fixture_bytes(CURRENT)
    reissue = _rezipped(first, zipfile.ZIP_DEFLATED, 1)
    served = {"body": first, "etag": '"v1"'}

    def september(headers: dict[str, str], **_: object) -> tuple[int, bytes, dict[str, str]]:
        if headers.get("If-None-Match") == served["etag"]:
            return 304, b"", {"ETag": str(served["etag"])}
        return 200, bytes(served["body"]), {"ETag": str(served["etag"])}

    routes = {
        "september_generator2026.xlsx": september,
        "generator2026.xlsx": _placeholder,
        "eia860m/": str(INDEX),
    }
    parses: list[str] = []
    real = runner_module.summarise

    def counting(connector, month, **kwargs):  # type: ignore[no-untyped-def]
        parses.append(month.route)
        return real(connector, month, **kwargs)

    monkeypatch.setattr(runner_module, "summarise", counting)
    at = dt.datetime(2026, 10, 23, 22, 0, tzinfo=dt.UTC)

    def week(n: int, http: FakeHttp | None = None):  # type: ignore[no-untyped-def]
        when = clock(at + dt.timedelta(days=7 * n))
        return run(SID, "monthly", store=store, http=http or FakeHttp(routes), registry=registry, now=when)

    week(0)
    september_url = XLS + "september_generator2026.xlsx"
    stored_run(
        store,
        "us.eia.860m",
        _rezipped(first, zipfile.ZIP_STORED),
        url=september_url,
        at=at - dt.timedelta(days=3),
    )
    parses.clear()
    second = week(1)
    assert second.record["status"] == "unchanged" and parses == []
    shared = next(i for i in second.record["items"] if i["route"] == "shared")
    assert shared["months"] == [
        {"period": "2026-09", "outcome": "skipped", "kept": "origin, retrieved later"}
    ]
    assert MonthlyStore(store.base_root).load(SID, "2026-09")["retrieved_from"]["route"] == "origin"

    # EIA re-issues the file, and the register connector stores it before this source's next tick
    served.update(body=reissue, etag='"v2"')
    stored_run(store, "us.eia.860m", reissue, url=september_url, at=at + dt.timedelta(days=9))
    third = week(2)
    assert third.record["documents"]["replaced"] == ["2026-09"] and parses == ["shared"]
    doc = MonthlyStore(store.base_root).load(SID, "2026-09")
    assert doc["raw"]["sha256"] == hashlib.sha256(reissue).hexdigest()
    assert third.record["origin"]["etag"] == '"v2"'  # the origin served the re-issue: unchanged, remembered

    http = FakeHttp(routes)
    fourth = week(3, http)
    assert fourth.record["status"] == "unchanged" and parses == ["shared"]
    asked = [c for c in http.calls if c["url"] == september_url]
    assert asked[0]["headers"].get("If-None-Match") == '"v2"'


def test_the_index_is_never_parked_by_its_failures(registry, store) -> None:
    """A file that failed three runs running is skipped until an operator asks; EIA's index page is
    the route every new month comes by, so an outage of it is retried every run."""
    for _ in range(MAX_ITEM_FAILURES + 1):
        result = run(SID, "monthly", store=store, http=FakeHttp({}), registry=registry, now=clock())
        [origin] = [i for i in result.record["items"] if i["route"] == "origin"]
        assert origin["status"] == "failed" and result.record["status"] == "failed"


def test_a_restatement_whose_snapshot_is_gone_reads_another_stored_copy(registry, store, monkeypatch) -> None:
    """The month's own snapshot is gone, but `us.eia.860m` stored the same bytes: the restatement
    reads that copy (checked by sha256) without a request, instead of failing and leaving the month
    under its old parser."""
    first = run(SID, "monthly", store=store, http=FakeHttp(_origin_routes()), registry=registry, now=clock())
    assert first.record["documents"]["written"] == ["2026-09"]
    monthly = MonthlyStore(store.base_root)
    own = monthly.load(SID, "2026-09")["raw"]["snapshot"]
    shared = stored_run(
        store, "us.eia.860m", fixture_bytes(CURRENT), url=XLS + "september_generator2026.xlsx"
    )
    store.backend.delete(own)
    monkeypatch.setattr(connector_class_for(SID), "version", classmethod(lambda _cls: "9.9.9+restated"))
    http = FakeHttp({"eia860m/": str(INDEX), "generator2026.xlsx": _placeholder})
    result = run(SID, "monthly", store=store, http=http, registry=registry, now=clock())
    assert "2026-09" in result.record["documents"]["replaced"]
    doc = monthly.load(SID, "2026-09")
    assert doc["parser_version"] == "9.9.9+restated" and doc["raw"]["snapshot"] == shared
    assert doc["retrieved_from"]["restated_from"] == shared
    [restate] = [i for i in result.record["items"] if i["route"] == "restate"]
    assert restate["status"] == "ok"  # read from the store: the origin item's requests are its own


def test_a_304_keeps_the_validators_it_does_not_repeat(registry, store) -> None:
    """EIA's 304 repeats the ETag but not Last-Modified, and asked with If-None-Match alone its server
    answers 200 and the whole workbook (measured 2026-10-24: 14,013,970 bytes for an unchanged
    September). The validators a 304 omits stay valid, so every later request carries both."""
    etag, modified = '"0ad2b0363dd1:0"', "Fri, 23 Oct 2026 15:32:18 GMT"

    def september(headers: dict[str, str], **_: object) -> tuple[int, bytes, dict[str, str]]:
        if headers.get("If-None-Match") == etag and headers.get("If-Modified-Since") == modified:
            return 304, b"", {"ETag": etag}
        return 200, fixture_bytes(CURRENT), {"ETag": etag, "Last-Modified": modified}

    routes = {
        "september_generator2026.xlsx": september,
        "generator2026.xlsx": _placeholder,
        "eia860m/": str(INDEX),
    }
    runs = [
        run(SID, "monthly", store=store, http=FakeHttp(routes), registry=registry, now=clock())
        for _ in range(3)
    ]
    assert runs[0].record["bytes"] > 0
    assert [r.record["bytes"] for r in runs[1:]] == [0, 0]  # a 304 each time, never the workbook again
    assert runs[2].record["origin"]["last_modified"] == modified
