"""The nightly M-11 visibility audit (`services/visibility_audit/run.py`; docs/10 §5 M-11; docs/04
R-4, S-9) and its admin read side (`services/api/admin_audit_routes.py`).

Seeding follows E-9 (docs/04): one record per `publish_state` that matters to the public surface
(`public`, `pending_review`, `unpublished` = taken down, `ingest_only`), per reuse class that
matters (`open`, `restricted`), a PJM row and a MISO row keyed to the real `data/sources.yaml`
ids, and a mixed-provenance proposal. Entity factories are the plain functions in
`services/api/conftest.py`; the DB and TestClient fixtures are `tests/conftest.py`'s.

What each breach test proves, and why it is built the way it is:

- **Public record on a gated source.** The predicate trusts `proposal.min_reuse_class` and the
  source's `publish_state`; a source whose *licence* was reclassified to `restricted` after load
  while it stayed `public` passes both. The audit must see it on the store pass and confirm the
  real app serves it (served pass, `served_leak`).
- **PJM / MISO row.** The store says the source is `public` and `open` (the drift an operator's
  mistaken flip produces); the register says `publication: none`. The register wins.
- **Taken-down record.** The correct predicate hides it, so the only ways it can breach are a
  predicate regression (store pass, stood in through `run.PREDICATES`) or a serving path that
  bypasses the predicate (served pass, stood in by patching the route module's import). Both are
  tested; and the clean store below contains taken-down rows and must still read `m11 = 0`.
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib
from collections.abc import Iterator
from typing import Any

import pytest
import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

import services.api.records as records_module
from services.api.conftest import (
    make_asset,
    make_event,
    make_open_licence,
    make_org,
    make_public_source,
    make_visible_opportunity,
    make_visible_proposal,
)
from services.db.models import Event, Licence, Proposal, ProposalSource, Source
from services.db.session import get_engine, get_sessionmaker, init_db
from services.visibility_audit import run
from tests.conftest import login, make_account, make_user
from tests.test_api_contract import assert_valid

UTC = dt.UTC
REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
PJM = "us.iso.pjm.gen_queue"
MISO = "us.iso.miso.gen_queue"
ERCOT = "us.iso.ercot.gen_queue"


@pytest.fixture(scope="module")
def spec() -> dict[str, Any]:
    with (REPO_ROOT / "api" / "openapi.yaml").open(encoding="utf-8") as fh:
        loaded: dict[str, Any] = yaml.safe_load(fh)
    return loaded


@pytest.fixture(autouse=True)
def _commercial_posture(monkeypatch: pytest.MonkeyPatch) -> None:
    """The predicate read the posture at import; the audit reads it per run. Pin both views to
    the default so a developer's shell `PLATFORM_POSTURE` cannot make the two disagree here."""
    monkeypatch.delenv("PLATFORM_POSTURE", raising=False)


# ------------------------------------------------------------------------------------ factories
def _restricted_licence(db: Session, id_: str = "restricted-lic") -> Licence:
    lic = Licence(
        id=id_,
        name="Restricted Licence",
        reuse_class="restricted",
        allows_derived_publication=False,
        allows_raw_publication=False,
        gate_flag=True,
        evidence_url="https://example.org/restricted-terms",
        evidence_retrieved_at=dt.datetime(2026, 9, 1, tzinfo=UTC),
        classified_by="legal-compliance",
    )
    db.add(lic)
    db.flush()
    return lic


def _source(db: Session, licence: Licence, id_: str, *, publish_state: str = "public") -> Source:
    src = make_public_source(db, licence, id_=id_)
    src.publish_state = publish_state
    db.flush()
    return src


def _link(db: Session, proposal: Proposal, source: Source, *, suffix: str, active: bool = True) -> None:
    now = dt.datetime.now(UTC)
    db.add(
        ProposalSource(
            proposal_id=proposal.id,
            source_id=source.id,
            source_record_id=f"X{suffix}",
            source_url=f"{source.url}#{suffix}",
            retrieved_at=now - dt.timedelta(days=1),
            licence_id=source.licence_id,
            raw={"Queue ID": f"X{suffix}"},
            first_seen=now - dt.timedelta(days=30),
            last_seen=now - dt.timedelta(days=1),
            active=active,
        )
    )
    db.flush()


def _hidden_proposal(db: Session, source: Source, *, suffix: str, publish_state: str) -> Proposal:
    """A record the public surface must not show: taken down (`unpublished`, `public_at` left
    set exactly as `PUT /admin/v1/.../publish-state` leaves it), pending review, or ingest-only."""
    prop = make_visible_proposal(db, source, public_id_suffix=suffix)
    prop.publish_state = publish_state
    db.flush()
    return prop


