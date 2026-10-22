"""Tests for the curated-issuer RFP connector (`pipeline/connectors/us_rfp`) on recorded fixtures.

`fixtures/README.md` lists what each file is. `fetch()` runs here only against `FakeSession`, a
stand-in for `requests.Session` under the real `PoliteSession`, so robots.txt, the challenge check,
retries and pacing are the production code; nothing leaves the machine (conftest's socket guard).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import pathlib
from collections.abc import Callable
from typing import Any

import pandas as pd
import pytest
import yaml

from pipeline.connectors import registry as registry_module
from pipeline.connectors.base import (
    BlockedError,
    ConnectorError,
    GateViolation,
    ParseError,
    PreviousSnapshot,
    RawSnapshot,
)
from pipeline.connectors.http import PoliteSession
from pipeline.connectors.opportunity import OPPORTUNITY_STATES
from pipeline.connectors.registry import RegistrationError, Registry, SourceEntry
from pipeline.connectors.runner import run
from pipeline.connectors.store import Store
from pipeline.connectors.us_rfp import classify as clf
from pipeline.connectors.us_rfp import connector as module
from pipeline.connectors.us_rfp import extract, htmltree
from pipeline.connectors.us_rfp.issuers import IssuerRow, fetchable_ids, registry_rows
from services import posture

FIX = pathlib.Path(__file__).with_name("fixtures")
NOW = dt.datetime(2026, 10, 21, 16, 0, tzinfo=dt.UTC)
AEP = ["us.rfp.pso", "us.rfp.appalachian_power", "us.rfp.swepco", "us.rfp.indiana_michigan_power"]
TRANCHE_1 = [*AEP, "us.rfp.ct_deep", "us.rfp.nj_bpu"]
SLUG = {sid: sid.removeprefix("us.rfp.") for sid in TRANCHE_1}
PSO_LISTING = "https://www.psoklahoma.com/business/b2b/energy-rfps/"
CT_2026 = "https://portal.ct.gov/deep/news-releases/news-releases---2026"
NJ_INDEX = "https://www.nj.gov/bpu/newsroom/public/"
GSESP_PDF = (
    "https://www.nj.gov/bpu/pdf/publicnotice/Public%20Notice%20-%20GSESP%20Tranche%202%20Extension.pdf"
)


# =============================================================================== helpers
class FakeResponse:
    def __init__(self, url: str, status: int, body: bytes, headers: dict[str, str]) -> None:
        self.url = url
        self.status_code = status
        self.content = body
        self.headers = headers

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", "replace")

    def close(self) -> None:
        pass


Route = tuple[int, bytes, dict[str, str]] | Callable[[dict[str, str]], tuple[int, bytes, dict[str, str]]]


class FakeSession:
    """A `requests.Session` that answers from a route table (404 for anything else) and records
    every request with its headers."""

    def __init__(self, routes: dict[str, Route]) -> None:
        self.routes = routes
        self.headers: dict[str, str] = {}
        self.calls: list[tuple[str, dict[str, str]]] = []

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        return self.request("GET", url, **kwargs)

    def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        sent = {**self.headers, **(kwargs.get("headers") or {})}
        self.calls.append((url, sent))
        route = self.routes.get(url)
        if route is None:
            return FakeResponse(url, 404, b"not found", {})
        status, body, headers = route(sent) if callable(route) else route
        return FakeResponse(url, status, body, headers)

    def urls(self) -> list[str]:
        return [u for u, _ in self.calls]


def page(name: str) -> tuple[int, bytes, dict[str, str]]:
    return 200, (FIX / name).read_bytes(), {"Content-Type": "text/html; charset=utf-8"}


def terms_page(sid: str, drop: int | None = None) -> tuple[int, bytes, dict[str, str]]:
    """A terms page carrying the row's quoted clauses (one left out when `drop` names it)."""
    quotes = [q for i, q in enumerate(registry_rows()[sid].terms_quotes) if i != drop]
    body = "<html><body><main>" + "".join(f"<p>{q}</p>" for q in quotes) + "</main></body></html>"
    return 200, body.encode("utf-8"), {"Content-Type": "text/html; charset=utf-8"}


def make(sid: str, routes: dict[str, Route]) -> tuple[module.UtilityRfpConnector, FakeSession]:
    reg = Registry()
    session = FakeSession(routes)
    http = PoliteSession(session=session, sleep=lambda _s: None)  # type: ignore[arg-type]
    cls = reg.connector_class(sid)
    c = cls(reg.get(sid), http=http)
    c.clock = lambda: NOW
    return c, session


def bundle(sid: str) -> dict[str, Any]:
    return dict(json.loads((FIX / f"bundle_{SLUG[sid]}_20261021.json").read_text(encoding="utf-8")))


def raw_of(sid: str, content: bytes | None = None, at: dt.datetime = NOW) -> RawSnapshot:
    body = content if content is not None else (FIX / f"bundle_{SLUG[sid]}_20261021.json").read_bytes()
    return RawSnapshot(
        content=body,
        content_type="application/json",
        url=registry_rows()[sid].listing_url.replace("{year}", "2026"),
        retrieved_at=at,
        http_status=200,
        ext="json",
    )


def frame(sid: str, at: dt.datetime = NOW) -> pd.DataFrame:
    reg = Registry()
    c = reg.connector_class(sid)(reg.get(sid))
    raw = raw_of(sid, at=at)
    return c.normalize(c.parse(raw), raw)


def previous(content: bytes, meta: dict[str, Any], at: dt.datetime) -> PreviousSnapshot:
    record = {
        "id": "prev",
        "snapshot": {
            "sha256": hashlib.sha256(content).hexdigest(),
            "meta": meta,
            "retrieved_at": at.isoformat(),
        },
    }
    return PreviousSnapshot(record=record, load=lambda: content)


def recent_terms(sid: str) -> dict[str, Any]:
    return {
        "terms_url": registry_rows()[sid].terms_url,
        "checked_at": NOW.isoformat(),
        "quotes": 1,
        "missing": [],
    }


def region_of(name: str, sid: str, role: str) -> str | None:
    row = registry_rows()[sid]
    start, ends = extract._region_spec(row, role)
    return htmltree.region((FIX / name).read_text(encoding="utf-8"), start, ends)


# =============================================================================== registry and gate
def test_the_registry_clears_exactly_tranche_one() -> None:
    """docs/28 §8.1: six cleared issuers with a listing; ODEC is cleared but has none."""
    assert sorted(fetchable_ids()) == sorted(TRANCHE_1)
    assert "no connector block" in str(registry_rows()["us.rfp.odec"].refusal())


@pytest.mark.parametrize("sid", sorted(set(registry_rows()) - set(TRANCHE_1)))
def test_every_other_registry_row_gets_no_connector_class(sid: str) -> None:
    """A `not_allowed` row, a link-out-only row (tranche 1b) and ODEC: no class, so no fetch path."""
    with pytest.raises(RegistrationError, match="does not clear"):
        module.connector_class_for(sid)


def test_an_unknown_id_gets_no_connector_class() -> None:
    with pytest.raises(RegistrationError, match="not in"):
        module.connector_class_for("us.rfp.no_such_issuer")


def _row(**overrides: Any) -> IssuerRow:
    raw = dict(registry_rows()["us.rfp.ct_deep"].raw)
    raw["connector"] = dict(raw["connector"])
    for key, value in overrides.items():
        if key.startswith("connector."):
            raw["connector"][key.split(".", 1)[1]] = value
        elif value is None:
            raw.pop(key, None)
        else:
            raw[key] = value
    return IssuerRow(id="us.rfp.test", name="Test", raw=raw)


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"automated_reading": None}, "automated_reading=None"),
        ({"automated_reading": "Allowed"}, "automated_reading='Allowed'"),
        ({"automated_reading": "not_allowed"}, "automated_reading"),
        ({"verdict": "link_out_only"}, "verdict"),
        ({"verdict": "manual_only"}, "verdict"),
        ({"manifest": {"reuse": "attribution", "publication": "none"}}, "publication is none"),
        ({"connector": None}, "no connector block"),
        ({"connector.extractor": "browser"}, "extractor"),
        ({"connector.listing_url": "http://portal.ct.gov/x"}, "not https"),
        ({"connector.listing_url": "https://www.dpuc.state.ct.us/x"}, "link-only"),
        ({"connector.issuer": ""}, "issuer"),
        ({"connector.issuer_type": "bank"}, "issuer_type"),
        ({"connector.kind": "news"}, "kind"),
    ],
)
def test_the_gate_fails_closed(overrides: dict[str, Any], reason: str) -> None:
    refusal = _row(**overrides).refusal()
    assert refusal is not None and reason in refusal
    assert _row().refusal() is None


