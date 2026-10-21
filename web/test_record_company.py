"""The company on every record (docs/16-ux-review-2026-10-20.md U-2, U-20, U-21, U-23).

* U-2: a record's first lines name the company behind it, linked to its company page, with its
  direct parent where one is recorded (as an ownership claim: source and date beside it); the
  project list, a grid point's projects and a site's records carry a Company column; an RFP names
  its issuer, linked.
* U-20: an asset page names its operator and owners in the header, linked.
* U-21: a company page hides an empty RFP section, drops the "Other" badges and shows its portfolio
  and subsidiaries as one Group table.
* U-23: an RFP record prints every date one way.

A natural person named in a register (`personal_data`) keeps their privacy handling: their company
page is `noindex`, so every link to it says `nofollow`, the line says "Sponsor", and nothing reads
who owns them or whom they own.

Same pattern as `web/test_proposal_fields.py`: `web.app.app` against a hand-written fake
`Transport`, no database; the fake is duplicated rather than imported (no `web/test_*.py` imports
another).
"""

from __future__ import annotations

import pathlib
import re
from collections.abc import Iterator, Mapping
from typing import Any

import httpx
import jsonschema
import pytest
import yaml
from fastapi.testclient import TestClient

from web.api_client import ApiClient
from web.app import app as web_app
from web.viewmodels import flatten_opportunity, flatten_proposal, org_ref

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
        "opportunity_technology": [{"value": "solar"}],
        "slip_bucket": [],
    }
}
PROV: dict[str, Any] = {
    "source_id": "us.eia.860m",
    "source_name": "EIA-860M",
    "source_url": "https://www.eia.gov/electricity/data/eia860m/",
    "retrieved_at": "2026-10-09T00:00:00Z",
    "reuse_class": "open",
    "attribution_text": "EIA-860M",
    "source_record_id": "69778",
}


class FakeTransport:
    def __init__(self, responses: Mapping[str, tuple[int, Any]] | None = None) -> None:
        self.responses: dict[str, tuple[int, Any]] = dict(responses or {})
        self.calls: list[tuple[str, str, Any]] = []

    def _respond(self, url: str) -> httpx.Response:
        if url in self.responses:
            status, body = self.responses[url]
            return httpx.Response(status, json=body)
        return httpx.Response(404, json={"title": "not_found", "detail": url})

    def get(
        self, url: str, *, params: Mapping[str, Any] | None = None, cookies: dict[str, str] | None = None
    ) -> httpx.Response:
        self.calls.append(("GET", url, dict(params or {})))
        return self._respond(url)

    def post(
        self, url: str, *, json: Mapping[str, Any] | None = None, cookies: dict[str, str] | None = None
    ) -> httpx.Response:
        self.calls.append(("POST", url, json))
        return self._respond(url)

    def request(
        self,
        method: str,
        url: str,
        *,
        json: Mapping[str, Any] | None = None,
        params: Mapping[str, Any] | None = None,
        cookies: dict[str, str] | None = None,
    ) -> httpx.Response:
        self.calls.append((method, url, json if json is not None else dict(params or {})))
        return self._respond(url)

    def close(self) -> None:
        pass


@pytest.fixture()
def client() -> Iterator[TestClient]:
    with TestClient(web_app) as c:
        yield c
    for key in ("api_client", "lag_days_default", "coverage_facts", "build_info", "sitemap_cache"):
        web_app.state.__dict__.pop(key, None)


def _install(responses: Mapping[str, tuple[int, Any]]) -> FakeTransport:
    transport = FakeTransport(
        {
            "/v1/health": (200, HEALTH),
            "/v1/meta/vocabularies": (200, VOCAB),
            "/v1/sources": (200, {"data": []}),
            **responses,
        }
    )
    web_app.state.api_client = ApiClient(transport)
    web_app.state.lag_days_default = None
    return transport


def _list(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "data": rows,
        "meta": {"total": len(rows), "total_is_estimate": False},
        "page": {"next_cursor": None, "prev_cursor": None, "has_more": False},
        "licence_summary": [],
    }


def _org(public_id: str, slug: str, name: str, *, person: bool = False) -> dict[str, Any]:
    return {
        "public_id": public_id,
        "slug": slug,
        "name_canonical": name,
        "type": "other",
        "url": f"https://inframation.io/organizations/{slug}",
        "personal_data": person,
    }


SERVICES = _org("org_SVC", "invenergy-services-llc", "Invenergy Services LLC")
INVENERGY = _org("org_INV", "invenergy-llc", "INVENERGY LLC")
PERSON = _org("org_PERSON", "jordan-example", "JORDAN EXAMPLE", person=True)


