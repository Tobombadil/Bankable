"""The posture sentence on `/about#tiers` and `/methodology` is the API's, never the template's
(docs/26): it renders from `GET /v1/health`'s `posture_statement`, changes with the value, and is
absent — not guessed — when the API does not report one."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from web.api_client import ApiClient
from web.app import app as web_app

HEALTH_BASE: dict[str, Any] = {
    "status": "ok",
    "api_version": "v1",
    "data_as_of": "2026-09-25T00:00:00Z",
    "live_as_of": "2026-09-25T00:00:00Z",
    "lag_days_default": {"supply": 0, "opportunities": 0},
    "checks": {"database": True, "billing_configured": False},
    "generated_at": "2026-09-25T00:00:00Z",
    "build": {"commit": None, "commit_source": "unavailable", "dirty": None},
    "source_data_as_of": "2026-09-13T00:00:00Z",
    "source_vintage": {"oldest": None, "oldest_label": None, "oldest_source_id": None},
}
NC_SENTENCE = (
    "This platform operates under a noncommercial posture: sources that permit noncommercial "
    "reuse are published with attribution; they will be withdrawn if the posture changes."
)
#: The coverage and vocabulary payloads `web/test_coverage_pages.py` renders `/methodology` from,
#: duplicated rather than imported (repo convention: no `web/test_*.py` imports another one).
COVERAGE: dict[str, Any] = {
    "data": {
        "sources": {
            "registered": 86,
            "with_rows": 16,
            "loaded_source_ids": ["us.eia.860m"],
            "withheld": [
                {
                    "source_id": "us.iso.pjm.gen_queue",
                    "name": "PJM New Services Queue",
                    "operator": "PJM Interconnection",
                    "jurisdiction": "US-PJM",
                    "category": "generation_queue",
                    "reuse": "restricted",
                    "publication": "none",
                    "url": "https://example.org/pjm",
                    "supply": True,
                    "reason": "terms require a licence or consent we do not hold",
                },
                {
                    "source_id": "us.iso.miso.gen_queue",
                    "name": "MISO Generator Interconnection Queue",
                    "operator": "MISO",
                    "jurisdiction": "US-MISO",
                    "category": "generation_queue",
                    "reuse": "unknown",
                    "publication": "none",
                    "url": "https://example.org/miso",
                    "supply": True,
                    "reason": "terms not retrievable, so nothing is assumed",
                },
            ],
        },
        "vintage": {
            "sources": [
                {
                    "source_id": "us.eia.860m",
                    "name": "EIA-860M",
                    "vintage": "2026-07",
                    "vintage_label": "July 2026",
                    "vintage_basis": "artefact_filename",
                    "fetched_at": "2026-09-13T20:25:39Z",
                },
                {
                    "source_id": "us.iso.caiso.gen_queue",
                    "name": "CAISO Public Queue Report",
                    "vintage": None,
                    "vintage_label": None,
                    "vintage_basis": "not_stated",
                    "fetched_at": "2026-09-13T20:25:21Z",
                },
            ],
            "oldest": {
                "source_id": "us.eia.860m",
                "name": "EIA-860M",
                "vintage": "2026-07",
                "vintage_label": "July 2026",
            },
            "sources_stating_a_release": 1,
            "sources_stating_none": 1,
            "sources_undetermined": 0,
        },
        "records": {
            "proposals": 10409,
            "opportunities": 707,
            "assets": 18055,
            "proposal_kinds": {"generation": 6692},
            "opportunity_kinds": {"tender": 553},
            "jurisdictions": {"US-TX": 2333},
        },
        "technologies": {"present": {"solar": 2824}, "absent": ["load"], "vocabulary": ["solar", "load"]},
        "states": {
            "lifecycle_counts": {"filed": 1879, "studied": 1604},
            "lifecycle_absent": ["unknown"],
            "opportunity_status_counts": {"open": 405},
            "opportunity_status_absent": ["frozen", "reinstated"],
            "pre_construction_states": ["filed", "studied", "permitted", "contracted"],
            "pre_construction_count": 4111,
        },
        "ownership": {
            "organizations": 7412,
            "with_recorded_parent": 298,
            "without_recorded_parent": 7114,
            "parent_source_ids": ["global.gleif.lei"],
        },
        "notes": [
            {
                "id": "no_large_load",
                "headline": "No large-load or data-centre proposals.",
                "body": "No loaded source publishes large-load interconnection requests as data.",
                "written": "2026-09-20",
            }
        ],
    }
}
VOCABULARY: dict[str, Any] = {
    "data": {
        "definitions_version": 1,
        "definitions_written": "2026-09-21",
        "lifecycle_states": [
            {
                "state": "unknown",
                "definition": "The source publishes a status we could not place.",
                "excludes": "Not early stage.",
                "uncertainty": None,
                "is_fallback": True,
                "maps_from": [],
            },
            {
                "state": "studied",
                "definition": "Inside the operator's process.",
                "excludes": "Says nothing about the outcome.",
                "uncertainty": "The broadest state.",
                "is_fallback": False,
                "maps_from": [
                    {
                        "source_id": "us.iso.caiso.gen_queue",
                        "status_key": "caiso",
                        "field": "Status",
                        "raw": "ACTIVE",
                        "rule": "map",
                        "note": None,
                        "rows_published": True,
                    },
                    {
                        "source_id": None,
                        "status_key": "spp",
                        "field": "Status (Original)",
                        "raw": "DISIS STAGE",
                        "rule": "map",
                        "note": None,
                        "rows_published": False,
                    },
                ],
            },
        ],
        "opportunity_statuses": [
            {
                "state": "frozen",
                "definition": "The issuer has paused the process.",
                "excludes": None,
                "uncertainty": None,
                "is_fallback": False,
                "maps_from": [],
            }
        ],
        "pre_construction_states": ["filed", "studied", "permitted", "contracted"],
        "status_map_files": ["pipeline/status_map.yaml"],
    }
}


class FakeTransport:
    """Canned responses keyed by path. Duplicated rather than imported: this repo's convention is
    that no `web/test_*.py` imports another one."""

    def __init__(self, responses: Mapping[str, tuple[int, Any]]) -> None:
        self.responses = dict(responses)

    def _respond(self, url: str) -> httpx.Response:
        if url in self.responses:
            status, body = self.responses[url]
            return httpx.Response(status, json=body)
        return httpx.Response(404, json={"title": "not_found", "detail": url})

    def get(
        self, url: str, *, params: Mapping[str, Any] | None = None, cookies: dict[str, str] | None = None
    ) -> httpx.Response:
        return self._respond(url)

    def post(
        self, url: str, *, json: Mapping[str, Any] | None = None, cookies: dict[str, str] | None = None
    ) -> httpx.Response:
        return self._respond(url)


@pytest.fixture()
def web_client() -> Iterator[TestClient]:
    with TestClient(web_app) as client:
        yield client
    web_app.state.build_info = None
    web_app.state.coverage_facts = None


def _install(health: dict[str, Any]) -> None:
    web_app.state.api_client = ApiClient(
        FakeTransport(
            {
                "/v1/health": (200, health),
                "/v1/sources": (200, {"data": [], "meta": {}}),
                "/v1/coverage": (200, COVERAGE),
                "/v1/lifecycle-states": (200, VOCABULARY),
            }
        )
    )
    web_app.state.lag_days_default = None
    web_app.state.build_info = None
    web_app.state.coverage_facts = None


@pytest.mark.parametrize("path", ["/about", "/methodology"])
def test_the_noncommercial_sentence_renders_from_health(web_client: TestClient, path: str) -> None:
    _install({**HEALTH_BASE, "posture": "noncommercial", "posture_statement": NC_SENTENCE})
    body = web_client.get(path).text
    assert 'data-posture="noncommercial"' in body
    assert NC_SENTENCE in body
    if path == "/about":
        tiers = body.split('id="tiers"')[1].split("</section>")[0]
        assert NC_SENTENCE in tiers


@pytest.mark.parametrize("path", ["/about", "/methodology"])
def test_the_commercial_sentence_is_whatever_the_api_says(web_client: TestClient, path: str) -> None:
    sentence = "This platform operates under a commercial posture: whatever the API says."
    _install({**HEALTH_BASE, "posture": "commercial", "posture_statement": sentence})
    body = web_client.get(path).text
    assert 'data-posture="commercial"' in body
    assert sentence in body
    assert "noncommercial posture" not in body


@pytest.mark.parametrize("path", ["/about", "/methodology"])
def test_no_sentence_when_the_api_reports_no_posture(web_client: TestClient, path: str) -> None:
    """Nothing about the posture is typed into a template: an API without the field yields no
    claim at all, rather than a default sentence the gate may not be applying."""
    _install(dict(HEALTH_BASE))
    body = web_client.get(path).text
    assert "platform-posture" not in body
    assert "operates under a" not in body