def seed_clean_store(db: Session) -> dict[str, Any]:
    """Everything the public surface should show, next to everything it must not, with no
    breach: the E-9 shape. The PJM source is in the store the way the loader leaves it —
    `ingest_only`, `restricted` — with its own proposal `ingest_only` and an inactive link on the
    mixed-provenance proposal."""
    open_lic = make_open_licence(db)
    restricted = _restricted_licence(db)
    ercot = _source(db, open_lic, ERCOT)
    pjm = _source(db, restricted, PJM, publish_state="ingest_only")
    sponsor = make_org(db, "Clean Sponsor LLC")
    shown = make_visible_proposal(db, ercot, public_id_suffix="1", sponsor=sponsor)
    make_event(db, shown, ercot)
    make_visible_opportunity(db, ercot, public_id_suffix="1")
    make_asset(db, ercot, open_lic)
    mixed = make_visible_proposal(db, ercot, public_id_suffix="2")
    _link(db, mixed, pjm, suffix="pjm-mixed", active=False)
    taken_down = _hidden_proposal(db, ercot, suffix="3", publish_state="unpublished")
    pending = _hidden_proposal(db, ercot, suffix="4", publish_state="pending_review")
    pjm_only = make_visible_proposal(db, pjm, public_id_suffix="5")
    pjm_only.publish_state = "ingest_only"
    db.flush()
    return {
        "open": open_lic,
        "restricted": restricted,
        "ercot": ercot,
        "pjm": pjm,
        "shown": shown,
        "taken_down": taken_down,
        "pending": pending,
        "pjm_only": pjm_only,
    }


def _breaches(result: dict[str, Any], surface: str | None = None) -> list[dict[str, Any]]:
    return [b for b in result["breaches"] if surface is None or b["surface"] == surface]


# ================================================================================ clean store
def test_a_clean_store_yields_m11_zero_and_probes_the_rows_that_must_stay_hidden(
    db: Session, db_sessionmaker: sessionmaker[Session]
) -> None:
    seeded = seed_clean_store(db)
    db.commit()

    result = run.run_audit(db_sessionmaker)

    assert result["m11"] == 0, result["breaches"]
    assert result["breaches"] == []
    assert result["posture"] == "commercial"
    assert result["posture_source"] == "platform"
    counts = result["counts"]
    assert counts["proposals"] == {"shown": 2, "breaches": 0}
    assert counts["opportunities"] == {"shown": 1, "breaches": 0}
    assert counts["events"] == {"shown": 1, "breaches": 0}
    assert counts["assets"] == {"shown": 1, "breaches": 0}
    assert counts["organizations"]["breaches"] == 0
    # The shown proposals' active links (2) + the opportunity's (1); the inactive PJM link is not shown.
    assert counts["source_links"] == {"shown": 3, "breaches": 0}
    # The store's PJM source is gated three ways at once (class, state, register).
    assert set(result["gated_sources"]) == {PJM}
    assert "register:publication=none" in result["gated_sources"][PJM]

    # The served pass asked the real app for every must-be-hidden row and got nothing back.
    served = result["served"]
    probed = {c["public_id"] for c in served["checks"]}
    assert {
        seeded["taken_down"].public_id,
        seeded["pending"].public_id,
        seeded["pjm_only"].public_id,
    } <= probed
    assert served["leaks"] == 0
    assert served["inconclusive"] == 0
    assert all(c["status"] == 404 and c["kind"] == "must_be_hidden" for c in served["checks"])

    # Persisted as one system-actored, never-public event row.
    events = db.scalars(select(Event).where(Event.event_type == run.EVENT_TYPE)).all()
    assert len(events) == 1
    assert events[0].actor_type == "system"
    assert events[0].public_at is None and events[0].published_at is None
    assert events[0].after is not None and events[0].after["m11"] == 0
    assert "_breaches" not in events[0].after
    assert result["event_id"].startswith("evt_")


def test_the_audit_event_never_reaches_the_public_events_feed(
    client: Any, db: Session, db_sessionmaker: sessionmaker[Session]
) -> None:
    seed_clean_store(db)
    db.commit()
    run.run_audit(db_sessionmaker)

    body = client.get("/v1/events").json()
    assert all(e.get("event_type") != run.EVENT_TYPE for e in body["data"])