def test_a_refused_row_wired_into_the_manifest_is_never_fetched(tmp_path: pathlib.Path) -> None:
    """Even a manifest row pointing a `not_allowed` issuer at this module gets no connector: the
    registry refuses before any session exists, so no request can be made."""
    manifest = {
        "version": "test",
        "sources": [
            {
                "id": "us.rfp.puget_sound_energy",
                "name": "PSE",
                "url": "https://www.pse.com/en/pages/energy-supply/acquiring-energy",
                "category": "procurement",
                "access": "html",
                "reuse": "attribution",
                "publication": "derived_only",
                "cadence": "annual",
                "connector_module": "us_rfp",
            }
        ],
    }
    path = tmp_path / "sources.yaml"
    path.write_text(yaml.safe_dump(manifest))
    reg = Registry(path)
    assert registry_rows()["us.rfp.puget_sound_energy"].raw["automated_reading"] == "not_allowed"
    with pytest.raises(RegistrationError, match="automated_reading='not_allowed'"):
        reg.instantiate("us.rfp.puget_sound_energy")


def test_the_manifest_rows_are_the_tranche_and_no_broader_than_the_registry() -> None:
    doc = yaml.safe_load(registry_module.SOURCES_YAML.read_text(encoding="utf-8"))
    rows = {s["id"]: s for s in doc["sources"] if s.get("connector_module") == "us_rfp"}
    assert sorted(rows) == sorted(TRANCHE_1)
    reg = Registry()
    for sid, entry in rows.items():
        issuer = registry_rows()[sid]
        assert entry["reuse"] == issuer.manifest["reuse"], sid
        assert entry["publication"] == issuer.manifest["publication"], sid
        assert entry["attribution"] == issuer.raw["attribution"], sid
        assert entry["parent"] == "us.utility_rfps"
        assert entry["poll"] == "daily"
        assert float(entry["rate_limit_rps"]) <= 0.5
        assert entry.get("licence_url")
        assert reg.get(sid).implemented
        assert reg.connector_class(sid).source_id == sid
    # The umbrella stays a manifest entry only (docs/28 §10): never fetched.
    umbrella = reg.get("us.utility_rfps")
    assert umbrella.reuse == "unknown" and umbrella.publication == "none" and not umbrella.implemented


def test_a_manifest_broader_than_the_registry_is_refused() -> None:
    reg = Registry()
    entry = reg.get("us.rfp.pso")
    broader = SourceEntry.from_yaml({**entry.raw, "reuse": "attribution"})
    with pytest.raises(GateViolation, match="at most"):
        reg.connector_class("us.rfp.pso")(broader)


def test_a_connector_module_that_is_not_a_package_name_is_ignored() -> None:
    entry = SourceEntry.from_yaml(
        {
            "id": "x.y",
            "name": "x",
            "url": "https://x.example/",
            "category": "c",
            "access": "html",
            "cadence": "daily",
        }
        | {"connector_module": "../../etc"}
    )
    assert entry.module_name == "x_y" and not entry.implemented


# ------------------------------------------------------------------ posture (docs/26)
def test_the_aep_rows_are_gated_under_the_commercial_posture(tmp_path: pathlib.Path) -> None:
    """The deployed posture is noncommercial; the suite's default is commercial, where the AEP
    class may not leave the building: listed as gated, refused without the flag, quarantined with it."""
    reg = Registry()
    states = {row["id"]: row["state"] for row in reg.status()}
    for sid in AEP:
        assert reg.get(sid).reuse == "noncommercial"
        assert states[sid] == "gated"
        with pytest.raises(GateViolation, match="allow_restricted"):
            reg.instantiate(sid)
    assert states["us.rfp.ct_deep"] == states["us.rfp.nj_bpu"] == "implemented"
    result = run(
        "us.rfp.pso", registry=reg, store=Store(tmp_path), allow_restricted=True, raw=raw_of("us.rfp.pso")
    )
    assert result.status == "ok" and result.run["publishable"] is False
    assert all("quarantine" in str(p) for p in result.paths.values())
    assert not (tmp_path / "normalized").exists()


def test_the_aep_rows_publish_under_the_noncommercial_posture(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        registry_module, "PUBLISHABLE_REUSE", frozenset(posture.publishable_reuse_classes("noncommercial"))
    )
    monkeypatch.setattr(
        registry_module, "GATED_REUSE", frozenset(posture.gated_reuse_classes("noncommercial"))
    )
    reg = Registry()
    assert {row["id"]: row["state"] for row in reg.status()}["us.rfp.pso"] == "implemented"
    result = run("us.rfp.pso", registry=reg, store=Store(tmp_path), raw=raw_of("us.rfp.pso"))
    assert result.status == "ok" and result.run["publishable"] is True
    assert (tmp_path / "normalized" / "us.rfp.pso").exists()
    assert result.run["reuse_class"] == "noncommercial"


# =============================================================================== classifier
@pytest.mark.parametrize(
    ("title", "description", "category", "rule"),
    [
        (
            "2026 APCo Combined Cycle PSA RFP - Proposals Due September 17, 2026",
            "Up to 1500 MW",
            "generation",
            "",
        ),
        ("2025 SPP Capacity RFP", "", "generation", ""),
        ("2024 RFPs", "secure up to 4,000 MW of generation resources", "generation", ""),
        ("2026 All-Source RFP", "Purchase and Sale Agreements (PSA)", "generation", ""),
        ("DEEP Announces 2026 Zero Carbon Request for Proposals", "", "generation", ""),
        ("DEEP Launches New Procurement For Energy Storage", "", "generation", ""),
        ("In the Matter of the Garden State Energy Storage Program Phase 1, Tranche 2", "", "generation", ""),
        (
            "In the Matter of the Opening of New Jersey's Fourth Solicitation for Offshore Wind ORECs",
            "",
            "generation",
            "",
        ),
        (
            "Natural Gas Lateral, Interconnect & Associated Facilities / Firm Transportation RFP - Benton",
            "",
            "non_generation",
            "fuel_infrastructure",
        ),
        (
            "Rockport Natural Gas Project RFP",
            "to construct, operate and maintain a natural gas pipeline lateral",
            "non_generation",
            "fuel_infrastructure",
        ),
        (
            "Opening of a Solicitation for a Transmission Infrastructure Project",
            "",
            "non_generation",
            "transmission",
        ),
        ("DEEP Issues Request for Proposals for Paddlecraft Rental Services", "", "non_generation", "topic"),
        ("DEEP Issues RFP for Food and Beverage Concession Services", "", "non_generation", "topic"),
        (
            "DEEP Seeking Proposals for Grants for Aquatic Invasive Species Control",
            "",
            "non_generation",
            "topic",
        ),
        ("ESIP Revised Request for Proposal (RFP) Template", "", "non_generation", "topic"),
        ("DEEP Issues RFP for Trail Maintenance", "statewide parks", "non_generation", "no_resource"),
        (
            "Connecticut Partners Announce Over 100 MW of Clean Energy Selections",
            "",
            "not_solicitation",
            "award",
        ),
        ("DEEP Selects Proposals Under Novel Efficiency RFP", "", "not_solicitation", "award"),
        ("DEEP Announces RFI and Technical Meeting on Grid Efficiency", "", "not_solicitation", ""),
        ("Request for Information on Storage", "", "not_solicitation", ""),
        (
            "Stakeholder Meeting: Virtual Power Plant Program",
            "seeking proposals later",
            "not_solicitation",
            "",
        ),
        (
            "PROVISION OF BASIC GENERATION SERVICE FOR THE PERIOD BEGINNING JUNE 1, 2027",
            "",
            "not_solicitation",
            "",
        ),
    ],
)
def test_the_classifier(title: str, description: str, category: str, rule: str) -> None:
    verdict = clf.classify(title, description)
    assert verdict.category == category, verdict
    assert rule in verdict.rule
    assert verdict.publishable is (category == "generation")