def _proposal(sponsor: dict[str, Any] | None = SERVICES, **extra: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "public_id": "prop_HK",
        "slug": "hashknife-energy-center-llc-ba9gw3",
        "name_canonical": "Hashknife Energy Center LLC",
        "kind": "generation",
        "technology": "solar",
        "capacity_mw": 275.0,
        "jurisdiction": "US-AZ",
        "iso": "AZPS",
        "lifecycle_state": "under_construction",
        "identifiers": {},
        "source_count": 1,
        "sponsor": sponsor,
        "provenance": [PROV],
    }
    row.update(extra)
    return row


def _sponsor_detail(parent: dict[str, Any] | None, as_of: str | None = None) -> dict[str, Any]:
    edge = (
        {
            "organization": parent,
            "source_id": "curated.organization_parents",
            "as_of": as_of,
            "share_pct": None,
        }
        if parent
        else None
    )
    return {"data": {**SERVICES, "parent": parent, "parent_edge": edge, "ancestors": [edge] if edge else []}}


def _proposal_routes(row: dict[str, Any], detail: dict[str, Any] | None = None) -> dict[str, tuple[int, Any]]:
    routes: dict[str, tuple[int, Any]] = {
        "/v1/proposals": (200, _list([row])),
        f"/v1/proposals/{row['public_id']}": (200, {"data": row}),
        f"/v1/proposals/{row['public_id']}/events": (200, _list([])),
    }
    if detail is not None:
        routes["/v1/organizations/org_SVC"] = (200, detail)
    return routes


def _header(body: str) -> str:
    """The record's first lines: from the H1 to the field grid."""
    return body[body.index("<h1") : body.index('<dl class="field-grid"')]


# ---------------------------------------------------------------------------- the view model
def test_org_ref_links_the_company_page_by_slug_else_public_id() -> None:
    assert org_ref(SERVICES) == {
        "name": "Invenergy Services LLC",
        "public_id": "org_SVC",
        "slug": "invenergy-services-llc",
        "href": "/organizations/invenergy-services-llc",
        "personal_data": False,
    }
    no_slug = org_ref({"public_id": "org_X", "name_canonical": "X Co"})
    assert no_slug is not None and no_slug["href"] == "/organizations/org_X"
    assert org_ref(None) is None and org_ref({"public_id": "org_X"}) is None


def test_flatten_proposal_keeps_the_sponsor_slug() -> None:
    """docs/16 §0 item 2: `web/viewmodels.py` kept only the sponsor's name, though the API sends
    its slug."""
    record = flatten_proposal(_proposal())
    assert record["sponsor"] == "Invenergy Services LLC"
    assert record["sponsor_slug"] == "invenergy-services-llc"
    assert record["sponsor_public_id"] == "org_SVC"
    assert record["company"]["href"] == "/organizations/invenergy-services-llc"
    assert record["company_parent"] is None  # a list row reads no parent


def test_flatten_opportunity_links_a_linked_issuer() -> None:
    record = flatten_opportunity(
        {
            "public_id": "opp_1",
            "slug": "o",
            "title": "T",
            "issuer": _org("org_APS", "aps", "Arizona Public Service"),
        }
    )
    assert record["issuer"] == "Arizona Public Service"
    assert record["issuer_org"]["href"] == "/organizations/aps"


# ---------------------------------------------------------------------------- the project page
def test_the_project_header_names_the_company_and_its_parent_with_the_claim(client: TestClient) -> None:
    transport = _install(_proposal_routes(_proposal(), _sponsor_detail(INVENERGY)))
    body = client.get("/proposals/hashknife-energy-center-llc-ba9gw3").text
    header = _header(body)
    line = header[header.index('id="record-company"') :].split("</p>")[0]
    assert 'Company</span> <a href="/organizations/invenergy-services-llc">Invenergy Services LLC</a>' in line
    # The parent's name as the reader sees it (U-22), linked, then the claim's source and date.
    assert 'part of <a href="/organizations/invenergy-llc">Invenergy LLC</a>' in line
    claim = header[header.index('id="record-company-claim"') :].split("</p>")[0]
    assert "no date stated by the source" in claim and "ownership-claim--undated" in header
    assert ("GET", "/v1/organizations/org_SVC", {}) in transport.calls
    # The grid no longer repeats the sponsor (docs/16 §4.2: the grid without the header's fields).
    assert "<dt>Sponsor</dt>" not in body
    # U-14: the balancing authority by name.
    assert "<dt>ISO / operator</dt><dd>Arizona Public Service (AZPS)</dd>" in body


