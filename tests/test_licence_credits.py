"""Credit lines render verbatim from the manifest on every surface (L-2 and L-10 of the 2026-09-30
legal audit; docs/13 §2.5, §2.4, §2.19; docs/21 §8 "the credit line is not optional").

NESO's Open Data Licence: "Acknowledge NESO as the source of the Information by including the
following attribution statement 'Supported by National Energy SO Open Data' ... if you fail to
comply with them the rights granted to you under this licence ... will end automatically." The
loader used to write `Source: National Energy System Operator` for every attribution source, and
that, not the statement, reached the API, RSS, CSV, bulk, `/attribution` and the pages. The NESO
gazetteer that places GB records was credited nowhere. Here the real manifest entries are loaded
and every surface is read.
"""

from __future__ import annotations

import csv
import io
import json
import pathlib
import re
from collections.abc import Iterator
from typing import Any

import pandas as pd
import pytest
import yaml
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from pipeline.connectors.registry import Registry
from services.ingest.loader import load_dataframe, refresh_all_licence_credits, upsert_licence_and_source
from tests.conftest import login, make_account, make_api_key, make_user

NESO = "Supported by National Energy SO Open Data"
NESO_LICENCE = "https://www.neso.energy/data-portal/neso-open-licence"
TEC = "gb.neso.tec_register"