@pytest.mark.parametrize(
    ("title", "description"),
    [
        ("Request for Proposals for the Sale of Excess Capacity", ""),
        ("2026 REC Sale RFP", ""),
        ("RFP to Sell Surplus Generating Units", ""),
        ("Capacity RFP", "The Company is soliciting offers to purchase energy from the Company's Plant"),
        ("2026 Energy RFP", "offers to purchase the output of the units"),
    ],
)
def test_sell_side_items_are_left_out(title: str, description: str) -> None:
    verdict = clf.classify(title, description)
    assert verdict.category == "sell_side" and not verdict.publishable


def test_a_purchase_and_sale_agreement_is_buy_side() -> None:
    description = (
        "up to 500 MW via one or more Purchase and Sale Agreements (PSAs) for new or existing resources"
    )
    assert clf.classify("2026 APCo Energy Storage PSA RFP", description).category == "generation"


def test_the_issuer_name_is_not_read_as_a_resource() -> None:
    title = "Request for Proposals for Website Services"
    snippet = "The NJ Clean Energy Program seeks bids for a new website"
    assert clf.classify(title, snippet).category == "generation"  # "clean energy" read as a resource
    verdict = clf.classify(title, snippet, ignore=("NJ Clean Energy Program",))
    assert verdict.category == "non_generation" and verdict.rule == "non_generation.no_resource"


@pytest.mark.parametrize(
    ("text", "mw"),
    [
        ("Up to 1500 MW of Combined Cycle Natural Gas resources", 1500.0),
        ("up to 4,000 megawatts (MW) of SPP accredited capacity", 4000.0),
        ("up to 2 GW of storage", 2000.0),
        ("Up to 800 MW of wind and/or solar; up to 300 MW of wind, solar, and/or hydro", None),
        ("up to 350 megawatts (MW) in 2028 and 600 MW in 2029 and 2030", None),
        ("seeking capacity resources", None),
        ("150 MW and again 150 MW", 150.0),
    ],
)
def test_capacity_is_stated_or_null_never_summed(text: str, mw: float | None) -> None:
    assert clf.capacity_sought_mw(text) == mw


def test_a_target_needs_a_verb_of_procurement() -> None:
    assert clf.target_mw("Tranche 2 aims to award an additional 645 MW of storage capacity") == 645.0
    assert clf.target_mw("the mandate of deploying 2,000 MW of energy storage by 2030") is None


def test_technologies_and_contract_types() -> None:
    assert clf.technologies("2026 All-Source RFP", "wind, solar and battery storage") == []
    assert clf.technologies("2026 APCo Energy Storage CPA RFP", "Up to 300 MW of Energy Storage") == ["bess"]
    assert clf.contract_types("PSAs; PPAs; renewable energy certificates (RECs) and a CPA") == [
        "PSA",
        "PPA",
        "REC",
        "CPA",
    ]


@pytest.mark.parametrize(
    ("date_text", "after", "expected"),
    [
        ("March 17, 2026", ", at 12:00 PM EPT for all other bids", "2026-03-17T16:00:00+00:00"),
        ("February 27, 2026", ", at 11:59 PM EPT", "2026-02-28T04:59:00+00:00"),
        ("November 3, 2026", " at 5:00 p.m. CT", "2026-11-03T23:00:00+00:00"),
        ("September 17, 2026", "", "2026-09-17T00:00:00+00:00"),
        ("9/30/2026", "", "2026-09-30T00:00:00+00:00"),
        ("Sept. 30 2026", "", "2026-09-30T00:00:00+00:00"),
    ],
)
def test_deadlines(date_text: str, after: str, expected: str) -> None:
    when = clf.deadline(date_text, after)
    assert when is not None and when.isoformat() == expected


def test_an_impossible_date_is_null() -> None:
    assert clf.parse_date("February 30, 2026") is None and clf.deadline("no date here") is None


# =============================================================================== extraction, raw pages
def test_the_aep_region_is_the_content_column_only() -> None:
    region = region_of("appalachian_power_energy_rfps.html", "us.rfp.appalachian_power", "listing")
    assert region is not None and region.startswith('<div id="main"') and len(region) < 10_000
    for chrome in ("<script", "__VIEWSTATE", "RelatedInformationHeader", "<footer", "stand-in"):
        assert chrome not in region, chrome
    assert "2026 APCo Combined Cycle PSA RFP" in region


AEP_PAGES = {
    "pso_energy_rfps.html": ("us.rfp.pso", "listing"),
    "pso_allsource2026.html": ("us.rfp.pso", "detail"),
    "appalachian_power_energy_rfps.html": ("us.rfp.appalachian_power", "listing"),
    "swepco_energy_rfps.html": ("us.rfp.swepco", "listing"),
    "indiana_michigan_power_energy_rfps.html": ("us.rfp.indiana_michigan_power", "listing"),
}


@pytest.mark.parametrize("name", sorted(AEP_PAGES))
def test_the_aep_pages_are_committed_trimmed(name: str) -> None:
    """AEP's terms permit personal use and limited quotations (the rows' `license`, publication
    derived_only) and this repository is public: an AEP page is committed as its content column in a
    stand-in skeleton, never as served (fixtures/README.md; docs/04 E-6)."""
    sid, role = AEP_PAGES[name]
    body = (FIX / name).read_text(encoding="utf-8")
    region = region_of(name, sid, role)
    assert region is not None
    assert len(body.replace(region, "")) < 1_500, "more than the stand-in skeleton outside the region"
    assert "__VIEWSTATE" in body and 'value="stand-in"' in body and body.count("<script") == 1
    assert "\r" not in body


def _aep(sid: str, listing: str, details: dict[str, str] | None = None) -> list[dict[str, Any]]:
    row = registry_rows()[sid]
    pages: list[dict[str, Any]] = [
        {
            "role": "listing",
            "url": row.listing_url,
            "status": 200,
            "region": region_of(listing, sid, "listing"),
        }
    ]
    for url, name in (details or {}).items():
        pages.append({"role": "detail", "url": url, "status": 200, "region": region_of(name, sid, "detail")})
    kept, left_out = extract.records({"pages": pages}, row, NOW.date())
    return kept + left_out


def test_aep_items_from_each_listing() -> None:
    by_sid = {
        "us.rfp.pso": ("pso_energy_rfps.html", 2, 0),
        "us.rfp.appalachian_power": ("appalachian_power_energy_rfps.html", 6, 0),
        "us.rfp.swepco": ("swepco_energy_rfps.html", 4, 3),
        "us.rfp.indiana_michigan_power": ("indiana_michigan_power_energy_rfps.html", 1, 1),
    }
    for sid, (listing, kept, left) in by_sid.items():
        items = _aep(sid, listing)
        assert sum(r["classification"] == "generation" for r in items) == kept, sid
        assert [r["classification_rule"] for r in items if r["classification"] != "generation"] == [
            "non_generation.fuel_infrastructure"
        ] * left, sid