# ============================================================================= breach: gated source
def test_a_public_record_whose_only_source_is_gated_is_found_and_the_app_is_shown_to_serve_it(
    db: Session, db_sessionmaker: sessionmaker[Session]
) -> None:
    """The licence was reclassified to `restricted` after load; the source and the record were
    left `public` and the record's `min_reuse_class` stale. The predicate shows it."""
    seed_clean_store(db)
    reclassified = _restricted_licence(db, "reclassified-lic")
    gated = _source(db, reclassified, "us.test.reclassified")
    record = make_visible_proposal(db, gated, public_id_suffix="10")
    record.min_reuse_class = "open"
    db.commit()

    result = run.run_audit(db_sessionmaker, persist=False)

    found = _breaches(result, "proposals")
    assert len(found) == 1
    breach = found[0]
    assert breach["public_id"] == record.public_id
    assert breach["source_id"] == "us.test.reclassified"
    assert breach["reason"] == "only_gated_evidence:source_class_gated:restricted"
    assert breach["served_status"] == 200
    assert breach["served_leak"] is True
    assert result["m11"] == 1
    assert result["counts"]["proposals"]["breaches"] == 1
    assert "event_id" not in result


def test_an_asset_and_an_event_resting_on_a_gated_source_are_found(
    db: Session, db_sessionmaker: sessionmaker[Session]
) -> None:
    seeded = seed_clean_store(db)
    reclassified = _restricted_licence(db, "reclassified-lic")
    gated = _source(db, reclassified, "us.test.reclassified")
    # Both rows keep the open licence they were loaded under; only their source moved.
    asset = make_asset(db, gated, seeded["open"], source_asset_id="77", name="Drifted Plant")
    event = make_event(db, seeded["shown"], gated, event_type="capacity_change")
    event.licence_id = seeded["open"].id
    db.commit()

    result = run.run_audit(db_sessionmaker, persist=False)

    reasons = {(b["surface"], b["reason"]) for b in result["breaches"]}
    assert ("assets", "asset_source_gated:source_class_gated:restricted") in reasons
    assert ("events", "event_source_gated:source_class_gated:restricted") in reasons
    assert any(b["public_id"] == asset.public_id for b in _breaches(result, "assets"))
    assert result["counts"]["assets"]["breaches"] == 1
    assert result["counts"]["events"]["breaches"] == 1
    assert result["m11"] == 2


# =================================================================================== breach: PJM/MISO
@pytest.mark.parametrize("source_id", [PJM, MISO])
def test_a_pjm_or_miso_row_on_the_public_surface_is_found_whatever_the_store_says(
    source_id: str, db: Session, db_sessionmaker: sessionmaker[Session]
) -> None:
    """The store row was flipped `public` under an `open` licence; `data/sources.yaml` says
    `publication: none`. The record, its sponsor organisation, and the gated link on an otherwise
    clean mixed-provenance record are each a breach."""
    open_lic = make_open_licence(db)
    ercot = _source(db, open_lic, ERCOT)
    flipped = _source(db, open_lic, source_id)
    sponsor = make_org(db, "Register Only Sponsor LLC")
    iso_only = make_visible_proposal(db, flipped, public_id_suffix="20", sponsor=sponsor)
    mixed = make_visible_proposal(db, ercot, public_id_suffix="21")
    _link(db, mixed, flipped, suffix="mixed")
    db.commit()

    result = run.run_audit(db_sessionmaker, persist=False)

    by_surface = {b["surface"]: b for b in result["breaches"]}
    assert by_surface["proposals"]["public_id"] == iso_only.public_id
    assert by_surface["proposals"]["source_id"] == source_id
    assert by_surface["proposals"]["reason"] == "only_gated_evidence:register:publication=none"
    assert by_surface["proposals"]["served_leak"] is True
    # docs/21 §8 item 3: the gated link row on a rightly-public record is a leak of its own, and
    # the served pass confirms the record's sources endpoint names the source.
    assert by_surface["source_links"]["public_id"] == mixed.public_id
    assert by_surface["source_links"]["reason"] == "source_link_gated:register:publication=none"
    assert by_surface["source_links"]["served_status"] == 200
    assert by_surface["source_links"]["served_leak"] is True
    assert by_surface["organizations"]["public_id"] == sponsor.public_id
    assert (
        by_surface["organizations"]["reason"] == "organization_gated_evidence_only:register:publication=none"
    )
    assert result["m11"] == 3
    assert result["served"]["leaks"] >= 2


def test_the_register_is_read_under_the_posture_in_force() -> None:
    commercial = run.register_gated_sources("commercial")
    noncommercial = run.register_gated_sources("noncommercial")
    assert commercial[PJM] == "register:publication=none"
    assert commercial[MISO] == "register:publication=none"
    assert set(noncommercial) <= set(commercial)