def test_a_dated_parent_claim_says_its_date(client: TestClient) -> None:
    _install(_proposal_routes(_proposal(), _sponsor_detail(INVENERGY, as_of="2024-06-30")))
    header = _header(client.get("/proposals/hashknife-energy-center-llc-ba9gw3").text)
    assert "Ownership stated as of 2024-06-30" in header
    assert "ownership-claim--undated" not in header


def test_no_parent_and_a_failed_read_both_leave_the_company_line_alone(client: TestClient) -> None:
    _install(_proposal_routes(_proposal(), _sponsor_detail(None)))
    resp = client.get("/proposals/hashknife-energy-center-llc-ba9gw3")
    assert resp.status_code == 200
    header = _header(resp.text)
    assert "Invenergy Services LLC" in header and "part of" not in header
    assert 'id="record-company-claim"' not in header

    _install({**_proposal_routes(_proposal()), "/v1/organizations/org_SVC": (503, {"title": "unavailable"})})
    resp = client.get("/proposals/hashknife-energy-center-llc-ba9gw3")
    assert resp.status_code == 200 and "part of" not in _header(resp.text)


def test_a_record_with_no_sponsor_says_none_is_recorded(client: TestClient) -> None:
    """Not "not named in the source": the API serves no sponsor for a company that is not public
    too, and the register may well name one."""
    transport = _install(_proposal_routes(_proposal(sponsor=None)))
    header = _header(client.get("/proposals/hashknife-energy-center-llc-ba9gw3").text)
    assert 'Company</span> <span class="record-company__none">not recorded</span>' in header
    assert not [c for c in transport.calls if c[1].startswith("/v1/organizations/")]


def test_a_natural_person_keeps_their_privacy_handling(client: TestClient) -> None:
    """A person named in a register: labelled "Sponsor", the link to their `noindex` page says
    `nofollow`, and no ownership read is made about them."""
    transport = _install(_proposal_routes(_proposal(sponsor=PERSON)))
    header = _header(client.get("/proposals/hashknife-energy-center-llc-ba9gw3").text)
    assert (
        'Sponsor</span> <a href="/organizations/jordan-example" rel="nofollow">Jordan Example</a>' in header
    )
    assert "part of" not in header
    assert not [c for c in transport.calls if c[1].startswith("/v1/organizations/")]


def test_a_parent_who_is_a_person_is_not_named(client: TestClient) -> None:
    _install(_proposal_routes(_proposal(), _sponsor_detail(PERSON)))
    header = _header(client.get("/proposals/hashknife-energy-center-llc-ba9gw3").text)
    assert "Invenergy Services LLC" in header
    assert "part of" not in header and "Jordan" not in header


def test_the_project_list_has_a_linked_company_column(client: TestClient) -> None:
    rows = [
        _proposal(),
        _proposal(sponsor=None, public_id="prop_2", slug="two"),
        _proposal(sponsor=PERSON, public_id="prop_3", slug="three"),
    ]
    _install({"/v1/proposals": (200, _list(rows))})
    html = client.get("/proposals").text
    table = html[html.index('<table class="record-table">') :].split("</table>")[0]
    heads = re.findall(r'<th scope="col"[^>]*>([^<]+)</th>', table)
    assert heads[:3] == ["Status", "Name", "Company"]
    cells = re.findall(r'<td data-label="Company">(.*?)</td>', table, re.S)
    assert cells == [
        '<a href="/organizations/invenergy-services-llc">Invenergy Services LLC</a>',
        "—",
        '<a href="/organizations/jordan-example" rel="nofollow">Jordan Example</a>',
    ]


# ---------------------------------------------------------------------------- the RFP page
def _opportunity(**extra: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "public_id": "opp_1",
        "slug": "aps-all-source-rfp",
        "title": "APS 2026 All-Source RFP",
        "kind": "rfp",
        "status": "open",
        "open_at": "2025-01-08",
        "due_at": "2026-11-30",
        "issuer": _org("org_APS", "arizona-public-service", "ARIZONA PUBLIC SERVICE COMPANY"),
        "identifiers": {},
        "provenance": [PROV],
    }
    row.update(extra)
    return row