def test_aep_titles_status_and_facts() -> None:
    items = {r["key"]: r for r in _aep("us.rfp.appalachian_power", "appalachian_power_energy_rfps.html")}
    cc = items["/business/b2b/energy-rfps/2026-combinedcyclerfp"]
    assert cc["title"] == "2026 APCo Combined Cycle PSA RFP"
    assert cc["due_at"] == "2026-09-17T00:00:00+00:00" and cc["status_raw"] == "listed"
    assert (
        cc["capacity_sought_mw"] == 1500.0
        and cc["technologies"] == ["gas"]
        and cc["contract_types"] == ["PSA"]
    )
    psa = items["/business/b2b/energy-rfps/energy-storage-rfps#communicatingitem1"]
    cpa = items["/business/b2b/energy-rfps/energy-storage-rfps#communicatingitem2"]
    assert (psa["capacity_sought_mw"], cpa["capacity_sought_mw"]) == (500.0, 300.0)
    renewables = items["/business/b2b/energy-rfps/2026-rfps"]
    assert renewables["capacity_sought_mw"] is None and renewables["mw_stated"] == [800.0, 300.0]
    archived = items["/business/b2b/energy-rfps/2025-rfps"]
    assert archived["section"] == "archive" and archived["status_raw"] == "closed"
    assert "– CLOSED" in archived["title_line"] and "â€" not in archived["title_line"]
    assert archived["title"] == "2025 APCo PSA, PPA, and REC RFPs"


def test_aep_per_rfp_page_gives_the_schedule() -> None:
    items = _aep(
        "us.rfp.pso", "pso_energy_rfps.html", {PSO_LISTING + "AllSource2026": "pso_allsource2026.html"}
    )
    allsource = next(r for r in items if r["key"] == "/business/b2b/energy-rfps/allsource2026")
    assert allsource["open_at"] == "2026-01-02"  # "Final RFP Issued", not "Draft RFP Issued November 7, 2025"
    assert allsource["due_at"] == "2026-03-16T00:00:00+00:00"
    assert allsource["capacity_sought_mw"] == 4000.0 and allsource["technologies"] == []
    joint = next(r for r in items if r["key"] == "/business/b2b/energy-rfps/2025-spp-capacity-rfp")
    assert joint["capacity_sought_mw"] is None and joint["mw_stated"] == [350.0, 600.0]


def test_a_self_build_deadline_is_not_the_bidders_deadline() -> None:
    """SWEPCO's 2026 All-Source page (live run 2026-10-21) states "Self-Build Proposal Due Date:
    7/28/2026" just before "Proposal Due Date: 7/29/2026". The utility's own deadline is not the
    bidders': read as such, the RFP would show closed a day before bids close."""
    b = bundle("us.rfp.swepco")
    detail = next(p for p in b["pages"] if p["url"].endswith("/AllSource2026"))
    text = htmltree.parse(str(detail["region"])).text()
    assert "Self-Build Proposal Due Date: 7/28/2026 Proposal Due Date: 7/29/2026" in text
    kept, _ = extract.records(b, registry_rows()["us.rfp.swepco"], NOW.date())
    allsource = next(r for r in kept if r["key"] == "/business/b2b/energy-rfps/allsource2026")
    assert allsource["due_text"] == "7/29/2026" and allsource["due_at"] == "2026-07-29T00:00:00+00:00"
    label = extract._AEP_DUE_LABEL
    for qualified in ("Self-Build Proposal Due Date: 7/28/2026", "self build proposals due 7/28/2026"):
        assert clf.labelled_dates(qualified, label) == [], qualified
    assert [d for d, _ in clf.labelled_dates("Proposal Due Date: 7/29/2026", label)] == ["7/29/2026"]


def test_a_per_rfp_page_shared_by_two_items_is_not_read_for_dates() -> None:
    kept, _ = extract.records(
        bundle("us.rfp.appalachian_power"), registry_rows()["us.rfp.appalachian_power"], NOW.date()
    )
    storage = [r for r in kept if "energy-storage-rfps#" in r["key"]]
    assert len(storage) == 2 and all(r["open_at"] is None and r["detail_url"] is None for r in storage)
    assert all(r["due_at"] == "2026-09-30T00:00:00+00:00" for r in storage)  # from the listing itself


def test_a_missing_content_column_is_a_parse_error() -> None:
    row = registry_rows()["us.rfp.pso"]
    with pytest.raises(ParseError, match="content column"):
        extract.records(
            {"pages": [{"role": "listing", "url": row.listing_url, "region": None}]}, row, NOW.date()
        )
    with pytest.raises(ParseError, match="no listing"):
        extract.records({"pages": []}, row, NOW.date())


def test_ct_listing_pages_and_the_zero_carbon_release() -> None:
    row = registry_rows()["us.rfp.ct_deep"]
    pages = [
        {
            "role": "listing",
            "url": CT_2026,
            "region": region_of("ct_deep_news_2026_page1.html", row.id, "listing"),
        },
        {
            "role": "listing",
            "url": CT_2026 + "?page=9",
            "region": region_of("ct_deep_news_2026_page9.html", row.id, "listing"),
        },
        {
            "role": "listing",
            "url": CT_2026 + "?page=10",
            "region": region_of("ct_deep_news_2026_page10.html", row.id, "listing"),
        },
        {
            "role": "detail",
            "url": CT_2026 + "/deep-announces-2026-zero-carbon-request-for-proposals",
            "region": region_of("ct_deep_zero_carbon_rfp_release.html", row.id, "detail"),
        },
    ]
    items, total = extract.news_items(pages[0]["region"], CT_2026)
    assert total == 10 and len(items) == 15 and items[0].date == dt.date(2026, 10, 21)
    kept, left = extract.records({"pages": pages, "window_start": "2025-10-21"}, row, NOW.date())
    assert [r["title"] for r in kept] == ["DEEP Announces 2026 Zero Carbon Request for Proposals"]
    zero = kept[0]
    # "through February 27, 2026, at 11:59 PM EPT ... and through March 17, 2026, at 12:00 PM EPT": the later.
    assert zero["due_at"] == "2026-03-17T16:00:00+00:00"
    assert zero["open_at"] == "2026-01-30" and zero["status_raw"] == "released"
    # "... nuclear, hydropower, solar, onshore wind and associated transmission": no transmission token.
    assert zero["technologies"] == ["wind", "solar_pv", "bess", "hydro", "nuclear"]
    assert zero["capacity_sought_mw"] is None
    assert zero["document_links"] and all("www.dpuc.state.ct.us" in u for u in zero["document_links"])
    rules = {r["title"][:40]: r["classification_rule"] for r in left}
    assert rules["DEEP Issues Request for Proposals for Pa"] == "non_generation.topic"
    assert rules["DEEP Selects Proposals to Help Lower Ele"] == "not_solicitation.award"
    assert rules["Connecticut and New England State Partne"] == "not_solicitation.award"
    # Its release page was not read in this bundle: never emitted as a thinner row.
    assert rules["DEEP Launches New Procurement For Energy"] == "not_read_this_run"


def test_a_request_for_comment_ahead_of_an_rfp_is_announced_not_open() -> None:
    frame_ = frame("us.rfp.ct_deep").set_index("source_record_id")
    storage = frame_.loc[
        "/deep/news-releases/news-releases---2026/deep-launches-new-procurement-for-energy-storage"
    ]
    assert storage["status_raw"] == "pre_solicitation" and storage["status"] == "announced"
    assert pd.isna(storage["open_at"]) and pd.isna(storage["due_at"])
    assert storage["technologies"] == "bess"


def test_nj_notice_table() -> None:
    row = registry_rows()["us.rfp.nj_bpu"]
    notices = extract.notice_rows(region_of("nj_bpu_public_notices.html", row.id, "listing"), NJ_INDEX)
    assert len(notices) == 142
    assert {n.year for n in notices} == {2024, 2025, 2026}
    gsesp = next(n for n in notices if n.url == GSESP_PDF)
    assert gsesp.issued == dt.date(2026, 7, 30)
    assert gsesp.title == "In the Matter of the Garden State Energy Storage Program Phase 1, Tranche 2"
    assert all(" " not in n.url for n in notices)
    with pytest.raises(ParseError, match="not on the page"):
        extract.notice_rows(None, NJ_INDEX)


