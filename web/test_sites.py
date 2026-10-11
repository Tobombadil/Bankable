"""Site pages (`web/sites.py`): `/sites/{public_id}` and the proposal page's "At this site" panel.

Same fake-`Transport` pattern as `web/test_head_requests.py`, duplicated rather than imported (no
`web/test_*.py` imports another): `web.app.app` against canned API envelopes, no database.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from web.api_client import ApiClient
from web.app import app as web_app

SITE_ID = "site_1M4KDKH9SEAESTS0B0TG2C5TV"
HEALTH: dict[str, Any] = {
    "status": "ok",
    "data_as_of": "2026-09-01",
    "live_as_of": "2026-09-15T00:00:00Z",
    "lag_days_default": {"supply": 0, "opportunities": 0},
}
VOCAB: dict[str, Any] = {
    "data": {
        "technology": [{"value": "solar"}],
        "proposal_kind": [{"value": "generation"}],
        "opportunity_kind": [{"value": "rfp"}],
        "opportunity_status": [{"value": "open"}],
    }
}
PROV = {
    "source_id": "us.eia.860m",
    "source_name": "EIA-860M",
    "source_url": "https://example.org/eia",
    "retrieved_at": "2026-10-09T00:00:00Z",
    "reuse_class": "open",
    "attribution_text": None,
    "source_record_id": "69798-PMN1",
}


def _row(
    pid: str,
    name: str,
    *,
    relation: str,
    mw: float,
    tech: str,
    plants: list[str],
    parent: str | None = None,
    lead: bool = False,
) -> dict[str, Any]:
    return {
        "public_id": pid,
        "slug": pid.lower(),
        "url": f"https://infraque.com/proposals/{pid.lower()}",
        "name_canonical": name,
        "kind": "generation",
        "technology": tech,
        "capacity_mw": mw,
        "lifecycle_state": "announced",
        "lifecycle_bucket": "active",
        "jurisdiction": "US-TX",
        "proposed_online_date": None,
        "sponsor": {
            "public_id": "org_FERMI",
            "slug": "fermi-america",
            "name_canonical": "Fermi America",
            "type": "developer",
            "url": "https://infraque.com/organizations/fermi-america",
            "personal_data": False,
        },
        "provenance": [PROV],
        "is_lead": lead,
        "group": {"key": "eia:" + ",".join(plants), "eia_plant_ids": plants},
        "parent_public_id": parent,
        "relation": relation,
        "relation_rule": "x",
        "confidence": "high",
        "grouping_rule": "eia_plant",
    }


def _site(extra_units: int = 0) -> dict[str, Any]:
    members = [
        _row(
            "PROP_N1",
            "Project Matador Nuclear",
            relation="lead",
            mw=1117,
            tech="nuclear",
            plants=["69798"],
            lead=True,
        ),
        _row(
            "PROP_N2",
            "Project Matador Nuclear",
            relation="unit_of",
            mw=1117,
            tech="nuclear",
            plants=["69798"],
            parent="PROP_N1",
        ),
        _row(
            "PROP_G1",
            "Project Matador Gas Plant",
            relation="co_located",
            mw=260,
            tech="gas_ct",
            plants=["69799"],
        ),
        _row(
            "PROP_G2",
            "Project Matador Gas Plant",
            relation="unit_of",
            mw=40,
            tech="gas_ct",
            plants=["69799"],
            parent="PROP_G1",
        ),
    ]
    members += [
        _row(
            f"PROP_X{i}",
            "Project Matador Gas Plant",
            relation="unit_of",
            mw=40,
            tech="gas_ct",
            plants=["69799"],
            parent="PROP_G1",
        )
        for i in range(extra_units)
    ]
    neighbour = _row(
        "PROP_NB", "Westlands Solar", relation="shares_interconnection_point", mw=900, tech="solar", plants=[]
    )
    neighbour["interconnection_point"] = {
        "public_id": "poi_1",
        "url": "https://infraque.com/interconnection-points/poi_1",
        "name": "Midway 500kV",
    }
    return {
        "public_id": SITE_ID,
        "url": f"https://infraque.com/sites/{SITE_ID}",
        "name": "Project Matador Nuclear",
        "rule_version": "2026-10-10.1",
        "member_count": len(members),
        "lead": members[0],
        "members": members,
        "members_truncated": False,
        "totals": {"member_count": len(members), "total_mw": 2534.0, "active_count": len(members), "active_mw": 2534.0, "withdrawn_count": 0, "withdrawn_mw": 0.0, "built_count": 0, "built_mw": 0.0, "other_count": 0, "other_mw": 0.0},  # noqa: E501
        "sponsors": [{**members[0]["sponsor"], "member_count": len(members)}],
        "anchors": {
            "eia_plant_ids": ["69798", "69799"],
            "interconnection_points": [{"public_id": "poi_1", "url": "https://infraque.com/interconnection-points/poi_1", "name": "Midway 500kV"}],  # noqa: E501
            "assets": [
                {
                    "public_id": "asset_1", "slug": "riverton-us-ks", "url": "https://infraque.com/assets/riverton-us-ks",
                    "asset_type": "power_plant", "name": "Riverton", "eia_plant_id": "69799",
                    "owners": [{"organization": {"public_id": "org_LU", "slug": "liberty", "name_canonical": "Liberty Utilities Co", "url": "https://infraque.com/organizations/liberty"}, "role": "owner"}],  # noqa: E501
                }
            ],
        },
        "shares_interconnection_point": [neighbour],
        "shares_interconnection_point_count": 1,
    }  # fmt: skip


PROPOSAL: dict[str, Any] = {
    "public_id": "PROP_G2",
    "slug": "prop_g2",
    "name_canonical": "Project Matador Gas Plant",
    "kind": "generation",
    "technology": "gas_ct",
    "capacity_mw": 40.0,
    "jurisdiction": "US-TX",
    "lifecycle_state": "announced",
    "status_raw": "P",
    "identifiers": {},
    "proposed_online_date": None,
    "schedule_slip": None,
    "source_count": 1,
    "provenance": [PROV],
}
EMBED = {
    "public_id": SITE_ID,
    "url": f"https://infraque.com/sites/{SITE_ID}",
    "name": "Project Matador Nuclear",
    "member_count": 4,
    "is_lead": False,
    "parent_public_id": "PROP_G1",
    "relation": "unit_of",
    "relation_rule": "shared_eia_plant",
    "confidence": "high",
    "grouping_rule": "eia_plant",
    "lead": {
        "public_id": "PROP_N1",
        "slug": "prop_n1",
        "url": "https://infraque.com/proposals/prop_n1",
        "name_canonical": "Project Matador Nuclear",
    },
}


def _list_env(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "data": rows,
        "meta": {"total": len(rows)},
        "page": {"next_cursor": None, "has_more": False},
        "licence_summary": {"sources": []},
    }


class FakeTransport:
    def __init__(self, responses: Mapping[str, tuple[int, Any]]) -> None:
        self.responses = dict(responses)
        self.calls: list[str] = []
        self.params: list[tuple[str, dict[str, Any]]] = []
        self.posted: list[tuple[str, Any]] = []

    def get(
        self, url: str, *, params: Mapping[str, Any] | None = None, cookies: dict[str, str] | None = None
    ) -> httpx.Response:
        self.calls.append(url)
        self.params.append((url, dict(params or {})))
        if url in self.responses:
            status, body = self.responses[url]
            return httpx.Response(status, json=body)
        return httpx.Response(404, json={"title": "not_found", "detail": url})

    def post(
        self, url: str, *, json: Mapping[str, Any] | None = None, cookies: dict[str, str] | None = None
    ) -> httpx.Response:
        self.calls.append(url)
        self.posted.append((url, json))
        return httpx.Response(202, json={})

    def request(self, method: str, url: str, **_: Any) -> httpx.Response:
        return self.get(url)

    def close(self) -> None:
        pass


def _transport(site: dict[str, Any] | None = None, embed: dict[str, Any] | None = EMBED) -> FakeTransport:
    site = site if site is not None else _site()
    return FakeTransport(
        {
            "/v1/health": (200, HEALTH),
            "/v1/meta/vocabularies": (200, VOCAB),
            "/v1/sources": (200, {"data": []}),
            "/v1/sources/us.eia.860m": (
                200,
                {"data": {"licence": {"url": "https://example.org/l", "quote_text": "Public domain."}}},
            ),
            "/v1/proposals": (200, _list_env([PROPOSAL])),
            "/v1/proposals/PROP_G2": (200, {"data": {**PROPOSAL, "site": embed}}),
            "/v1/proposals/PROP_G2/events": (200, _list_env([])),
            f"/v1/sites/{SITE_ID}": (
                200,
                {
                    "data": site,
                    "licence_summary": {
                        "sources": [
                            {
                                "source_id": "us.eia.860m",
                                "name": "EIA-860M",
                                "reuse_class": "open",
                                "attribution_text": None,
                            }
                        ]
                    },
                },
            ),
            "/v1/ui-events": (202, {}),
        }
    )


@pytest.fixture()
def install() -> Iterator[Any]:
    def _install(transport: FakeTransport) -> FakeTransport:
        web_app.state.api_client = ApiClient(transport)
        web_app.state.lag_days_default = None
        return transport

    yield _install
    for key in ("api_client", "lag_days_default"):
        web_app.state.__dict__.pop(key, None)


@pytest.fixture()
def client(install: Any) -> Iterator[TestClient]:
    with TestClient(web_app) as c:
        yield c


def test_the_site_page_lists_groups_units_sponsors_anchors_and_neighbours(
    client: TestClient, install: Any
) -> None:
    install(_transport())
    resp = client.get(f"/sites/{SITE_ID}")
    assert resp.status_code == 200
    body = resp.text
    assert "<h1>Project Matador Nuclear</h1>" in body
    assert "Lead filing" in body and "Co-located, other technology" in body
    assert body.count("Unit of EIA plant 69798") == 1 and body.count("Unit of EIA plant 69799") == 1
    assert body.count('class="site-row--unit"') == 2
    assert '<a href="/organizations/fermi-america">Fermi America</a>' in body
    assert '<a href="/assets/riverton-us-ks">Riverton</a>' in body
    assert '<a href="/organizations/liberty">Liberty Utilities Co</a>' in body
    assert '<a href="/interconnection-points/poi_1">Midway 500kV</a>' in body
    assert "69798, 69799" in body
    # Neighbours are a separate, collapsed list, never rows of the members table.
    neighbours = body[body.index("Also at this interconnection point") :]
    assert "<details" in neighbours and "Westlands Solar" in neighbours
    assert "Westlands Solar" not in body[: body.index("Also at this interconnection point")]
    assert f'<link rel="canonical" href="http://testserver/sites/{SITE_ID}">' in body
    assert '"BreadcrumbList"' in body
    assert "EIA-860M" in body  # the Sources panel and the credit line


def test_the_site_page_is_accessible_by_structure(client: TestClient, install: Any) -> None:
    install(_transport())
    body = client.get(f"/sites/{SITE_ID}").text
    assert len(re.findall(r"<h1[ >]", body)) == 1
    for table in re.findall(r"<table.*?</table>", body, re.S):
        assert "<caption" in table
        assert all('scope="col"' in th for th in re.findall(r"<th(?:\s[^>]*)?>", table))
    for link in re.findall(r"<a [^>]*>(.*?)</a>", body, re.S):
        assert link.strip(), "every link has text"


def test_the_proposal_page_shows_the_site_panel(client: TestClient, install: Any) -> None:
    install(_transport())
    body = client.get("/proposals/prop_g2").text
    panel = body[body.index('id="site-panel-heading"') :]
    assert f'href="/sites/{SITE_ID}"' in panel
    assert panel.index("Project Matador Nuclear") < panel.index("Project Matador Gas Plant")
    assert 'aria-current="true"' in panel and "this record" in panel
    assert "<summary>Also at this interconnection point (1)</summary>" in panel
    assert "Sources of these rows: EIA-860M" in panel


def test_the_panel_asks_for_its_own_records_linked_group(client: TestClient, install: Any) -> None:
    """When a hidden record is all that links some visible members to the rest, the API serves one
    linked group; the panel asks for the group holding this record, not the site page's largest."""
    transport = install(_transport())
    client.get("/proposals/prop_g2")
    assert (f"/v1/sites/{SITE_ID}", {"member": "PROP_G2"}) in transport.params