def test_the_rfp_header_links_its_issuer_and_prints_one_date_format(client: TestClient) -> None:
    _install({"/v1/opportunities": (200, _list([_opportunity()]))})
    body = client.get("/opportunities/aps-all-source-rfp").text
    header = _header(body)
    assert (
        'Issuer</span> <a href="/organizations/arizona-public-service">Arizona Public Service Company</a>'
        in header
    )
    assert "<dt>Issuer</dt>" not in body  # the header carries it
    # U-23: "Opens 2025-01-08" sat beside "Due 30 Nov 2026"; the ISO date stays in `datetime` only.
    assert '<dt>Opens</dt><dd class="tnum">8 Jan 2025</dd>' in body
    assert '<dt>Due</dt><dd class="tnum">30 Nov 2026</dd>' in body
    assert '<time datetime="2025-01-08">8 Jan 2025</time>' in body
    assert ">2025-01-08<" not in body


def test_an_unlinked_issuer_falls_back_to_the_agency_code(client: TestClient) -> None:
    _install(
        {
            "/v1/opportunities": (
                200,
                _list([_opportunity(issuer=None, identifiers={"agency_code": "DOE-GFO"})]),
            )
        }
    )
    header = _header(client.get("/opportunities/aps-all-source-rfp").text)
    assert 'Issuer</span> <span class="mono">DOE-GFO</span>' in header and "(agency code)" in header


def test_a_named_issuer_keeps_its_agency_code_beside_it(client: TestClient) -> None:
    """docs/16 §4.5: the agency name, with the code in mono beside it; the search snippet reads the
    same name the header does, not the register's capitals."""
    _install({"/v1/opportunities": (200, _list([_opportunity(identifiers={"agency_code": "APS-RFP"})]))})
    body = client.get("/opportunities/aps-all-source-rfp").text
    link = '<a href="/organizations/arizona-public-service">Arizona Public Service Company</a>'
    assert f'Issuer</span> {link} <span class="mono">APS-RFP</span>' in _header(body)
    meta = re.search(r'<meta name="description" content="([^"]*)"', body)
    assert meta and "(RFP) from Arizona Public Service Company, due 30 Nov 2026." in meta.group(1)
    assert "ARIZONA PUBLIC SERVICE COMPANY" not in body.split("</head>")[0]


def test_the_rfp_list_links_a_linked_issuer(client: TestClient) -> None:
    _install({"/v1/opportunities": (200, _list([_opportunity()]))})
    html = client.get("/opportunities").text
    link = '<a href="/organizations/arizona-public-service">Arizona Public Service Company</a>'
    assert f'<td data-label="Issuer">{link}</td>' in html


# ---------------------------------------------------------------------------- a grid point
def _point(proposals: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "data": {
            "public_id": "poi_1",
            "name": "Windhub 500kV",
            "iso": "CAISO",
            "kind": "substation",
            "voltage_kv": 500,
            "totals": {"active_count": len(proposals), "active_mw": 100.0, "by_technology": []},
            "provenance": [PROV],
            "proposals": proposals,
            "recent_changes": [
                {
                    "id": 1,
                    "event_type": "created",
                    "observed_at": "2026-10-09T00:00:00Z",
                    "after": {"lifecycle_state": "filed"},
                    "subject": {"name": "Point Solar", "url": "https://inframation.io/proposals/point-solar"},
                    "provenance": PROV,
                }
            ],
        }
    }


SPEC = yaml.safe_load((pathlib.Path(__file__).resolve().parents[1] / "api" / "openapi.yaml").read_text())
#: A grid point's sponsor with an id the spec's pattern accepts (the row is checked against it).
POINT_SPONSOR = _org("org_01JBQ8C4X1", "invenergy-services-llc", "Invenergy Services LLC")
POINT_PERSON = _org("org_01JBQ8C4X2", "jordan-example", "JORDAN EXAMPLE", person=True)


def _point_row(sponsor: dict[str, Any] | None) -> dict[str, Any]:
    """One `proposals[]` row exactly as `GET /v1/interconnection-points/{id}` sends it
    (`services/api/interconnection_points.py::_proposal_row`): every key the spec's
    `InterconnectionPointProposal` names and no other, with `sponsor` `null` where there is none
    or the tier may not see it. `tests/test_api_interconnection_points.py` holds the API to the same
    key set, so this fake cannot carry a field the API does not send."""
    row: dict[str, Any] = {
        "public_id": "prop_01JBQ7Z8KD",
        "slug": "point-solar",
        "url": "https://inframation.io/proposals/point-solar",
        "name_canonical": "Point Solar",
        "technology": "solar",
        "capacity_mw": 100.0,
        "lifecycle_state": "filed",
        "lifecycle_bucket": "active",
        "jurisdiction": "US-CA",
        "proposed_online_date": "2028-06-01",
        "sponsor": _summary_of(sponsor) if sponsor is not None else None,
    }
    schema = SPEC["components"]["schemas"]["InterconnectionPointProposal"]
    assert set(row) == set(schema["properties"])
    resolver = jsonschema.validators.RefResolver.from_schema(SPEC)
    jsonschema.validators.validator_for(schema)(schema, resolver=resolver).validate(row)
    return row


