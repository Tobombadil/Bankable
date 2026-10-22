"""Curated-issuer RFPs from connector run to public API, under each platform posture (docs/26, docs/28).

The path the scheduler takes, end to end: `pipeline.connectors.runner.run` on a recorded bundle
(`pipeline/connectors/us_rfp/fixtures`), `services.ingest.loader.load_from_files` on the frame that
run wrote, then `GET /v1/opportunities` and the record's Sources panel. Each of the three gates
(connector registry, loader, API predicate) reads its class set once at import from
`PLATFORM_POSTURE`; the posture is set here by patching those module-level sets, as
`tests/test_platform_posture.py` does (that file proves the setting produces them).

What must hold (docs/28 §3.3, §8.1):

- AEP's four rows are `noncommercial`. Under the deployed `noncommercial` posture they are
  published as derived fields with AEP's credit line; flipped to `commercial` they disappear from
  every non-admin read with no other change, while the state rows (CT DEEP `attribution`, NJ BPU
  `open`) stay.
- Under `commercial` the run is quarantined and the loader refuses it, so nothing reaches the table.
"""

from __future__ import annotations

import datetime as dt
import pathlib
from typing import Any

import pytest
from sqlalchemy import select

import pipeline.connectors.registry as registry_module
import services.api.visibility as visibility
import services.ingest.loader as loader
from pipeline.connectors.base import RawSnapshot
from pipeline.connectors.registry import Registry
from pipeline.connectors.runner import run
from pipeline.connectors.store import Store
from services import posture
from services.db.models import Opportunity

FIXTURES = pathlib.Path(__file__).resolve().parents[1] / "pipeline" / "connectors" / "us_rfp" / "fixtures"
AT = dt.datetime(2026, 10, 21, 16, 0, tzinfo=dt.UTC)
PSO_CREDIT = "Source: Public Service Company of Oklahoma (AEP), Energy RFPs"
CT_CREDIT = "Source: Connecticut Department of Energy and Environmental Protection"


def _set_posture(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    publishable = frozenset(posture.publishable_reuse_classes(value))
    gated = frozenset(posture.gated_reuse_classes(value))
    for module in (registry_module, loader):
        monkeypatch.setattr(module, "PUBLISHABLE_REUSE", publishable)
        monkeypatch.setattr(module, "GATED_REUSE", gated)
    monkeypatch.setattr(visibility, "PUBLISHABLE_REUSE_CLASSES", posture.publishable_reuse_classes(value))


def _raw(source_id: str) -> RawSnapshot:
    slug = source_id.removeprefix("us.rfp.")
    return RawSnapshot(
        content=(FIXTURES / f"bundle_{slug}_20261021.json").read_bytes(),
        content_type="application/json",
        url=Registry().get(source_id).url,
        retrieved_at=AT,
        http_status=200,
        ext="json",
    )


def _run_and_load(db: Any, root: pathlib.Path, source_id: str) -> None:
    reg = Registry()
    result = run(source_id, registry=reg, store=Store(root), raw=_raw(source_id))
    assert result.status == "ok" and result.run["publishable"] is True, result.run
    ts = pathlib.Path(result.paths["normalized"]).stem
    loader.load_from_files(db, source_id, ts, data_root=root, registry=reg)
    db.commit()


def _listed(client: Any) -> dict[str, dict[str, Any]]:
    body = client.get("/v1/opportunities?kind=rfp&status=closed,unknown,announced,open").json()
    return {row["title"]: row for row in body["data"]}


def _credits(client: Any) -> set[str]:
    body = client.get("/v1/opportunities?kind=rfp&status=closed,unknown,announced,open").json()
    return {s.get("attribution_text") for s in (body.get("licence_summary") or {}).get("sources") or []}


def test_aep_rfps_publish_under_noncommercial_and_vanish_under_commercial(
    client: Any, db: Any, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_posture(monkeypatch, "noncommercial")
    _run_and_load(db, tmp_path, "us.rfp.pso")
    _run_and_load(db, tmp_path, "us.rfp.ct_deep")
    assert {o.min_reuse_class for o in db.scalars(select(Opportunity))} == {"noncommercial", "attribution"}

    listed = _listed(client)
    assert {"2026 All-Source RFP", "2025 SPP Capacity RFP"} <= set(listed)
    assert "DEEP Announces 2026 Zero Carbon Request for Proposals" in listed
    allsource = listed["2026 All-Source RFP"]
    assert allsource["kind"] == "rfp" and allsource["capacity_sought_mw"] == 4000.0
    assert allsource["status"] == "closed"
    assert {PSO_CREDIT, CT_CREDIT} <= _credits(client)

    # The Sources panel (US-302 AC1): the issuer page, its retrieval time and the licence credit.
    sources = client.get(f"/v1/opportunities/{allsource['public_id']}/sources").json()["data"]
    assert len(sources) == 1
    provenance = sources[0]
    assert provenance["source_id"] == "us.rfp.pso"
    assert provenance["source_url"] == "https://www.psoklahoma.com/business/b2b/energy-rfps/AllSource2026"
    assert provenance["retrieved_at"].startswith("2026-10-21")
    assert provenance["attribution_text"] == PSO_CREDIT

    # The flip back: AEP is gone from every read, the state procurement stays.
    _set_posture(monkeypatch, "commercial")
    listed = _listed(client)
    assert "2026 All-Source RFP" not in listed and "2025 SPP Capacity RFP" not in listed
    assert "DEEP Announces 2026 Zero Carbon Request for Proposals" in listed
    assert PSO_CREDIT not in _credits(client)
    assert client.get(f"/v1/opportunities/{allsource['public_id']}").status_code == 404


def test_under_commercial_an_aep_run_is_quarantined_and_never_loaded(
    db: Any, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_posture(monkeypatch, "commercial")
    reg = Registry()
    result = run(
        "us.rfp.swepco", registry=reg, store=Store(tmp_path), allow_restricted=True, raw=_raw("us.rfp.swepco")
    )
    assert result.status == "ok" and result.run["publishable"] is False
    assert not (tmp_path / "normalized" / "us.rfp.swepco").exists()
    with pytest.raises(loader.GateRefused, match="noncommercial"):
        loader.load_from_files(db, "us.rfp.swepco", "20261021T160000Z", data_root=tmp_path, registry=reg)
    assert db.scalars(select(Opportunity)).first() is None