def test_a_partial_site_says_so_on_the_page_and_the_panel(client: TestClient, install: Any) -> None:
    install(_transport(site={**_site(), "partial": True}))
    page = client.get(f"/sites/{SITE_ID}").text
    assert "This page lists the largest group of linked records at this site." in page
    panel = client.get("/proposals/prop_g2").text
    assert "Other records at this site are not linked to these by anything shown here." in panel
    install(_transport())
    assert "largest group of linked records" not in client.get(f"/sites/{SITE_ID}").text


def test_a_site_page_view_is_counted_as_a_site(client: TestClient, install: Any) -> None:
    """The beta is measured by use: a site page counts as `page.viewed {page_type: site}`."""
    transport = install(_transport())
    assert client.get(f"/sites/{SITE_ID}").status_code == 200
    assert ("/v1/ui-events", {"name": "page.viewed", "props": {"page_type": "site"}}) in transport.posted


def test_a_large_site_summarises_units_in_the_panel(client: TestClient, install: Any) -> None:
    install(_transport(site=_site(extra_units=30)))
    body = client.get("/proposals/prop_g2").text
    panel = body[body.index('id="site-panel-heading"') :]
    assert "Plus 31 more units of EIA plant 69799, this record among them" in panel
    assert panel.count("PROP_X") == 0