def test_nj_notice_fields_are_read_in_memory_and_never_kept_as_text() -> None:
    text = (FIX / "nj_bpu_gsesp_tranche2_extension.txt").read_text(encoding="utf-8")
    fields = extract.pdf_fields(b"%PDF-1.7 recorded", extract_text=lambda _b: text)
    assert fields["docket"] == "QO25120634"  # the notice's own docket, not the footnoted QO22080540
    assert fields["dated"] == "2026-07-30"  # "Dated: July 30, 2026", not "Order dated March 4, 2026"
    # The extended deadline with its stated time: "has been extended to 11:59 p.m. ET on October 5,
    # 2026" is 03:59 UTC on 6 October (EDT), not midnight UTC on the 5th, which would close the
    # record a day early. The bidders' question deadline ("Deadline for bidder's question
    # submissions July 1, 2026") is not a candidate.
    assert fields["due_at"] == "2026-10-06T03:59:00+00:00"
    assert fields["due_text"] == "October 5, 2026"
    assert fields["open_at"] == "2026-05-20"
    # The PDF's text layer splits words ("a ward an additional 645 MW"): the target is not read,
    # and no other MW figure in the notice (2,000; 1,000; 355) is taken instead.
    assert fields["target_mw"] is None
    assert fields["restricted"] is False
    assert set(fields) == {
        "sha256",
        "bytes",
        "restricted",
        "restriction_phrase",
        "docket",
        "dated",
        "due_text",
        "due_at",
        "open_text",
        "open_at",
        "target_mw",
    }


@pytest.mark.parametrize(
    ("text", "due_at"),
    [
        # time and zone between the label and the date
        ("The proposal deadline is 5:00 p.m. CT on March 3, 2027.", "2027-03-03T23:00:00+00:00"),
        # time and zone after the date
        ("The bid deadline is April 1, 2027 at 3:00 PM EPT.", "2027-04-01T19:00:00+00:00"),
        # words other than a time between label and date: the date alone, midnight UTC
        ("The proposal deadline is extended to Friday, March 5, 2027.", "2027-03-05T00:00:00+00:00"),
        # a question deadline is not a bid deadline
        ("Deadline for bidder's question submissions July 1, 2027", None),
    ],
)
def test_notice_deadlines_keep_a_stated_time(text: str, due_at: str | None) -> None:
    assert extract.pdf_fields(b"%PDF", extract_text=lambda _b: text)["due_at"] == due_at


def test_a_notice_with_a_restriction_on_free_use_is_skipped() -> None:
    fields = extract.pdf_fields(
        b"%PDF", extract_text=lambda _b: "Notice. © 2026 Vendor LLC. All rights reserved."
    )
    assert fields["restricted"] is True and fields["docket"] is None
    row = registry_rows()["us.rfp.nj_bpu"]
    b = bundle("us.rfp.nj_bpu")
    for p in b["pages"]:
        if p["role"] == "notice":
            p["fields"] = fields
    kept, left = extract.records(b, row, NOW.date())
    assert kept == []
    assert any(r["classification_rule"] == "restricted_material" for r in left)


def _minimal_pdf(text: str) -> bytes:
    """A one-page PDF with one line of text, offsets computed, for the real pdfplumber path."""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for i, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return out


def test_pdf_text_reads_a_real_pdf_and_refuses_a_damaged_one() -> None:
    fields = extract.pdf_fields(_minimal_pdf("Docket No. QO26010001 Dated: May 1, 2026"))
    assert fields["docket"] == "QO26010001" and fields["dated"] == "2026-05-01"
    with pytest.raises(ParseError, match="could not be read"):
        extract.pdf_text(b"%PDF-1.4 this is not a pdf")


# =============================================================================== bundles: parse + normalize
EXPECTED_ROWS = {
    "us.rfp.pso": 2,
    "us.rfp.appalachian_power": 6,
    "us.rfp.swepco": 4,
    "us.rfp.indiana_michigan_power": 1,
    "us.rfp.ct_deep": 2,
    "us.rfp.nj_bpu": 1,
}


@pytest.mark.parametrize("sid", TRANCHE_1)
def test_every_record_carries_provenance_and_the_registry_facts(sid: str) -> None:
    reg = Registry()
    entry = reg.get(sid)
    issuer = registry_rows()[sid]
    df = frame(sid)
    assert len(df) == EXPECTED_ROWS[sid]
    assert (df["source_id"] == sid).all()
    assert (df["licence_id"] == entry.licence_id).all()
    assert (df["retrieved_at"] == "2026-10-21T16:00:00Z").all()
    assert (df["record_id"] == sid + ":" + df["source_record_id"]).all()
    assert df["source_url"].str.startswith("https://" + issuer.listing_host + "/").all()
    assert (df["kind"] == "rfp").all()
    assert (df["issuer"] == issuer.issuer).all() and (df["jurisdiction"] == issuer.jurisdiction).all()
    assert set(df["status"]) <= set(OPPORTUNITY_STATES)
    assert df["status_rule"].str.startswith("utility_rfp.").all()
    assert not df["record_id"].duplicated().any()
    for raw in df["raw"]:
        payload = json.loads(raw)
        assert payload["issuer_id"] == sid and payload["classification"] == "generation"
        assert "@" not in json.dumps(payload)
    ids = [json.loads(v) for v in df["identifiers"]]
    assert all(i["issuer_registry_id"] == sid for i in ids)


def test_statuses_follow_the_stated_deadlines() -> None:
    swepco = frame("us.rfp.swepco").set_index("source_record_id")
    assert (
        swepco.loc["/business/b2b/energy-rfps/2024-energy-rfp", "capacity_sought_mw"] == 2100.0
    )  # per-RFP page
    assert (swepco["status"] == "closed").all()
    im = frame("us.rfp.indiana_michigan_power")
    assert im["status"].tolist() == ["unknown"]  # no stated deadline: never guessed
    nj = frame("us.rfp.nj_bpu").iloc[0]
    assert nj["source_record_id"] == "docket:qo25120634" and nj["status"] == "closed"
    assert json.loads(nj["identifiers"])["docket"] == "QO25120634"
    # The same bundle read before the deadline: open.
    early = frame("us.rfp.nj_bpu", at=dt.datetime(2026, 9, 1, tzinfo=dt.UTC)).iloc[0]
    assert early["status"] == "open" and early["status_rule"] == "utility_rfp.notice_deadline_ahead"


def test_a_bundle_from_another_issuer_is_refused() -> None:
    reg = Registry()
    c = reg.connector_class("us.rfp.ct_deep")(reg.get("us.rfp.ct_deep"))
    with pytest.raises(ParseError, match="names issuer"):
        c.parse(raw_of("us.rfp.ct_deep", content=(FIX / "bundle_nj_bpu_20261021.json").read_bytes()))
    with pytest.raises(ParseError, match="not a JSON bundle"):
        c.parse(raw_of("us.rfp.ct_deep", content=b"<html>"))


def test_the_effective_parser_version_moves_with_the_registry_row(monkeypatch: pytest.MonkeyPatch) -> None:
    cls = module.connector_class_for("us.rfp.pso")
    before = cls.parser_digest()
    changed = IssuerRow(
        cls.issuer_row.id, cls.issuer_row.name, {**cls.issuer_row.raw, "connector": {"kind": "auction"}}
    )
    monkeypatch.setattr(cls, "issuer_row", changed)
    assert cls.parser_digest() != before


# =============================================================================== fetch (fake session)
def _pso_routes(**extra: Route) -> dict[str, Route]:
    routes: dict[str, Route] = {
        PSO_LISTING: page("pso_energy_rfps.html"),
        PSO_LISTING + "AllSource2026": page("pso_allsource2026.html"),
        "https://www.aep.com/terms/": terms_page("us.rfp.pso"),
    }
    routes.update(extra)
    return routes