def test_a_grid_points_projects_carry_a_company_column_when_rows_name_one(client: TestClient) -> None:
    rows = [_point_row(POINT_SPONSOR), {**_point_row(POINT_PERSON), "slug": "person-solar"}]
    rows.append({**_point_row(None), "slug": "unnamed-solar"})
    _install({"/v1/interconnection-points/poi_1": (200, _point(rows))})
    body = client.get("/interconnection-points/poi_1").text
    assert '<th scope="col">Company</th>' in body
    link = '<a href="/organizations/invenergy-services-llc">Invenergy Services LLC</a>'
    assert f'<td data-label="Company">{link}</td>' in body
    # A natural person: linked with nofollow (their page is `noindex`); a row with none: a dash.
    person = '<a href="/organizations/jordan-example" rel="nofollow">Jordan Example</a>'
    assert f'<td data-label="Company">{person}</td>' in body
    assert '<td data-label="Company">—</td>' in body
    # docs/31 §4: one date format (the proposed date and the change's observation date).
    assert '<td data-label="Proposed online" class="tnum">1 Jun 2028</td>' in body
    assert '<time datetime="2026-10-09">9 Oct 2026</time>' in body


def test_a_grid_point_without_sponsors_shows_no_empty_company_column(client: TestClient) -> None:
    _install({"/v1/interconnection-points/poi_1": (200, _point([_point_row(None)]))})
    body = client.get("/interconnection-points/poi_1").text
    assert "Point Solar" in body and '<th scope="col">Company</th>' not in body


# ---------------------------------------------------------------------------- a site
SITE_ID = "site_1"