@pytest.fixture(autouse=True)
def _export_dir(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXPORT_DIR", str(tmp_path / "exports"))


@pytest.fixture(scope="module")
def registry() -> Registry:
    return Registry()


def _neso_row(record_id: str = "TEC-1") -> dict[str, Any]:
    return {
        "record_id": f"{TEC}:{record_id}",
        "source_id": TEC,
        "source_record_id": record_id,
        "source_url": "https://api.neso.energy/tec/" + record_id,
        "retrieved_at": "2026-09-13T20:26:18Z",
        "kind": "generation",
        "name_canonical": "Longside Wind Farm",
        "sponsor_name": None,
        "technology": "wind_onshore",
        "technology_raw": "Wind Onshore",
        "capacity_mw": 50.0,
        "storage_mwh": None,
        "iso": None,
        "state": None,
        "county": None,
        "lifecycle_state": "filed",
        "status_raw": "Scoping",
        "proposed_cod": None,
        "queue_id": record_id,
        "eia_plant_id": None,
        "eia_generator_id": None,
        "raw": json.dumps({"Project Name": "Longside Wind Farm"}),
    }


def _load_neso(db: Session, registry: Registry) -> str:
    source = upsert_licence_and_source(db, registry.sources[TEC], registry.version)
    load_dataframe(db, source, "proposal", pd.DataFrame([_neso_row()]), None)
    db.commit()
    from services.db.models import Proposal

    prop = db.query(Proposal).one()
    return prop.public_id


@pytest.fixture()
def site(client: TestClient) -> Iterator[TestClient]:
    """The public site reading the in-process API that `client`'s database override serves."""
    from web.api_client import build_client
    from web.app import app as web_app

    previous = web_app.state.__dict__.get("api_client")
    web_app.state.api_client = build_client(api_base_url="")
    try:
        with TestClient(web_app) as w:
            yield w
    finally:
        if previous is None:
            web_app.state.__dict__.pop("api_client", None)
        else:
            web_app.state.api_client = previous


def test_the_loader_writes_the_mandated_neso_statement_and_licence_link(
    db: Session, registry: Registry
) -> None:
    source = upsert_licence_and_source(db, registry.sources[TEC], registry.version)
    assert source.licence.attribution_text == NESO
    assert source.licence.url == NESO_LICENCE


def test_a_licence_loaded_with_the_generic_line_is_corrected_by_the_next_load_or_a_refresh(
    db: Session, registry: Registry
) -> None:
    source = upsert_licence_and_source(db, registry.sources[TEC], registry.version)
    source.licence.attribution_text = "Source: National Energy System Operator"  # the pre-fix row
    source.licence.url = None
    db.commit()
    assert refresh_all_licence_credits(db, registry) == [source.licence_id]
    assert (source.licence.attribution_text, source.licence.url) == (NESO, NESO_LICENCE)
    source.licence.attribution_text = "Source: National Energy System Operator"
    upsert_licence_and_source(db, registry.sources[TEC], registry.version)
    assert source.licence.attribution_text == NESO


def test_every_served_surface_carries_the_neso_statement_verbatim(
    client: TestClient, db: Session, registry: Registry, site: TestClient
) -> None:
    pid = _load_neso(db, registry)

    detail = client.get(f"/v1/proposals/{pid}").json()
    assert detail["data"]["provenance"][0]["attribution_text"] == NESO
    summary = detail["licence_summary"]["sources"][0]
    assert (summary["attribution_text"], summary["licence_url"]) == (NESO, NESO_LICENCE)

    rss = client.get("/feeds/proposals.rss").text
    assert f"<inframation:attribution>{NESO}</inframation:attribution>" in rss
    assert f"<dc:creator>{NESO}</dc:creator>" in rss
    assert "Source: National Energy System Operator" not in rss

    account = make_account(db, entitlement="api")
    user = make_user(db, account)
    _key, secret = make_api_key(db, account, user, scopes=["read:live", "read:bulk"])
    db.commit()
    bulk = client.get("/v1/bulk/proposals", headers={"Authorization": f"Bearer {secret}"})
    lines = [json.loads(line) for line in bulk.iter_lines() if line]
    assert lines[0]["licence_summary"]["sources"][0]["attribution_text"] == NESO
    assert lines[1]["provenance"][0]["attribution_text"] == NESO

    login(client, db, user)
    created = client.post("/v1/exports", json={"entity": "proposal", "query": {}})
    text = client.get(f"/v1/exports/{created.json()['data']['export_id']}/download").text
    header = [line for line in text.splitlines() if line.startswith(f"# {TEC}:")]
    assert header and f"— {NESO};" in header[0] and f"licence_url={NESO_LICENCE};" in header[0]
    body = "\n".join(line for line in text.splitlines() if not line.startswith("#"))
    assert [r["attribution_text"] for r in csv.DictReader(io.StringIO(body))] == [NESO]

    attribution = site.get("/attribution").text
    assert attribution.count(NESO) >= 2  # the TEC register's row and the GSP gazetteer's
    assert "Grid Supply Point" in attribution and NESO_LICENCE in attribution

    page = site.get(f"/proposals/{detail['data']['slug']}").text
    assert NESO in page


def test_a_generic_credit_is_not_labelled_source_twice_on_a_record_page(
    client: TestClient, db: Session, site: TestClient
) -> None:
    """`_macros.html` prefixed "Source:" to a credit that already began "Source:" (L-10)."""
    from services.api.conftest import make_attribution_licence, make_public_source, make_visible_proposal

    prop = make_visible_proposal(db, make_public_source(db, make_attribution_licence(db)))
    db.commit()
    page = site.get(f"/proposals/{prop.slug}").text
    text = re.sub(r"<[^>]+>", "", page)
    assert "Source: Test ISO" in text
    assert "Source: Source:" not in text


def test_cc_by_credits_carry_their_licence_link_and_statement_of_changes(registry: Registry) -> None:
    lbnl = registry.sources["us.lbnl.ferc_hifld_transmission_lines"]
    assert lbnl.credit_text is not None
    assert lbnl.credit_text.startswith(
        "Yin, Nait Belaid & Heleno (2026), Lawrence Berkeley National Laboratory, OEDI, CC BY 4.0"
    )
    assert lbnl.credit_text.endswith(
        "Modified by Inframation: HIFLD-side fields only, names re-cased, coordinates rounded."
    )
    assert lbnl.licence_url == "https://creativecommons.org/licenses/by/4.0/"
    world_bank = registry.sources["mdb.worldbank.procnotices"]
    assert world_bank.credit_text is not None and "CC BY 4.0" in world_bank.credit_text
    assert "Modified by Inframation" in world_bank.credit_text
    assert world_bank.licence_url == "https://creativecommons.org/licenses/by/4.0/"


def test_every_loaded_attribution_source_has_a_licence_link(registry: Registry) -> None:
    """The seven attribution licences the audit store held all had `licence.url` NULL (L-10)."""
    loaded = (
        "us.iso.caiso.gen_queue",
        "us.iso.nyiso.gen_queue",
        TEC,
        "eu.ted.api",
        "gb.find_a_tender",
        "mdb.worldbank.procnotices",
        "us.lbnl.ferc_hifld_transmission_lines",
    )
    for source_id in loaded:
        assert registry.sources[source_id].licence_url.startswith("https://"), source_id


# ------------------------------------------- required notices (docs/13 §6.4, 2026-10-22)
# The Texas RRC Site Policies: copiers "must follow Sections 3 and 5 of The State Website Linking and
# Privacy Policy", whose copying paragraph requires the agency, "its Web address, the date the
# information was copied" and "a statement that neither the site owner nor the information, as it is
# presented on the site owner's Web site, is endorsed by the State or any state agency". AEP's terms:
# "Any copy you make must include this copyright notice: Copyright 1996-2026 by American Electric Power
# Company, Inc., all rights reserved." Before 2026-10-22 neither rendered anywhere.
RRC = "us.tx.rrc.class_vi"
RRC_CREDIT = "Source: Railroad Commission of Texas, https://www.rrc.texas.gov"
RRC_NOTICE = (
    "Neither Inframation nor this information, as presented on Inframation, is endorsed by the State of "
    "Texas or any state agency."
)
RRC_FULL = f"{RRC_CREDIT}. {RRC_NOTICE}"
AEP_NOTICE = "Copyright 1996-2026 by American Electric Power Company, Inc., all rights reserved."
AEP_CREDITS = {
    "us.rfp.pso": "Source: Public Service Company of Oklahoma (AEP), Energy RFPs",
    "us.rfp.appalachian_power": "Source: Appalachian Power (AEP), Energy RFPs",
    "us.rfp.swepco": "Source: SWEPCO (AEP), Energy RFPs",
    "us.rfp.indiana_michigan_power": "Source: Indiana Michigan Power (AEP), Energy RFPs",
}


def _entry(**fields: Any) -> Any:
    from pipeline.connectors.registry import SourceEntry

    base = {"id": "t.src", "name": "Test", "url": "https://example.org", "category": "permit"}
    return SourceEntry.from_yaml({**base, "access": "html", "cadence": "weekly", **fields})


def test_a_required_notice_renders_last_after_the_credit_and_any_statement_of_changes() -> None:
    """The general mechanism: `notice` is appended to whatever credit the source has, once, with one
    full stop between, and stands alone for a source whose terms ask no credit."""
    assert _entry(reuse="noncommercial", operator="Agency", notice="Not endorsed.").credit_text == (
        "Source: Agency. Not endorsed."
    )
    both = _entry(
        reuse="attribution", attribution="Doe, CC BY 4.0", changes_statement="Modified.", notice="N."
    )
    assert both.credit_text == "Doe, CC BY 4.0. Modified. N."
    assert _entry(reuse="open", notice="Copyright X.").credit_text == "Copyright X."
    assert _entry(reuse="open").credit_text is None
    assert (
        _entry(reuse="attribution", attribution="A", changes_statement="Changed.").credit_text
        == "A. Changed."
    )


def test_the_texas_rrc_and_aep_credits_carry_the_notices_their_terms_require(registry: Registry) -> None:
    for source_id in (RRC, "us.tx.rrc.datasets"):
        assert registry.sources[source_id].credit_text == RRC_FULL, source_id
    for source_id, credit in AEP_CREDITS.items():
        assert registry.sources[source_id].credit_text == f"{credit}. {AEP_NOTICE}", source_id


def test_the_aep_notice_is_the_clause_the_terms_check_verifies() -> None:
    """The copyright notice is not free text: it is the tail of a `terms_quote` clause that the RFP
    connector re-reads on aep.com/terms (pipeline/connectors/us_rfp/extract.py `terms_check`), so when
    AEP's page changes the year the issuer pauses until the quote and both notices move together."""
    from pipeline.connectors.us_rfp.issuers import load_rows

    rows = load_rows()
    manifest = {e["id"]: e for e in yaml.safe_load(Registry().path.read_text(encoding="utf-8"))["sources"]}
    for source_id in AEP_CREDITS:
        clause = next(q for q in rows[source_id].terms_quotes if "copyright notice:" in q)
        assert clause.split("copyright notice: ", 1)[1] == AEP_NOTICE
        assert rows[source_id].raw["notice"] == manifest[source_id]["notice"] == AEP_NOTICE


def _noncommercial_posture(monkeypatch: pytest.MonkeyPatch) -> None:
    """What a process started under `PLATFORM_POSTURE=noncommercial` computes at import, as
    `tests/test_rfp_publication.py` patches it."""
    import pipeline.connectors.registry as registry_module
    import services.api.visibility as visibility
    import services.ingest.loader as loader
    from services import posture

    for module in (registry_module, loader):
        monkeypatch.setattr(
            module, "PUBLISHABLE_REUSE", frozenset(posture.publishable_reuse_classes("noncommercial"))
        )
        monkeypatch.setattr(module, "GATED_REUSE", frozenset(posture.gated_reuse_classes("noncommercial")))
    monkeypatch.setattr(
        visibility, "PUBLISHABLE_REUSE_CLASSES", posture.publishable_reuse_classes("noncommercial")
    )


def _rrc_row() -> dict[str, Any]:
    return {
        **_neso_row("RRC-1"),
        "record_id": f"{RRC}:CCS-0042",
        "source_id": RRC,
        "source_record_id": "CCS-0042",
        "source_url": "https://www.rrc.texas.gov/media/3ivkqhtb/class_vi_application-09222026.pdf",
        "retrieved_at": "2026-10-19T06:00:00Z",
        "kind": "ccs",
        "name_canonical": "Gulf Coast Storage Hub",
        "technology": "co2_geologic_sequestration",
        "technology_raw": None,
        "capacity_mw": None,
        "state": "TX",
        "lifecycle_state": "studied",
        "status_raw": "Under Review",
        "queue_id": "CCS-0042",
        "raw": json.dumps({"Project Name": "Gulf Coast Storage Hub"}),
    }


def test_rrc_records_carry_agency_address_date_copied_and_no_endorsement_statement_on_every_surface(
    client: TestClient, db: Session, registry: Registry, site: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The four elements of the state copying paragraph, on the record page, the API's attribution
    field, the feed, `/attribution` and `/legal/reuse` (docs/13 §6.4)."""
    _noncommercial_posture(monkeypatch)
    source = upsert_licence_and_source(db, registry.sources[RRC], registry.version)
    load_dataframe(db, source, "proposal", pd.DataFrame([_rrc_row()]), None)
    db.commit()
    from services.db.models import Proposal

    slug = db.query(Proposal).one().slug
    detail = client.get(f"/v1/proposals/{db.query(Proposal).one().public_id}").json()
    provenance = detail["data"]["provenance"][0]
    assert provenance["source_id"] == RRC
    assert provenance["attribution_text"] == RRC_FULL
    assert provenance["retrieved_at"].startswith("2026-10-19")  # the date copied, beside the credit
    assert detail["licence_summary"]["sources"][0]["attribution_text"] == RRC_FULL

    rss = client.get("/feeds/proposals.rss").text
    assert f"<inframation:attribution>{RRC_FULL}</inframation:attribution>" in rss
    assert "<inframation:retrieved_at>2026-10-19" in rss

    page = site.get(f"/proposals/{slug}").text
    lines = [
        re.sub(r"<[^>]+>", "", m) for m in re.findall(r'<p class="attribution-line">(.*?)</p>', page, re.S)
    ]
    # Agency, web address, the no-endorsement statement and the date copied, in one sentence run,
    # with one full stop after the statement (the macro drops the notice's own before "Retrieved").
    assert f"{RRC_FULL} Retrieved 19 Oct 2026." in lines, lines
    assert ".." not in " ".join(lines)

    assert RRC_FULL in site.get("/attribution").text
    reuse = site.get("/legal/reuse").text
    assert f"Keep this notice with every copy: “{RRC_NOTICE}”" in reuse
    assert f"Keep this notice with every copy: “{AEP_NOTICE}”" in reuse