def test_aep_fetch_is_polite_and_reads_only_the_listing_its_pages_and_the_terms() -> None:
    c, session = make("us.rfp.pso", _pso_routes())
    snap = c.fetch()
    hosts = {u.split("/")[2] for u in session.urls()}
    assert hosts == {"www.psoklahoma.com", "www.aep.com"}
    assert session.urls() == [
        "https://www.aep.com/robots.txt",
        "https://www.aep.com/terms/",
        "https://www.psoklahoma.com/robots.txt",
        PSO_LISTING,
        PSO_LISTING + "2025-SPP-Capacity-RFP",  # 404 in the fake: noted, not fatal
        PSO_LISTING + "AllSource2026",
    ]
    for _url, headers in session.calls:
        assert "Bankable/0.2 (+https://" in headers["User-Agent"] and "/contact)" in headers["User-Agent"]
    assert c.http.rate_limits == {"www.psoklahoma.com": 0.5, "www.aep.com": 0.5}
    assert snap.meta["notes"] == {"missing": [f"404 {PSO_LISTING}2025-SPP-Capacity-RFP"]}
    assert snap.meta["terms_check"]["missing"] == [] and snap.meta["items_kept"] == 2
    b = json.loads(snap.content)
    assert [p["role"] for p in b["pages"]] == ["listing", "detail"]
    assert all(len(p["region"]) < 20_000 for p in b["pages"])


def test_robots_disallowing_the_listing_blocks_the_run(tmp_path: pathlib.Path) -> None:
    robots = (200, b"User-agent: *\nDisallow: /business/\n", {})
    c, session = make("us.rfp.pso", _pso_routes(**{"https://www.psoklahoma.com/robots.txt": robots}))
    with pytest.raises(BlockedError, match=r"robots\.txt disallows"):
        c.fetch()
    assert PSO_LISTING not in session.urls()


def test_robots_disallowing_a_per_rfp_page_skips_it() -> None:
    robots = (200, b"User-agent: Bankable\nDisallow: /business/b2b/energy-rfps/AllSource2026\n", {})
    c, session = make("us.rfp.pso", _pso_routes(**{"https://www.psoklahoma.com/robots.txt": robots}))
    snap = c.fetch()
    assert PSO_LISTING + "AllSource2026" not in session.urls()
    assert snap.meta["notes"]["robots_disallowed"] == [PSO_LISTING + "AllSource2026"]


@pytest.mark.parametrize("status", [401, 403, 451])
def test_a_refusal_stops_the_run_without_a_retry(tmp_path: pathlib.Path, status: int) -> None:
    c, session = make("us.rfp.pso", _pso_routes(**{PSO_LISTING: (status, b"Access Denied", {})}))
    with pytest.raises(BlockedError, match=f"HTTP {status}"):
        c.fetch()
    assert session.urls().count(PSO_LISTING) == 1
    assert not any("AllSource2026" in u for u in session.urls())


def test_a_challenge_page_is_a_block_never_retried() -> None:
    challenge = (503, b"<html><title>Just a moment...</title></html>", {"cf-mitigated": "challenge"})
    c, session = make("us.rfp.pso", _pso_routes(**{PSO_LISTING: challenge}))
    with pytest.raises(BlockedError, match="challenge"):
        c.fetch()
    assert session.urls().count(PSO_LISTING) == 1


def test_a_tdm_reservation_is_honoured() -> None:
    status, body, headers = page("pso_energy_rfps.html")
    c, _ = make(
        "us.rfp.pso", _pso_routes(**{PSO_LISTING: (status, body, {**headers, "tdm-reservation": "1"})})
    )
    with pytest.raises(BlockedError, match="tdm-reservation"):
        c.fetch()


def test_a_declared_crawl_delay_slows_the_host() -> None:
    robots = (200, b"User-agent: *\nCrawl-delay: 10\nAllow: /\n", {})
    c, _ = make("us.rfp.pso", _pso_routes(**{"https://www.psoklahoma.com/robots.txt": robots}))
    c.fetch()
    assert c.http.rate_limits["www.psoklahoma.com"] == pytest.approx(0.1)


def test_a_link_only_host_is_never_requested() -> None:
    c, session = make("us.rfp.ct_deep", {})
    fx = extract.Fetcher(http=c.http, row=c.issuer_row)
    with pytest.raises(ConnectorError, match="link-only"):
        fx.get("https://www.dpuc.state.ct.us/DEEPEnergy.nsf/notice.pdf")
    with pytest.raises(ConnectorError, match="not this issuer's"):
        fx.get("https://example.org/rfp")
    with pytest.raises(ConnectorError, match="non-https"):
        fx.get("http://portal.ct.gov/deep")
    assert session.calls == []


def test_terms_drift_pauses_the_issuer(tmp_path: pathlib.Path) -> None:
    c, session = make(
        "us.rfp.pso", _pso_routes(**{"https://www.aep.com/terms/": terms_page("us.rfp.pso", drop=2)})
    )
    with pytest.raises(BlockedError, match="1 of 4 quoted terms clauses"):
        c.fetch()
    assert PSO_LISTING not in session.urls()  # nothing read under terms nobody has re-read


def test_the_terms_are_re_read_monthly_not_daily() -> None:
    first, _ = make("us.rfp.pso", _pso_routes())
    snap = first.fetch()
    second, session = make("us.rfp.pso", _pso_routes())
    second.previous = previous(snap.content, snap.meta, NOW - dt.timedelta(days=1))
    again = second.fetch()
    assert "https://www.aep.com/terms/" not in session.urls()
    assert again.meta["terms_check"] == snap.meta["terms_check"]
    later, session = make("us.rfp.pso", _pso_routes())
    stale = {
        **snap.meta,
        "terms_check": {**snap.meta["terms_check"], "checked_at": "2026-09-01T00:00:00+00:00"},
    }
    later.previous = previous(snap.content, stale, NOW - dt.timedelta(days=1))
    later.fetch()
    assert "https://www.aep.com/terms/" in session.urls()


def _nj_routes(index: Route, pdf: Route | None = None) -> dict[str, Route]:
    routes: dict[str, Route] = {
        NJ_INDEX: index,
        "https://www.nj.gov/nj/legal.shtml": terms_page("us.rfp.nj_bpu"),
    }
    if pdf is not None:
        routes[GSESP_PDF] = pdf
    return routes