def _site_row(pid: str, name: str, sponsor: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {
        "public_id": pid,
        "slug": pid.lower(),
        "url": f"https://inframation.io/proposals/{pid.lower()}",
        "name_canonical": name,
        "technology": "gas_cc",
        "capacity_mw": 360.0,
        "lifecycle_state": "announced",
        "sponsor": sponsor,
        "provenance": [PROV],
        **extra,
    }


def _site(sponsors: list[dict[str, Any]]) -> dict[str, Any]:
    lead = _site_row("PROP_A", "Homer City Generating Station", sponsors[0], is_lead=True, relation="lead")
    neighbour = _site_row("PROP_N", "Neighbour Solar", sponsors[-1], relation="shares_interconnection_point")
    return {
        "data": {
            "public_id": SITE_ID,
            "name": "Homer City Generating Station",
            "member_count": 1,
            "members": [lead],
            "totals": {"active_count": 1, "active_mw": 360.0},
            "sponsors": [{**o, "member_count": 1} for o in sponsors],
            "anchors": {},
            "shares_interconnection_point": [neighbour],
            "shares_interconnection_point_count": 1,
        }
    }


def test_the_site_header_names_its_companies_and_its_tables_say_company(client: TestClient) -> None:
    homer = _org("org_HC", "homer-city-generation-l-p", "Homer City Generation, L.P.")
    _install({f"/v1/sites/{SITE_ID}": (200, _site([homer, SERVICES]))})
    body = client.get(f"/sites/{SITE_ID}").text
    line = body[body.index('id="record-company"') :].split("</p>")[0]
    assert "Companies</span>" in line
    assert '<a href="/organizations/homer-city-generation-l-p">Homer City Generation, L.P.</a>' in line
    assert '<a href="/organizations/invenergy-services-llc">Invenergy Services LLC</a>' in line
    assert "<dt>Sponsors</dt>" not in body  # moved to the header
    members = body[body.index("Records at this site") :].split("</table>")[0]
    assert '<th scope="col">Company</th>' in members and "Sponsor" not in members
    neighbours = body[body.index("Also at this interconnection point") :].split("</table>")[0]
    assert '<td data-label="Company"><a href="/organizations/invenergy-services-llc">' in neighbours


def test_a_site_sponsored_by_a_person_says_sponsor_and_nofollow(client: TestClient) -> None:
    _install({f"/v1/sites/{SITE_ID}": (200, _site([PERSON]))})
    body = client.get(f"/sites/{SITE_ID}").text
    line = body[body.index('id="record-company"') :].split("</p>")[0]
    assert 'Sponsor</span> <a href="/organizations/jordan-example" rel="nofollow">Jordan Example</a>' in line


# ---------------------------------------------------------------------------- an asset (U-20)
def _owner(org: dict[str, Any], role: str, share: float | None = None) -> dict[str, Any]:
    return {"organization": org, "role": role, "share_pct": share, "as_of": None, "provenance": PROV}


def _asset(owners: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    entity: dict[str, Any] = {
        "public_id": "asset_1",
        "slug": "scherer-us-ga",
        "name": "Scherer",
        "asset_type": "power_plant",
        "status": "operating",
        "operator_name": None,
        "technology": "coal",
        "capacity_mw": 3564.0,
        "state_code": "US-GA",
        "attributes": {},
        "owners": owners,
        "provenance": [PROV],
    }
    entity.update(extra)
    return entity


def _asset_routes(entity: dict[str, Any]) -> dict[str, tuple[int, Any]]:
    return {
        "/v1/assets": (200, _list([entity])),
        "/v1/assets/asset_1": (200, {"data": entity}),
        "/v1/assets/asset_1/nearby-proposals": (200, {"data": []}),
    }


def _asset_line(body: str) -> str:
    return body[body.index('id="record-company"') :].split("</p>")[0]


def test_the_asset_header_names_its_operator_and_owners_linked(client: TestClient) -> None:
    georgia = _org("org_GP", "georgia-power-co", "GEORGIA POWER COMPANY")
    fpl = _org("org_FPL", "florida-power-light-co", "Florida Power & Light Co")
    dalton = _org("org_DU", "dalton-utilities", "Dalton Utilities")
    owners = [
        _owner(georgia, "operator"),
        _owner(dalton, "owner"),  # no stated share: after every owner that states one
        _owner(georgia, "owner", 8.4),
        _owner(fpl, "owner", None),
        _owner(fpl, "owner", 76.4),  # the same holder from a second source: one name in the header
    ]
    _install(_asset_routes(_asset(owners, operator_name="GEORGIA POWER COMPANY")))
    line = _asset_line(client.get("/assets/scherer-us-ga").text)
    assert 'Operated by</span> <a href="/organizations/georgia-power-co">Georgia Power Company</a>' in line
    assert "Owned by</span>" in line
    assert line.count('href="/organizations/florida-power-light-co"') == 1
    # Largest stated share first, then the owners that state none.
    owned = line[line.index("Owned by</span>") :]
    assert re.findall(r'href="/organizations/([^"]+)"', owned) == [
        "florida-power-light-co",
        "georgia-power-co",
        "dalton-utilities",
    ]
    assert '<a href="/organizations/georgia-power-co">Georgia Power Company</a>, ' in owned


def test_one_holder_reads_owned_and_operated_by(client: TestClient) -> None:
    georgia = _org("org_GP", "georgia-power-co", "Georgia Power Co")
    _install(_asset_routes(_asset([_owner(georgia, "operator"), _owner(georgia, "owner")])))
    line = _asset_line(client.get("/assets/scherer-us-ga").text)
    assert (
        'Owned and operated by</span> <a href="/organizations/georgia-power-co">Georgia Power Co</a>' in line
    )
    assert "Operated by</span>" not in line


def test_more_than_three_owners_link_to_the_table(client: TestClient) -> None:
    owners = [_owner(_org(f"org_{i}", f"co-{i}", f"Co {i}"), "owner") for i in range(5)]
    _install(_asset_routes(_asset(owners)))
    body = client.get("/assets/scherer-us-ga").text
    line = _asset_line(body)
    assert line.count("/organizations/co-") == 3 and 'and <a href="#owners">2 more</a>' in line
    assert 'id="owners"' in body


def test_an_unlinked_operator_is_named_and_a_person_holder_is_nofollow(client: TestClient) -> None:
    _install(_asset_routes(_asset([_owner(PERSON, "owner")], operator_name="U S Bureau of Reclamation")))
    line = _asset_line(client.get("/assets/scherer-us-ga").text)
    assert "Operated by</span> U S Bureau of Reclamation" in line
    assert '<a href="/organizations/jordan-example" rel="nofollow">Jordan Example</a>' in line


def test_a_person_operator_is_nofollow_in_the_header_and_the_fields_alike(client: TestClient) -> None:
    _install(_asset_routes(_asset([_owner(PERSON, "operator"), _owner(SERVICES, "owner", 100.0)])))
    body = client.get("/assets/scherer-us-ga").text
    assert '<a href="/organizations/jordan-example" rel="nofollow">Jordan Example</a>' in _asset_line(body)
    operator_row = body[body.index("<dt>Operator</dt>") :].split("</div>")[0]
    assert 'href="/organizations/jordan-example" rel="nofollow"' in operator_row


def test_an_asset_with_no_holder_has_no_header_line(client: TestClient) -> None:
    _install(_asset_routes(_asset([])))
    assert 'id="record-company"' not in client.get("/assets/scherer-us-ga").text


# ---------------------------------------------------------------------------- a company (U-21)
def _company(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        **_org("org_DUKE", "duke-energy-corp", "DUKE ENERGY CORP"),
        "country": "US",
        "jurisdiction": None,
        "website": None,
        "ids": {},
        "is_curated_issuer": False,
        "provenance": [],
        "parent": None,
        "parent_edge": None,
        "ancestors": [],
        "subsidiaries": [
            _org("org_DEF", "duke-energy-florida-llc", "Duke Energy Florida, LLC"),
            _org("org_DEC", "duke-energy-carolinas-llc", "Duke Energy Carolinas, LLC"),
        ],
        "subsidiary_count": 2,
        "descendant_count": 2,
        "group_scope": {"scope": "all", "organizations": 3, "depth": 1},
    }
    base.update(overrides)
    return base


def _summary_of(org: Mapping[str, Any]) -> dict[str, Any]:
    keys = ("public_id", "slug", "name_canonical", "type", "url", "personal_data")
    return {k: org[k] for k in keys if k in org}


def _holdings(rows: list[tuple[dict[str, Any], int]]) -> dict[str, Any]:
    return {
        "assets": sum(n for _, n in rows),
        "organization_count": len(rows),
        "by_organization": [
            {"organization": _summary_of(org), "assets": n, "by_type": {"power_plant": n}} for org, n in rows
        ],
    }


def _company_routes(
    entity: dict[str, Any], totals: dict[str, Any], opportunities: list[dict[str, Any]]
) -> dict[str, tuple[int, Any]]:
    pid = entity["public_id"]
    return {
        "/v1/organizations": (200, {"data": [entity]}),
        f"/v1/organizations/{pid}": (200, {"data": entity}),
        f"/v1/organizations/{pid}/assets": (
            200,
            {"data": [], "totals": totals, "scope": entity["group_scope"]},
        ),
        f"/v1/organizations/{pid}/proposals": (200, {"data": []}),
        f"/v1/organizations/{pid}/opportunities": (200, {"data": opportunities}),
        f"/v1/organizations/{pid}/nearby-proposals": (200, {"data": [], "totals": {}}),
    }


def _group_rows(body: str) -> list[tuple[str, str, str]]:
    """The Group table as (company, relationship, assets) text triples, in page order."""
    group = body[body.index('id="group"') :].split("</section>")[0]
    cells = (
        r'<tr>\s*<td data-label="Company">(.*?)</td>\s*'
        r'<td data-label="Relationship">(.*?)</td>\s*'
        r'<td data-label="Assets"[^>]*>(.*?)</td>'
    )
    return [(re.sub(r"<[^>]+>", "", n), r, a) for n, r, a in re.findall(cells, group, re.S)]


def _company_page(
    client: TestClient, entity: dict[str, Any], holdings: list[tuple[dict[str, Any], int]], query: str = ""
) -> str:
    totals = _holdings(holdings)
    entity["asset_counts"] = entity["group_asset_counts"] = totals
    _install(_company_routes(entity, totals, []))
    return client.get(f"/organizations/duke-energy-corp{query}").text


def test_the_company_page_merges_portfolio_and_subsidiaries_into_one_group_table(client: TestClient) -> None:
    entity = _company()
    body = _company_page(client, entity, [(entity, 46), (entity["subsidiaries"][0], 3)])
    assert 'aria-label="Portfolio"' not in body and 'aria-label="Subsidiaries"' not in body
    group = body[body.index('id="group"') :].split("</section>")[0]
    assert "Group <span" in group and "3 companies" in group
    assert _group_rows(body) == [
        ("Duke Energy Corp", "This company", "46"),
        ("Duke Energy Florida, LLC", "Subsidiary", "3"),
        # The whole descent is read and every holder is listed: it, and anything below it, holds none.
        ("Duke Energy Carolinas, LLC", "Subsidiary", "0"),
    ]
    # No "Other" type badge on any row.
    assert "reuse-badge" not in group and "Other" not in group
    assert 'id="group-uncounted"' not in body


def test_a_holding_parent_with_one_holding_subsidiary_states_its_holding(client: TestClient) -> None:
    """The holding-company case: a parent that holds nothing itself and one subsidiary holding
    everything. The one-row portfolio is that subsidiary's, and the table says 5, never 0."""
    entity = _company()
    body = _company_page(client, entity, [(entity["subsidiaries"][0], 5)])
    assert _group_rows(body) == [
        ("Duke Energy Florida, LLC", "Subsidiary", "5"),
        ("Duke Energy Carolinas, LLC", "Subsidiary", "0"),
    ]


def test_a_single_holder_further_down_is_listed_and_no_branch_is_called_empty(client: TestClient) -> None:
    """One holder, a grandchild: it is on the table, and each direct subsidiary holding nothing
    itself may be its parent, so its count is a dash with the reason in words, not "0"."""
    grandchild = _org("org_DEFP", "duke-energy-florida-project-llc", "Duke Energy Florida Project LLC")
    entity = _company(descendant_count=3, group_scope={"scope": "all", "organizations": 4, "depth": 2})
    body = _company_page(client, entity, [(grandchild, 5)])
    assert _group_rows(body) == [
        ("Duke Energy Florida Project LLC", "Further down the group", "5"),
        ("Duke Energy Florida, LLC", "Subsidiary", "—"),
        ("Duke Energy Carolinas, LLC", "Subsidiary", "—"),
    ]
    assert 'id="group-uncounted"' in body


def test_a_cut_short_group_never_says_a_subsidiary_holds_none(client: TestClient) -> None:
    scope = {"scope": "all", "organizations": 3, "depth": 1, "truncated": True, "max_organizations": 3}
    entity = _company(group_scope=scope)
    body = _company_page(client, entity, [(entity, 46), (entity["subsidiaries"][0], 3)])
    assert ("Duke Energy Carolinas, LLC", "Subsidiary", "—") in _group_rows(body)


def test_one_level_down_a_subsidiary_holding_nothing_itself_is_not_called_empty(client: TestClient) -> None:
    """`?scope=children` reads the direct subsidiaries' own holdings only; what sits below them is
    out of scope. Every holder there is a direct subsidiary."""
    entity = _company()
    body = _company_page(client, entity, [(entity["subsidiaries"][0], 5)], query="?scope=children")
    assert _group_rows(body) == [
        ("Duke Energy Florida, LLC", "Subsidiary", "5"),
        ("Duke Energy Carolinas, LLC", "Subsidiary", "—"),
    ]


def test_a_lone_company_has_no_group_table(client: TestClient) -> None:
    entity = _company(subsidiaries=[], subsidiary_count=0, descendant_count=0)
    body = _company_page(client, entity, [(entity, 12)])
    assert 'id="group"' not in body


def test_a_company_of_type_other_has_no_other_badge_in_its_header(client: TestClient) -> None:
    entity = _company(subsidiaries=[], subsidiary_count=0, descendant_count=0)
    totals = _holdings([])
    entity["asset_counts"] = entity["group_asset_counts"] = totals
    _install(_company_routes(entity, totals, []))
    body = client.get("/organizations/duke-energy-corp").text
    header = body.split('<div class="detail-header">')[1].split("</div>")[0]
    assert "<h1>Duke Energy Corp</h1>" in header  # U-22
    assert "reuse-badge" not in header and "Other" not in header
    assert "<dt>Type</dt><dd>Other</dd>" in body  # the stored type stays in the fields table


def test_a_company_that_issues_no_rfp_shows_no_rfp_section(client: TestClient) -> None:
    entity = _company(subsidiaries=[], subsidiary_count=0, descendant_count=0)
    totals = _holdings([(entity, 2)])
    entity["asset_counts"] = entity["group_asset_counts"] = totals
    _install(_company_routes(entity, totals, []))
    body = client.get("/organizations/duke-energy-corp").text
    assert 'aria-label="Opportunities"' not in body
    assert "No opportunities issued by this company." not in body


def test_a_company_that_issues_rfps_lists_them_before_its_sources(client: TestClient) -> None:
    entity = _company(subsidiaries=[], subsidiary_count=0, descendant_count=0)
    totals = _holdings([(entity, 2)])
    entity["asset_counts"] = entity["group_asset_counts"] = totals
    opportunity = {
        "public_id": "opp_1",
        "slug": "duke-rfp",
        "title": "Duke RFP",
        "status": "open",
        "provenance": [PROV],
    }
    _install(_company_routes(entity, totals, [opportunity]))
    body = client.get("/organizations/duke-energy-corp").text
    assert 'aria-label="Opportunities"' in body and "/opportunities/duke-rfp" in body
    assert '<span class="section-num">5.</span> Sources' in body