def test_an_unreadable_register_is_reported_empty_not_raised(tmp_path: pathlib.Path) -> None:
    missing = tmp_path / "nope.yaml"
    assert run.register_gated_sources("commercial", missing) == {}
    reuse_only = tmp_path / "sources.yaml"
    reuse_only.write_text(
        "sources:\n  - id: a.gated\n    reuse: noncommercial\n  - id: a.open\n    reuse: open\n  - junk\n",
        encoding="utf-8",
    )
    assert run.register_gated_sources("commercial", reuse_only) == {"a.gated": "register:reuse=noncommercial"}
    assert run.register_gated_sources("noncommercial", reuse_only) == {}


# ============================================================================= breach: taken down
def _regressed_proposal_predicate(entitlement: str = "public", now: dt.datetime | None = None) -> list[Any]:
    """The predicate with its record-state clause lost — the regression the invariant checks
    exist to catch."""
    now = now or dt.datetime.now(UTC)
    return [
        Proposal.public_at.is_not(None),
        Proposal.public_at <= now,
        Proposal.min_reuse_class.in_(("open", "attribution")),
    ]


def test_a_taken_down_record_is_found_when_the_predicate_regresses(
    db: Session, db_sessionmaker: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    seeded = seed_clean_store(db)
    db.commit()
    monkeypatch.setitem(run.PREDICATES, "proposals", _regressed_proposal_predicate)

    result = run.run_audit(db_sessionmaker, persist=False)

    by_id = {b["public_id"]: b for b in _breaches(result, "proposals")}
    taken_down = by_id[seeded["taken_down"].public_id]
    assert taken_down["reason"] == "record_not_public:unpublished"
    assert by_id[seeded["pending"].public_id]["reason"] == "record_not_public:pending_review"
    # The real app's predicate is intact, so the served pass disagrees with the store pass: the
    # breach stands (the audit's own predicate view is what regressed) and is annotated 404.
    assert taken_down["served_status"] == 404
    assert taken_down["served_leak"] is False
    assert result["m11"] >= 2


def test_a_taken_down_record_served_by_a_path_that_bypasses_the_predicate_is_found(
    db: Session, db_sessionmaker: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The store pass is clean (the audit's predicate is right); the serving path is not."""
    seeded = seed_clean_store(db)
    db.commit()
    monkeypatch.setattr(records_module, "proposal_visibility_filter", _regressed_proposal_predicate)

    result = run.run_audit(db_sessionmaker, persist=False)

    served_hidden = [b for b in result["breaches"] if b["reason"].startswith("served_hidden:")]
    assert {b["public_id"] for b in served_hidden} == {
        seeded["taken_down"].public_id,
        seeded["pending"].public_id,
    }
    assert {b["reason"] for b in served_hidden} == {
        "served_hidden:publish_state=unpublished",
        "served_hidden:publish_state=pending_review",
    }
    assert all(b["served_status"] == 200 and b["served_leak"] for b in served_hidden)
    assert result["counts"]["proposals"]["breaches"] == 2
    assert result["m11"] == 2


def test_the_served_pass_restores_a_callers_dependency_override(
    client: Any, db: Session, db_sessionmaker: sessionmaker[Session]
) -> None:
    from services.api.app import app
    from services.api.deps import get_db

    before = app.dependency_overrides[get_db]
    seed_clean_store(db)
    db.commit()
    run.run_audit(db_sessionmaker, persist=False)
    assert app.dependency_overrides[get_db] is before


def test_a_starved_served_pass_is_counted_inconclusive_not_clean(
    db: Session, db_sessionmaker: sessionmaker[Session]
) -> None:
    from services.api.ratelimit import TIER_LIMITS, default_limiter

    seed_clean_store(db)
    db.commit()
    for _ in range(TIER_LIMITS["public"]):
        default_limiter.check("public:testclient", limit=TIER_LIMITS["public"])

    result = run.run_audit(db_sessionmaker, persist=False)

    assert result["served"]["checked"] > 0
    assert result["served"]["inconclusive"] == result["served"]["checked"]
    assert result["served"]["leaks"] == 0


# ================================================================================ caps and helpers
def test_the_breach_list_is_capped_but_the_metric_is_not(
    db: Session, db_sessionmaker: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_clean_store(db)
    db.commit()
    monkeypatch.setitem(run.PREDICATES, "proposals", _regressed_proposal_predicate)
    monkeypatch.setattr(run, "BREACH_CAP", 1)

    result = run.run_audit(db_sessionmaker, persist=False, served_sample=0)

    assert len(result["breaches"]) == 1
    assert result["breach_total"] == result["m11"] >= 2
    assert result["breaches_truncated"] is True
    assert result["served"]["checked"] == 0


def test_posture_resolution_and_the_override_flag(
    db: Session, db_sessionmaker: sessionmaker[Session]
) -> None:
    assert run.resolve_posture(None) == "commercial"
    assert run.resolve_posture(" Auto ") == "commercial"
    assert run.resolve_posture("noncommercial") == "noncommercial"
    assert run.resolve_posture("typo") == "commercial"
    seed_clean_store(db)
    db.commit()
    result = run.run_audit(db_sessionmaker, posture="noncommercial", persist=False)
    assert result["posture_source"] == "override"
    assert "noncommercial" in result["publishable_reuse_classes"]


def test_detail_paths_cover_every_surface() -> None:
    assert run._detail_path("events", "evt_x") == "/v1/events/evt_x"
    assert run._detail_path("organizations", "org_x") == "/v1/organizations/org_x"
    assert run._detail_path("assets", "asset_x") == "/v1/assets/asset_x"
    assert run._detail_path("source_links", "opp_x") == "/v1/opportunities/opp_x/sources"
    assert run._detail_path("source_links", "asset_x") == "/v1/assets/asset_x"
    assert run._detail_path("source_links", "evt_x") is None
    assert run._detail_path("nowhere", "x") is None
    assert run._mentions_source({"a": [{"source_id": "s"}]}, "s") is True
    assert run._mentions_source({"a": [{"source_id": "t"}], "b": 1}, "s") is False


def test_latest_results_returns_newest_first(db: Session, db_sessionmaker: sessionmaker[Session]) -> None:
    seed_clean_store(db)
    db.commit()
    first = run.run_audit(db_sessionmaker)
    second = run.run_audit(db_sessionmaker)
    with db_sessionmaker() as s:
        rows = run.latest_results(s, limit=2)
        assert [r.after["run_id"] for r in rows if r.after] == [second["run_id"], first["run_id"]]


# ============================================================================================ CLI
@pytest.fixture()
def file_store(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[sessionmaker[Session]]:
    url = f"sqlite+pysqlite:///{tmp_path / 'audit.db'}"
    engine = get_engine(url)
    init_db(engine)
    monkeypatch.setenv("DATABASE_URL", url)
    yield get_sessionmaker(engine)
    engine.dispose()


def test_the_cli_prints_a_summary_and_exits_zero_on_a_clean_store(
    file_store: sessionmaker[Session], capsys: pytest.CaptureFixture[str]
) -> None:
    with file_store() as db:
        seed_clean_store(db)
        db.commit()

    code = run.main(["--no-persist"])

    out = json.loads(capsys.readouterr().out)
    assert code == 0
    assert out["m11"] == 0
    assert "breaches" not in out and "gated_sources" not in out
    assert "checks" not in out["served"]


def test_the_cli_exits_non_zero_when_m11_is_positive(
    file_store: sessionmaker[Session], capsys: pytest.CaptureFixture[str]
) -> None:
    with file_store() as db:
        open_lic = make_open_licence(db)
        flipped = _source(db, open_lic, PJM)
        make_visible_proposal(db, flipped, public_id_suffix="30")
        db.commit()

    code = run.main(["--json", "--sample", "5"])

    out = json.loads(capsys.readouterr().out)
    assert code == 1
    assert out["m11"] == 1
    assert out["breaches"][0]["source_id"] == PJM
    assert out["event_id"].startswith("evt_")


# ================================================================================ admin read side
def _admin(client: Any, db: Session, *, role: str = "operator") -> None:
    account = make_account(db, entitlement="admin", name="Ops")
    user = make_user(db, account, email=f"{role}@example.com", role=role)
    db.commit()
    login(client, db, user)


def test_the_admin_audit_routes_require_an_operator(client: Any, db: Session) -> None:
    assert client.get("/admin/v1/visibility-audits").status_code == 401
    assert client.get("/admin/v1/visibility-audits/latest").status_code == 401
    _admin(client, db, role="member")
    assert client.get("/admin/v1/visibility-audits").status_code == 403


def test_latest_is_404_until_a_run_is_persisted(client: Any, db: Session) -> None:
    _admin(client, db)
    assert client.get("/admin/v1/visibility-audits/latest").status_code == 404
    body = client.get("/admin/v1/visibility-audits").json()
    assert body["data"] == []


def test_the_admin_routes_list_runs_newest_first_and_serve_the_latest_in_full(
    client: Any, db: Session, db_sessionmaker: sessionmaker[Session], spec: dict[str, Any]
) -> None:
    open_lic = make_open_licence(db)
    flipped = _source(db, open_lic, PJM)
    breach_record = make_visible_proposal(db, flipped, public_id_suffix="40")
    db.commit()
    first = run.run_audit(db_sessionmaker)
    second = run.run_audit(db_sessionmaker)
    _admin(client, db)

    page_one = client.get("/admin/v1/visibility-audits", params={"limit": 1})
    assert page_one.status_code == 200
    body = page_one.json()
    assert_valid(spec, "VisibilityAuditListResponse", body)
    assert [row["run_id"] for row in body["data"]] == [second["run_id"]]
    assert "breaches" not in body["data"][0]
    assert body["data"][0]["m11"] == 1
    assert body["page"]["has_more"] is True

    page_two = client.get(
        "/admin/v1/visibility-audits", params={"limit": 1, "cursor": body["page"]["next_cursor"]}
    ).json()
    assert [row["run_id"] for row in page_two["data"]] == [first["run_id"]]

    latest = client.get("/admin/v1/visibility-audits/latest")
    assert latest.status_code == 200
    full = latest.json()
    assert_valid(spec, "VisibilityAuditResponse", full)
    assert full["data"]["run_id"] == second["run_id"]
    assert full["data"]["id"] == second["event_id"]
    assert full["data"]["breaches"][0]["public_id"] == breach_record.public_id
    assert full["data"]["breaches"][0]["source_id"] == PJM


def test_the_admin_list_refuses_unknown_parameters(client: Any, db: Session) -> None:
    _admin(client, db)
    assert client.get("/admin/v1/visibility-audits", params={"offset": 5}).status_code == 400
    assert client.get("/admin/v1/visibility-audits/latest", params={"x": 1}).status_code == 400


# ================================================================ withheld operator names (lane E15)
HIDDEN_OPERATOR = "Shadow Midstream Partners"
HIDDEN_OPERATOR_REGISTER = "SHADOW MIDSTREAM PARTNERS, LLC"


@pytest.fixture()
def _fresh_name_caches() -> Iterator[None]:
    import services.api.assets as assets_module
    from services.api import withheld_names

    assets_module._reset_asset_index_cache()
    withheld_names.reset_cache()
    yield
    assets_module._reset_asset_index_cache()
    withheld_names.reset_cache()


def seed_taken_down_operator(db: Session) -> dict[str, Any]:
    """The clean store plus an organisation taken down while three public assets still name it: a
    plant operated through an edge (register spelling differs), a compressor named only by its
    register string, and a pipeline line whose attributes name it. The assets stay public."""
    from services.api.conftest import make_asset_owner

    seeded = seed_clean_store(db)
    lic, src = seeded["open"], seeded["ercot"]
    hidden = make_org(db, HIDDEN_OPERATOR)
    public_owner = make_org(db, "Open Owner Holdings")
    plant = make_asset(db, src, lic, source_asset_id="w1", name="Prairie Gas Plant", geom=(-98.0, 32.0))
    plant.operator_name = HIDDEN_OPERATOR_REGISTER
    plant.attributes = {"operator_raw": HIDDEN_OPERATOR_REGISTER, "city": "Midland"}
    make_asset_owner(db, plant, hidden, src, lic, role="operator", share_pct=None)
    make_asset_owner(db, plant, public_owner, src, lic, role="owner")
    compressor = make_asset(db, src, lic, source_asset_id="w2", name="Mesa Compressor", geom=(-97.0, 31.0))
    compressor.operator_name = HIDDEN_OPERATOR.upper()
    line = make_asset(
        db, src, lic, source_asset_id="w3", name="Long Line", asset_type="gas_pipeline", technology=None
    )
    line.geom = None
    line.geom_line = [(-99.0, 31.0), (-97.0, 33.0)]
    line.operator_name = HIDDEN_OPERATOR
    line.attributes = {"operator": HIDDEN_OPERATOR, "miles": 120.0}
    hidden.publish_state = "unpublished"
    db.flush()
    return {**seeded, "hidden_org": hidden, "plant": plant, "compressor": compressor, "line": line}


def test_a_taken_down_operator_named_by_public_assets_is_not_a_breach_when_every_surface_withholds_it(
    db: Session, db_sessionmaker: sessionmaker[Session], _fresh_name_caches: None, spec: dict[str, Any]
) -> None:
    seeded = seed_taken_down_operator(db)
    db.commit()

    result = run.run_audit(db_sessionmaker)

    assert result["m11"] == 0, result["breaches"]
    name_checks = [c for c in result["served"]["checks"] if c["kind"] == "withheld_name"]
    detail_ids = {c["public_id"] for c in name_checks if c["why"].startswith("detail")}
    assert {seeded["plant"].public_id, seeded["compressor"].public_id, seeded["line"].public_id} <= detail_ids
    assert any(c["why"].startswith("search") for c in name_checks)
    assert all(c["status"] == 200 and not c["leak"] for c in name_checks)
    # The persisted row never carries the withheld spelling the search probe used.
    stored = db.scalars(select(Event).where(Event.event_type == run.EVENT_TYPE)).one()
    assert HIDDEN_OPERATOR.lower() not in json.dumps(stored.after).lower()
    assert not any(k.startswith("_") for k in result)


def test_an_asset_surface_that_forgets_the_withheld_names_is_a_breach_on_both_passes(
    db: Session,
    db_sessionmaker: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    _fresh_name_caches: None,
) -> None:
    """The regression the check exists for: an asset serializer called without the takedown set (a
    new surface, or a refactor that passes `NONE`). The store pass sees the name in the rendered
    shape; the served pass confirms the real detail page prints it."""
    import services.api.assets as assets_module
    from services.api import withheld_names
    from services.api.serialize import serialize_asset

    seeded = seed_taken_down_operator(db)
    db.commit()

    def forgetful(asset: Any, **kwargs: Any) -> dict[str, Any]:
        return serialize_asset(asset, **{**kwargs, "withheld": withheld_names.NONE})

    monkeypatch.setattr(assets_module, "serialize_asset", forgetful)

    result = run.run_audit(db_sessionmaker, persist=False)

    printed = [b for b in _breaches(result, "assets") if b["reason"].startswith("withheld_name_printed:")]
    by_asset = {b["public_id"]: b for b in printed if b["reason"].startswith("withheld_name_printed:detail:")}
    assert set(by_asset) == {
        seeded["plant"].public_id,
        seeded["compressor"].public_id,
        seeded["line"].public_id,
    }
    assert by_asset[seeded["plant"].public_id]["reason"] == "withheld_name_printed:detail:operator_name"
    assert all(b["served_status"] == 200 and b["served_leak"] for b in by_asset.values())
    # The map point feature does not go through the serializer, so it is not a breach here.
    assert not any(":map_point:" in b["reason"] for b in printed)
    assert result["m11"] == len(result["breaches"]) >= 3


def test_an_asset_search_that_matches_a_withheld_operator_name_is_a_breach(
    db: Session,
    db_sessionmaker: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    _fresh_name_caches: None,
) -> None:
    import sqlalchemy as sa

    from services.api.withheld_names import WithheldNames

    seeded = seed_taken_down_operator(db)
    db.commit()
    real = WithheldNames.operator_name_searchable
    calls = {"n": 0}

    def searchable_everywhere(self: WithheldNames) -> Any:
        # The audit's own store query uses the real clause; the served search gets the regression.
        calls["n"] += 1
        return real(self) if calls["n"] == 1 else sa.true()

    monkeypatch.setattr(WithheldNames, "operator_name_searchable", searchable_everywhere)

    result = run.run_audit(db_sessionmaker, persist=False)

    assert calls["n"] >= 2, "the served search never reached the regressed clause"
    searchable = {
        b["public_id"] for b in result["breaches"] if b["reason"] == "withheld_name_searchable:operator_name"
    }
    # Every spelling contains "Shadow Midstream Partners", so each search finds all three by operator
    # name; none of them carries the text in its own name or in a public owner's name.
    assert searchable == {seeded["plant"].public_id, seeded["compressor"].public_id, seeded["line"].public_id}
    assert not any(b["reason"].startswith("withheld_name_printed:") for b in result["breaches"])


def test_withheld_name_paths_reads_owners_by_id_and_an_operator_edge_whatever_the_spelling() -> None:
    from pipeline.normalize import org_key
    from services.api.withheld_names import WithheldNames

    # Keys, not raw strings: the register spelling below differs in case and legal form.
    withheld = WithheldNames(keys=frozenset({org_key(HIDDEN_OPERATOR)}))
    shape = {
        "operator_name": "Unrelated Energy",
        "attributes": {"nested": {"owner_raw": HIDDEN_OPERATOR_REGISTER}, "list": ["x", HIDDEN_OPERATOR]},
        "owners": [
            {"organization": {"public_id": "org_public", "name": HIDDEN_OPERATOR}},
            {"organization": {"public_id": "org_hidden", "name": "Renamed"}},
        ],
        "name": HIDDEN_OPERATOR,  # the asset's own name is not register text about the company
    }
    paths = run.withheld_name_paths(
        shape, withheld, operator_edge=False, hidden_org_ids=frozenset({"org_hidden"})
    )
    assert paths == ["attributes.nested.owner_raw", "attributes.list[1]", "owners[1]"]
    edge = run.withheld_name_paths({"operator_name": "Unrelated Energy"}, withheld, operator_edge=True)
    assert edge == ["operator_name"]
    feature = {"type": "Feature", "properties": {"operator_name": HIDDEN_OPERATOR}}
    assert run.withheld_name_paths(feature, withheld, operator_edge=False) == ["operator_name"]
    assert run.withheld_name_paths({"operator_name": None}, withheld, operator_edge=True) == []


# ======================================================= owner edges' raw spellings (lane E16)
def seed_raw_owner_spelling(db: Session) -> dict[str, Any]:
    """The clean store plus an organisation taken down while a public asset's owner edge -- to a
    *public* organisation -- still carries a raw register spelling of it. Nothing else links the asset
    to the hidden organisation (no operator edge, no operator name), so only the new arm finds it."""
    from services.api.conftest import make_asset_owner

    seeded = seed_clean_store(db)
    lic, src = seeded["open"], seeded["ercot"]
    hidden = make_org(db, HIDDEN_OPERATOR)
    public_owner = make_org(db, "Open Owner Holdings")
    plant = make_asset(db, src, lic, source_asset_id="r1", name="Prairie Gas Plant", geom=(-98.0, 32.0))
    plant.operator_name = "Open Owner Holdings"
    edge = make_asset_owner(db, plant, public_owner, src, lic, role="owner")
    edge.owner_name_raw = HIDDEN_OPERATOR_REGISTER
    hidden.publish_state = "unpublished"
    db.flush()
    return {**seeded, "hidden_org": hidden, "public_owner": public_owner, "plant": plant}


def test_a_raw_owner_spelling_withheld_by_the_serializer_is_probed_and_not_a_breach(
    db: Session, db_sessionmaker: sessionmaker[Session], _fresh_name_caches: None
) -> None:
    seeded = seed_raw_owner_spelling(db)
    db.commit()

    result = run.run_audit(db_sessionmaker, persist=False)

    assert result["m11"] == 0, result["breaches"]
    details = [
        c
        for c in result["served"]["checks"]
        if c["kind"] == "withheld_name" and c["why"].startswith("detail")
    ]
    assert seeded["plant"].public_id in {c["public_id"] for c in details}
    assert all(c["status"] == 200 and not c["leak"] for c in details)


def test_a_serializer_that_prints_a_raw_owner_spelling_is_a_breach_on_both_passes(
    db: Session,
    db_sessionmaker: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    _fresh_name_caches: None,
) -> None:
    """The regression the new arm exists for: the owner-edge serializer printing `owner_name_raw` as
    stored (the behaviour before lane E16). The store pass sees it in the rendered detail shape; the
    served pass confirms the real detail page prints it."""
    from services.api import serialize as serialize_module

    real = serialize_module.serialize_asset_owner
    seeded = seed_raw_owner_spelling(db)
    db.commit()

    def regressed(edge: Any, **kwargs: Any) -> dict[str, Any]:
        out: dict[str, Any] = real(edge, **kwargs)
        out["owner_name_raw"] = edge.owner_name_raw  # printed as stored, whatever it spells
        return out

    monkeypatch.setattr(serialize_module, "serialize_asset_owner", regressed)

    result = run.run_audit(db_sessionmaker, persist=False)

    printed = [
        b
        for b in _breaches(result, "assets")
        if b["public_id"] == seeded["plant"].public_id and b["reason"].startswith("withheld_name_printed:")
    ]
    assert [b["reason"] for b in printed] == ["withheld_name_printed:detail:owners[0].owner_name_raw"]
    assert printed[0]["served_status"] == 200 and printed[0]["served_leak"] is True
    assert result["m11"] == len(result["breaches"]) == 1
    # The audit row names the path, never the withheld spelling.
    assert HIDDEN_OPERATOR.lower() not in json.dumps(result["breaches"]).lower()


def test_withheld_name_paths_reads_raw_strings_on_an_edge_to_a_public_organisation() -> None:
    from pipeline.normalize import org_key
    from services.api.withheld_names import WithheldNames

    withheld = WithheldNames(keys=frozenset({org_key(HIDDEN_OPERATOR)}))
    shape = {
        "operator_name": None,
        "owners": [
            {
                "organization": {"public_id": "org_public", "name_canonical": "Open Owner Holdings"},
                "owner_name_raw": HIDDEN_OPERATOR_REGISTER,
                "provenance": {"source_name": "Test"},
            },
            {"organization": {"public_id": "org_public"}, "owner_name_raw": "Open Owner Holdings"},
            {"organization": {"public_id": "org_public"}, "owner_name_raw": None},
            # The edge's organisation summary is the public owner it points to, not raw text.
            {"organization": {"public_id": "org_twin", "name_canonical": HIDDEN_OPERATOR}},
        ],
    }
    assert run.withheld_name_paths(shape, withheld, operator_edge=False) == ["owners[0].owner_name_raw"]