def test_nj_sends_conditional_requests_and_reuses_a_304(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    text = (FIX / "nj_bpu_gsesp_tranche2_extension.txt").read_text(encoding="utf-8")
    monkeypatch.setattr(extract, "pdf_text", lambda _b: text)
    validators = {"ETag": '"1e806-6ad7a799"', "Last-Modified": "Tue, 20 Oct 2026 17:40:41 GMT"}
    status, body, headers = page("nj_bpu_public_notices.html")
    index_200 = (status, body, {**headers, **validators})
    pdf_200 = (200, b"%PDF-1.7 recorded", {"ETag": '"4c9c1-6a6b830a"', "Content-Type": "application/pdf"})
    store = Store(tmp_path)
    reg = Registry()

    first, session = make("us.rfp.nj_bpu", _nj_routes(index_200, pdf_200))
    r1 = run("us.rfp.nj_bpu", registry=reg, store=store, http=first.http, now=NOW)
    assert r1.status == "ok", r1.run["error"]
    assert r1.run["rows_fetched"] == 1 and GSESP_PDF in session.urls()
    assert r1.run["snapshot"]["meta"]["validators"][NJ_INDEX]["etag"] == '"1e806-6ad7a799"'

    def index_304(sent: dict[str, str]) -> tuple[int, bytes, dict[str, str]]:
        assert sent.get("If-None-Match") == '"1e806-6ad7a799"'
        assert sent.get("If-Modified-Since") == "Tue, 20 Oct 2026 17:40:41 GMT"
        return 304, b"", {}

    second, session = make("us.rfp.nj_bpu", _nj_routes(index_304))
    r2 = run("us.rfp.nj_bpu", registry=reg, store=store, http=second.http, now=NOW + dt.timedelta(days=1))
    assert r2.status == "unchanged", r2.run
    assert GSESP_PDF not in session.urls()  # an unchanged table points at the same notices
    assert "https://www.nj.gov/nj/legal.shtml" not in session.urls()  # terms read a day ago


def test_ct_reads_back_only_to_its_window() -> None:
    """A daily run reads page 1 (the window since the last fetch, 7 days' overlap)."""
    routes: dict[str, Route] = {CT_2026: page("ct_deep_news_2026_page1.html")}
    c, session = make("us.rfp.ct_deep", routes)
    prev = json.dumps({"issuer": "us.rfp.ct_deep", "pages": []}).encode()
    c.previous = previous(prev, {"terms_check": recent_terms("us.rfp.ct_deep")}, NOW - dt.timedelta(days=1))
    snap = c.fetch()
    assert [u for u in session.urls() if "news-releases" in u] == [CT_2026]
    assert json.loads(snap.content)["window_start"] == "2026-10-13"
    assert snap.meta["items_kept"] == 0 and snap.meta["truncated"] is False


def test_ct_pages_back_to_its_window_and_reads_only_the_candidate_releases() -> None:
    """After a gap (last fetch 6 February: window from 30 January) the run pages back until the
    listing passes the window (fixture pages 1, 9 and 10 stand in for pages 1-3) and opens only the
    releases the classifier keeps; the robots-closed document store is never requested."""
    routes: dict[str, Route] = {
        CT_2026: page("ct_deep_news_2026_page1.html"),
        CT_2026 + "?page=2": page("ct_deep_news_2026_page9.html"),
        CT_2026 + "?page=3": page("ct_deep_news_2026_page10.html"),
        CT_2026 + "/deep-announces-2026-zero-carbon-request-for-proposals": page(
            "ct_deep_zero_carbon_rfp_release.html"
        ),
        "https://portal.ct.gov/portal/policies/disclaimer/": terms_page("us.rfp.ct_deep"),
    }
    c, session = make("us.rfp.ct_deep", routes)
    prev = json.dumps({"issuer": "us.rfp.ct_deep", "pages": []}).encode()
    c.previous = previous(prev, {}, dt.datetime(2026, 2, 6, tzinfo=dt.UTC))
    snap = c.fetch()
    listings = [u for u in session.urls() if u.startswith(CT_2026) and "/deep-" not in u]
    assert listings == [CT_2026, CT_2026 + "?page=2", CT_2026 + "?page=3"]
    releases = [u for u in session.urls() if u.startswith(CT_2026 + "/")]
    assert releases == [
        CT_2026 + "/deep-launches-new-procurement-for-energy-storage",  # 404 in the fake: noted
        CT_2026 + "/deep-announces-2026-zero-carbon-request-for-proposals",
    ]
    assert not any("dpuc" in u for u in session.urls())
    assert json.loads(snap.content)["window_start"] == "2026-01-30"
    assert snap.meta["items_kept"] == 1 and snap.meta["truncated"] is False
    assert snap.meta["left_out"]["not_read_this_run"] == 1
    assert snap.meta["terms_check"]["quotes"] == 3 and snap.meta["terms_check"]["missing"] == []


def test_a_missing_current_year_listing_fails_the_run() -> None:
    c, _ = make(
        "us.rfp.ct_deep", {"https://portal.ct.gov/portal/policies/disclaimer/": terms_page("us.rfp.ct_deep")}
    )
    with pytest.raises(ConnectorError, match="HTTP 404"):
        c.fetch()


def test_early_in_january_a_missing_new_year_listing_reads_the_previous_year() -> None:
    """DEEP's `news-releases---2027` may not exist on 10 January. Within the grace period the run
    notes the 404 and reads 2026's listing as well; after it, the 404 fails the run as above."""
    ct_2027 = CT_2026.replace("2026", "2027")
    routes: dict[str, Route] = {
        CT_2026: page("ct_deep_news_2026_page1.html"),
        "https://portal.ct.gov/portal/policies/disclaimer/": terms_page("us.rfp.ct_deep"),
    }
    prev = json.dumps({"issuer": "us.rfp.ct_deep", "pages": []}).encode()
    january = dt.datetime(2027, 1, 10, 16, 0, tzinfo=dt.UTC)
    c, session = make("us.rfp.ct_deep", routes)
    c.clock = lambda: january
    c.previous = previous(prev, {}, january - dt.timedelta(days=1))
    snap = c.fetch()
    assert [u for u in session.urls() if "news-releases" in u] == [ct_2027, CT_2026]
    assert snap.meta["notes"]["missing"] == [f"404 {ct_2027}"]
    assert json.loads(snap.content)["window_start"] == "2027-01-02" and snap.meta["items_kept"] == 0
    later, _ = make("us.rfp.ct_deep", routes)
    later.clock = lambda: january + dt.timedelta(days=10)
    later.previous = previous(prev, {}, january + dt.timedelta(days=9))
    with pytest.raises(ConnectorError, match="HTTP 404"):
        later.fetch()


def test_a_candidate_linked_on_another_host_is_noted_and_skipped() -> None:
    """A candidate release linked off the issuer's hosts is never requested and does not fail the
    run: it is noted `off_host` and, like any release not read, not emitted."""
    storage = CT_2026 + "/deep-launches-new-procurement-for-energy-storage"
    elsewhere = "https://www.example.org/deep-launches-new-procurement-for-energy-storage"
    status, body, headers = page("ct_deep_news_2026_page9.html")
    assert storage.encode() in body
    routes: dict[str, Route] = {
        CT_2026: page("ct_deep_news_2026_page1.html"),
        CT_2026 + "?page=2": (status, body.replace(storage.encode(), elsewhere.encode()), headers),
        CT_2026 + "?page=3": page("ct_deep_news_2026_page10.html"),
        CT_2026 + "/deep-announces-2026-zero-carbon-request-for-proposals": page(
            "ct_deep_zero_carbon_rfp_release.html"
        ),
        "https://portal.ct.gov/portal/policies/disclaimer/": terms_page("us.rfp.ct_deep"),
    }
    c, session = make("us.rfp.ct_deep", routes)
    prev = json.dumps({"issuer": "us.rfp.ct_deep", "pages": []}).encode()
    c.previous = previous(prev, {}, dt.datetime(2026, 2, 6, tzinfo=dt.UTC))
    snap = c.fetch()
    assert snap.meta["notes"]["off_host"] == [elsewhere]
    assert not any("example.org" in u for u in session.urls())
    assert snap.meta["items_kept"] == 1


def test_a_final_rfp_release_that_mentions_its_draft_is_released() -> None:
    """The lead decides pre-solicitation only when it does not call the release the final RFP."""
    row = registry_rows()["us.rfp.ct_deep"]
    lead_start = "<p>(HARTFORD)-The Connecticut"
    mentions_draft = "<p>(HARTFORD)-Following comments on its draft, the Connecticut"

    def status_of(edit: Callable[[str], str]) -> str:
        b = bundle("us.rfp.ct_deep")
        for p in b["pages"]:
            if p["url"].endswith("zero-carbon-request-for-proposals"):
                assert lead_start in p["region"]
                p["region"] = edit(p["region"])
        kept, _ = extract.records(b, row, NOW.date())
        return str(next(r for r in kept if "zero-carbon" in r["key"])["status_raw"])

    def no_final(region: str) -> str:
        return region.replace(lead_start, mentions_draft).replace("released a final&nbsp;", "released a ")

    assert status_of(lambda r: r) == "released"
    assert status_of(lambda r: r.replace(lead_start, mentions_draft)) == "released"  # "a final RFP"
    assert status_of(no_final) == "pre_solicitation"


def test_an_unchanged_listing_is_an_unchanged_run(tmp_path: pathlib.Path) -> None:
    """Idempotent re-runs: the same bytes, and the same items in different bytes (CT's window
    moves every day), are `unchanged`, with no new snapshot and no events."""
    store = Store(tmp_path)
    reg = Registry()
    r1 = run("us.rfp.ct_deep", registry=reg, store=store, raw=raw_of("us.rfp.ct_deep"))
    assert r1.status == "ok" and r1.run["rows_new"] == 2
    r2 = run(
        "us.rfp.ct_deep",
        registry=reg,
        store=store,
        raw=raw_of("us.rfp.ct_deep", at=NOW + dt.timedelta(hours=1)),
    )
    assert r2.status == "unchanged"
    moved = bundle("us.rfp.ct_deep")
    moved["window_start"] = "2025-10-20"
    moved_bytes = json.dumps(moved, ensure_ascii=False, sort_keys=True, indent=1).encode()
    r3 = run(
        "us.rfp.ct_deep",
        registry=reg,
        store=store,
        raw=raw_of("us.rfp.ct_deep", content=moved_bytes, at=NOW + dt.timedelta(days=1)),
    )
    assert r3.status == "unchanged" and r3.run["events_emitted"] == 0
    assert len(list((tmp_path / "snapshots" / "us.rfp.ct_deep").iterdir())) == 1


def _quiet_ct_day() -> bytes:
    """The next day's CT bundle: the listing as recorded, the window moved past both releases,
    and no release page read (none is new)."""
    quiet = bundle("us.rfp.ct_deep")
    quiet["window_start"] = "2026-10-15"
    quiet["pages"] = [p for p in quiet["pages"] if p["role"] == "listing"][:1]
    return json.dumps(quiet, ensure_ascii=False, sort_keys=True, indent=1).encode()


def test_a_quiet_ct_day_is_an_ok_run_that_carries_the_releases_forward(tmp_path: pathlib.Path) -> None:
    """Live run 2026-10-21, second pass: a day with no new procurement release parses to no rows.
    Before the source columns were declared, DQ read that as every key column removed and held the
    run (`schema_drift`). Now it is `ok`: the two releases are carried forward, nothing is emitted,
    and a third identical day is `unchanged`."""
    store = Store(tmp_path)
    reg = Registry()
    r1 = run("us.rfp.ct_deep", registry=reg, store=store, raw=raw_of("us.rfp.ct_deep"))
    assert r1.status == "ok" and r1.run["rows_new"] == 2
    day = NOW + dt.timedelta(days=1)
    r2 = run("us.rfp.ct_deep", registry=reg, store=store, raw=raw_of("us.rfp.ct_deep", _quiet_ct_day(), day))
    assert r2.status == "ok", r2.run.get("hold_reasons")
    assert r2.run["dq_status"] in ("pass", "warn") and not r2.run.get("hold_reasons")
    assert r2.run["rows_fetched"] == 0 and r2.run["rows_seen"] == 2
    assert (
        r2.run["rows_new"] == r2.run["rows_changed"] == r2.run["rows_gone"] == r2.run["events_emitted"] == 0
    )
    carried = pd.read_parquet(r2.paths["normalized"])
    assert set(carried["title"]) == {
        "DEEP Announces 2026 Zero Carbon Request for Proposals",
        "DEEP Launches New Procurement For Energy Storage",
    }
    r3 = run(
        "us.rfp.ct_deep",
        registry=reg,
        store=store,
        raw=raw_of("us.rfp.ct_deep", _quiet_ct_day(), day + dt.timedelta(hours=1)),
    )
    assert r3.status == "unchanged" and r3.run["events_emitted"] == 0


def test_an_empty_nj_register_is_not_held_but_an_empty_aep_page_is(tmp_path: pathlib.Path) -> None:
    """NJ BPU's register is the generation solicitations noticed in the last 400 days: once
    GSESP Tranche 2's notice ages out it may hold none, and that is not a fault. An AEP page always
    lists its archive, so an AEP page with no items is a template change and holds."""
    reg = Registry()
    store = Store(tmp_path / "nj")
    assert run("us.rfp.nj_bpu", registry=reg, store=store, raw=raw_of("us.rfp.nj_bpu")).status == "ok"
    # 401 days on, the fetch no longer reads the notice PDF (`fetch_notices` skips notices older
    # than the look-back), so the bundle holds the table alone.
    aged_bundle = bundle("us.rfp.nj_bpu")
    aged_bundle["pages"] = [p for p in aged_bundle["pages"] if p["role"] == "listing"]
    aged_bytes = json.dumps(aged_bundle, ensure_ascii=False, sort_keys=True, indent=1).encode()
    later = NOW + dt.timedelta(days=401)
    aged = run("us.rfp.nj_bpu", registry=reg, store=store, raw=raw_of("us.rfp.nj_bpu", aged_bytes, later))
    assert aged.status == "ok", aged.run.get("hold_reasons")
    assert aged.run["rows_fetched"] == 0 and aged.run["rows_gone"] == 1

    empty = bundle("us.rfp.pso")
    for p in empty["pages"]:
        if p["role"] == "listing":
            p["region"] = '<div id="main"><h1>Energy Provider RFPs</h1></div>'
    empty_bytes = json.dumps(empty, ensure_ascii=False, sort_keys=True, indent=1).encode()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            registry_module,
            "PUBLISHABLE_REUSE",
            frozenset(posture.publishable_reuse_classes("noncommercial")),
        )
        mp.setattr(registry_module, "GATED_REUSE", frozenset(posture.gated_reuse_classes("noncommercial")))
        pso_reg = Registry()
        pso_store = Store(tmp_path / "pso")
        assert run("us.rfp.pso", registry=pso_reg, store=pso_store, raw=raw_of("us.rfp.pso")).status == "ok"
        held = run(
            "us.rfp.pso",
            registry=pso_reg,
            store=pso_store,
            raw=raw_of("us.rfp.pso", empty_bytes, NOW + dt.timedelta(days=1)),
        )
    assert held.status == "partial" and any("zero_rows" in h for h in held.run["hold_reasons"])