def test_no_site_means_no_panel(client: TestClient, install: Any) -> None:
    install(_transport(embed=None))
    body = client.get("/proposals/prop_g2").text
    assert "site-panel-heading" not in body


def test_the_kill_switch_hides_the_panel_and_the_page(
    client: TestClient, install: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    transport = install(_transport())
    monkeypatch.setenv("SITES_ENABLED", "off")
    assert "site-panel-heading" not in client.get("/proposals/prop_g2").text
    assert client.get(f"/sites/{SITE_ID}").status_code == 404
    assert f"/v1/sites/{SITE_ID}" not in transport.calls
    monkeypatch.setenv("SITES_ENABLED", "on")
    assert "site-panel-heading" in client.get("/proposals/prop_g2").text


def test_unknown_retired_and_failing_sites(client: TestClient, install: Any) -> None:
    transport = install(_transport())
    assert client.get("/sites/site_UNKNOWN").status_code == 404
    # A retired id: the API client follows the 301, so the envelope names the successor.
    transport.responses["/v1/sites/site_OLD"] = (200, {"data": _site(), "licence_summary": {"sources": []}})
    resp = client.get("/sites/site_OLD", follow_redirects=False)
    assert (resp.status_code, resp.headers["location"]) == (301, f"/sites/{SITE_ID}")
    transport.responses["/v1/sites/site_BROKEN"] = (500, {"title": "boom"})
    assert client.get("/sites/site_BROKEN").status_code == 503
    # A failing site call drops the panel, never the proposal page.
    transport.responses[f"/v1/sites/{SITE_ID}"] = (500, {"title": "boom"})
    resp = client.get("/proposals/prop_g2")
    assert resp.status_code == 200 and "site-panel-heading" not in resp.text