def test_the_small_registers_tolerate_one_more_solicitation() -> None:
    """Every full-register issuer holds two to eight items: the default 30 % hold would hold the
    run on the day a second RFP appears, which is the news. Hold only above +-100 %."""
    for sid in TRANCHE_1:
        entry = Registry().get(sid)
        if registry_rows()[sid].extractor == "news_release_index":
            continue  # incremental: row-count drift is informational there
        thresholds = entry.raw.get("dq_thresholds") or {}
        assert thresholds.get("row_drift_hold_pct", 0) >= 100.0, sid


def test_a_sell_side_item_on_a_listing_is_left_out_end_to_end() -> None:
    status, body, headers = page("pso_energy_rfps.html")
    sell = (
        b'<p class="text-bold mb-0">2026 REC Sale RFP</p>'
        b'<p class="mb-30">PSO invites offers to purchase renewable energy certificates from the Company.</p>'
        b'<a href="/business/b2b/energy-rfps/REC-Sale" class="btn">Learn more</a>'
    )
    marker = b'<p class="text-bold mb-0">2026 All-Source RFP</p>'
    assert marker in body
    c, session = make(
        "us.rfp.pso", _pso_routes(**{PSO_LISTING: (status, body.replace(marker, sell + marker), headers)})
    )
    snap = c.fetch()
    assert snap.meta["left_out"] == {"sell_side.issuer_sells": 1}
    assert PSO_LISTING + "REC-Sale" not in session.urls()  # a left-out item's page is not opened
    titles = [r["title"] for r in c.parse(snap)]
    assert "2026 REC Sale RFP" not in titles and "2026 All-Source RFP" in titles


def test_contact_details_are_redacted_before_storage() -> None:
    reg = Registry()
    c = reg.connector_class("us.rfp.pso")(reg.get("us.rfp.pso"))
    b = {
        "issuer": "us.rfp.pso",
        "pages": [
            {"role": "detail", "url": "u", "region": "<p>Email PSO2026RFP@aep.com or call 918-599-2000.</p>"}
        ],
    }
    out = json.loads(c.redact(json.dumps(b).encode()))
    assert out["pages"][0]["region"] == "<p>Email [email removed] or call [phone removed].</p>"
    assert c.redact(b"not json") == b"not json"
