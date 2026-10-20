"""`infra/scheduler/preflight.py`: each check's PASS, WARN and FAIL paths, the runner's timeouts and
budget, the verdict and exit code, the stored record, and the `bootstrap preflight` dispatch.

Store checks run on an in-memory SQLite store; the map timing on a stubbed HTTP client; the settings
checks on a patched environment mapping. Nothing sends email or opens a non-loopback socket. The
Postgres half (`READ ONLY`, `statement_timeout`) runs under `POSTGIS_TEST_URL` and skips without it.
"""

from __future__ import annotations

import base64
import datetime as dt
import io
import json
import os
import threading
import uuid
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest
import yaml
from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from infra.scheduler import bootstrap, jobs, preflight
from infra.scheduler.preflight import FAIL, PASS, WARN, Check, Context, Outcome, WebUnreachable
from services.api.common import DOMAIN
from services.db.models import (
    Event,
    Licence,
    Opportunity,
    OpportunitySource,
    Proposal,
    ProposalSource,
    Site,
    SiteMember,
    Source,
    SourceRun,
)
from services.db.session import get_engine, get_sessionmaker, init_db
from services.ids import public_id

UTC = dt.UTC
NOW = dt.datetime(2026, 10, 14, 9, 0, tzinfo=UTC)
ROOT = Path(__file__).resolve().parents[2]
CAISO = "us.iso.caiso.gen_queue"
TEST_SOURCE = "us.test.public_source"

#: A bcrypt hash's shape, base64-encoded as docs/64 §2 step 6 asks (not a real password's hash).
GATE_HASH = base64.b64encode(b"$2a$14$" + b"x" * 53).decode()
GOOD_ENV: dict[str, str] = {
    "DOMAIN": DOMAIN,
    "PLATFORM_POSTURE": "noncommercial",
    "RESEND_API_KEY": "re_test_key",
    "SENDER_LEGAL_NAME": "Example Data Company LLC",
    "SENDER_POSTAL_ADDRESS": "100 Main Street, Austin, TX 78701",
    "SENTRY_DSN": "https://key@o0.ingest.sentry.io/0",
    "CSP_MODE": "report",
    "SITE_ACCESS": "basic",
    "SITE_ACCESS_USER": "beta",
    "SITE_ACCESS_HASH": GATE_HASH,
}


# ------------------------------------------------------------------------------------ fixtures
def _factory() -> sessionmaker[Session]:
    engine = get_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    return get_sessionmaker(engine)


@pytest.fixture()
def factory() -> sessionmaker[Session]:
    return _factory()


def _stamp(factory: sessionmaker[Session], version: str) -> None:
    with factory() as session:
        session.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
        session.execute(text("INSERT INTO alembic_version (version_num) VALUES (:v)"), {"v": version})
        session.commit()


class _Store:
    """A connector store reduced to the one read the held-run check makes."""

    def __init__(self, by_source: Mapping[str, list[dict[str, Any]]] | None = None) -> None:
        self.by_source = dict(by_source or {})

    def runs(self, source_id: str) -> list[dict[str, Any]]:
        return list(self.by_source.get(source_id, []))


class _Audit:
    def __init__(self, result: Mapping[str, Any]) -> None:
        self.result = dict(result)
        self.calls = 0

    def __call__(self, _factory: sessionmaker[Session]) -> Mapping[str, Any]:
        self.calls += 1
        return self.result


def _ctx(
    tmp_path: Path,
    factory: sessionmaker[Session] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    database_ok: bool | None = None,
    **fields: Any,
) -> Context:
    defaults: dict[str, Any] = {
        "handcheck_dirs": [tmp_path / "handcheck"],
        "open_store": lambda _root: _Store(),
        "audit": _Audit({"m11": 0, "served": {"checked": 0, "leaks": 0, "inconclusive": 0}}),
    }
    ctx = Context(
        now=NOW,
        data_root=tmp_path,
        env=dict(GOOD_ENV if env is None else env),
        session_factory=factory,
        **{**defaults, **fields},
    )
    ctx.database_ok = factory is not None if database_ok is None else database_ok
    return ctx


def _licence(session: Session, id_: str = "open-lic") -> Licence:
    lic = Licence(
        id=id_,
        name="Open Licence",
        reuse_class="open",
        allows_derived_publication=True,
        allows_raw_publication=True,
        allows_api_redistribution=True,
        allows_bulk_export=True,
        allows_commercial_use=True,
        gate_flag=False,
        evidence_url="https://example.org/terms",
        evidence_retrieved_at=dt.datetime(2026, 9, 1, tzinfo=UTC),
        classified_by="legal-compliance",
    )
    session.add(lic)
    session.flush()
    return lic


def _source(
    session: Session, licence: Licence, id_: str = TEST_SOURCE, *, loaded: str | None = None
) -> Source:
    src = Source(
        id=id_,
        name=f"Test {id_}",
        category="generation_queue",
        url=f"https://example.org/{id_}",
        access="bulk_file",
        cadence="weekly",
        licence_id=licence.id,
        publish_state="public",
        implemented=True,
        last_loaded_ts=loaded,
    )
    session.add(src)
    session.flush()
    return src


def _proposal(
    session: Session,
    source: Source,
    suffix: str,
    *,
    lifecycle_state: str = "filed",
    gone: bool = False,
) -> Proposal:
    prop = Proposal(
        public_id="",
        slug=f"test-{suffix}",
        kind="storage",
        name_canonical=f"Test Storage {suffix}",
        jurisdiction="US-TX",
        lifecycle_state=lifecycle_state,
        publish_state="public",
        published_at=NOW - dt.timedelta(days=20),
        public_at=NOW - dt.timedelta(days=1),
        min_reuse_class=source.licence.reuse_class,
        source_count=1,
    )
    session.add(prop)
    session.flush()
    prop.public_id = public_id("prop", prop.id)
    session.add(
        ProposalSource(
            proposal_id=prop.id,
            source_id=source.id,
            source_record_id=f"Q{suffix}",
            source_url=f"{source.url}#{suffix}",
            retrieved_at=NOW - dt.timedelta(days=1),
            licence_id=source.licence_id,
            raw={},
            first_seen=NOW - dt.timedelta(days=30),
            last_seen=NOW - dt.timedelta(days=1),
            gone_at=NOW - dt.timedelta(days=2) if gone else None,
        )
    )
    session.flush()
    return prop


def _opportunity(session: Session, source: Source, suffix: str) -> Opportunity:
    opp = Opportunity(
        public_id="",
        slug=f"rfp-{suffix}",
        kind="rfp",
        title=f"Test RFP {suffix}",
        jurisdiction="US-AZ",
        technologies=["solar_pv"],
        status="open",
        publish_state="public",
        published_at=NOW - dt.timedelta(days=10),
        public_at=NOW - dt.timedelta(hours=1),
        min_reuse_class=source.licence.reuse_class,
        source_count=1,
    )
    session.add(opp)
    session.flush()
    opp.public_id = public_id("opp", opp.id)
    session.add(
        OpportunitySource(
            opportunity_id=opp.id,
            source_id=source.id,
            source_record_id=f"N{suffix}",
            source_url=f"{source.url}#N{suffix}",
            retrieved_at=NOW - dt.timedelta(hours=1),
            licence_id=source.licence_id,
            raw={},
            first_seen=NOW - dt.timedelta(days=5),
            last_seen=NOW - dt.timedelta(hours=1),
        )
    )
    session.flush()
    return opp


def _event(session: Session, prop: Proposal, source: Source, key: str) -> None:
    session.add(
        Event(
            subject_type="proposal",
            subject_id=prop.id,
            event_type="status_change",
            observed_at=NOW - dt.timedelta(days=2),
            source_id=source.id,
            source_url=source.url,
            retrieved_at=NOW - dt.timedelta(days=2),
            licence_id=source.licence_id,
            changed_keys=["lifecycle_state"],
            public_at=NOW - dt.timedelta(days=1),
            published_at=NOW - dt.timedelta(days=1),
            idempotency_key=key,
        )
    )
    session.flush()


def _seed_store(factory: sessionmaker[Session]) -> dict[str, Any]:
    """Two loaded sources: three proposals and an event on one, an opportunity on the other."""
    with factory() as session:
        lic = _licence(session)
        src = _source(session, lic, loaded="20261013T030700Z")
        opp_src = _source(session, lic, "us.test.tenders", loaded="20261013T030700Z")
        a = _proposal(session, src, "1")
        _proposal(session, src, "2", gone=True)
        _proposal(session, src, "3", lifecycle_state="withdrawn", gone=True)
        _opportunity(session, opp_src, "1")
        _event(session, a, src, "k1")
        session.commit()
        return {"licence": lic.id, "first": a.id}


# ------------------------------------------------------------------------------- 1. database
def test_database_at_the_migration_head_passes(tmp_path: Path, factory: sessionmaker[Session]) -> None:
    heads = sorted(preflight.migration_heads())
    _stamp(factory, heads[0])
    ctx = _ctx(tmp_path, factory, database_ok=False)
    out = preflight.check_database(ctx)
    assert out.status == PASS and ctx.database_ok, out
    assert out.measured == {"dialect": "sqlite", "current": heads, "heads": heads}
    assert heads[0] in out.summary


def test_database_behind_the_head_fails_and_says_how_to_migrate(
    tmp_path: Path, factory: sessionmaker[Session]
) -> None:
    _stamp(factory, "0001")
    ctx = _ctx(tmp_path, factory, database_ok=False)
    out = preflight.check_database(ctx)
    assert out.status == FAIL and "is at 0001" in out.summary, out
    assert ctx.database_ok  # reachable: the other checks still read it
    assert any("upgrade head" in a for a in out.actions)


def test_a_database_never_migrated_fails(tmp_path: Path, factory: sessionmaker[Session]) -> None:
    out = preflight.check_database(_ctx(tmp_path, factory, database_ok=False))
    assert out.status == FAIL and "no alembic_version" in out.summary, out


def test_no_database_url_and_an_unreachable_database_fail(tmp_path: Path) -> None:
    out = preflight.check_database(_ctx(tmp_path, None))
    assert out.status == FAIL and "DATABASE_URL is not set" in out.summary
    unreachable = get_sessionmaker(get_engine(f"sqlite+pysqlite:///{tmp_path / 'missing' / 'x.db'}"))
    ctx = _ctx(tmp_path, unreachable, database_ok=False)
    out = preflight.check_database(ctx)
    assert out.status == FAIL and "unreachable" in out.summary and not ctx.database_ok, out


# ------------------------------------------------------------------------------ 2. freshness
def _row(source_id: str, status: str, **extra: Any) -> dict[str, Any]:
    return {
        "source_id": source_id,
        "status": status,
        "age_hours": 400.0 if status == "stale" else 2.0,
        "stale_after_hours": 384.0,
        "vintage": None,
        "vintage_stale": False,
        "vintage_overdue_at": None,
        **extra,
    }


def _rows(*rows: dict[str, Any]) -> Callable[[dt.datetime, Path], list[dict[str, Any]]]:
    return lambda _now, _root: list(rows)


def test_fresh_sources_pass_and_unscheduled_ones_are_left_out(tmp_path: Path) -> None:
    ctx = _ctx(
        tmp_path,
        database_ok=False,
        freshness_rows=_rows(_row("a", "fresh"), _row("b", "late"), _row("c", "unscheduled")),
    )
    out = preflight.check_freshness(ctx)
    assert out.status == PASS and out.summary == "2 scheduled sources: 1 fresh, 1 late", out


def test_a_stale_source_warns_with_its_age_or_its_overdue_release(tmp_path: Path) -> None:
    ctx = _ctx(
        tmp_path,
        database_ok=False,
        freshness_rows=_rows(
            _row("a", "stale"),
            _row(
                "b",
                "stale",
                vintage="2026-08",
                vintage_stale=True,
                vintage_overdue_at="2026-10-06T00:00:00Z",
            ),
        ),
    )
    out = preflight.check_freshness(ctx)
    assert out.status == WARN, out
    assert "a: stale (last success 16.7 d ago, stale after 16.0 d)" in out.actions
    assert "b: stale (the release after 2026-08 was due 2026-10-06T00:00:00Z)" in out.actions


def test_a_source_never_run_fails_and_names_the_fetch(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, database_ok=False, freshness_rows=_rows(_row("a", "fresh"), _row("b", "never")))
    out = preflight.check_freshness(ctx)
    assert out.status == FAIL, out
    assert out.actions[0] == "no successful run record (ok or unchanged) in the store: b"
    assert out.actions[-1].endswith("python -m infra.scheduler.bootstrap fetch")


def test_the_source_rows_are_assessed_too(tmp_path: Path, factory: sessionmaker[Session]) -> None:
    """Fresh in the run records, but the `source` row the masthead reads has no success (FAIL), or
    there is no row at all (WARN)."""
    jobs.record_source_run(factory, CAISO, {"status": "ok", "finished_at": "2026-10-14T08:00:00Z"})
    with factory() as session:
        row = session.get(Source, CAISO)
        assert row is not None
        row.implemented = True
        session.commit()
    rows = _rows(_row(CAISO, "fresh"), _row("us.eia.860m", "fresh"))
    out = preflight.check_freshness(_ctx(tmp_path, factory, freshness_rows=rows))
    assert out.status == WARN, out
    assert out.measured["sources"][CAISO]["database"] == "fresh"
    assert "no `source` row, so no fetch is recorded in the database: us.eia.860m" in out.actions

    with factory() as session:
        row = session.get(Source, CAISO)
        assert row is not None
        row.last_success_at = None
        session.commit()
    out = preflight.check_freshness(_ctx(tmp_path, factory, freshness_rows=_rows(_row(CAISO, "fresh"))))
    assert out.status == FAIL and CAISO in out.actions[0] and "`source` row" in out.actions[0], out


def test_the_real_store_report_on_an_empty_data_root_fails(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, database_ok=False, freshness_rows=preflight.default_freshness_rows)
    out = preflight.check_freshness(ctx)
    assert out.status == FAIL and out.measured["by_status"] == {"never": out.measured["scheduled"]}, out


# ------------------------------------------------------------------------------ 3. held runs
def _held(factory: sessionmaker[Session], *, started: str = "2026-10-13T03:07:00Z") -> str:
    run_id = str(uuid.uuid4())
    jobs.record_source_run(
        factory,
        CAISO,
        {
            "id": run_id,
            "status": "partial",
            "dq_status": "fail",
            "started_at": started,
            "finished_at": started,
        },
    )
    return run_id


def test_a_held_run_fails_with_its_admin_release_link(tmp_path: Path, factory: sessionmaker[Session]) -> None:
    run_id = _held(factory)
    out = preflight.check_held_runs(_ctx(tmp_path, factory))
    assert out.status == FAIL, out
    run_public = public_id("run", uuid.UUID(run_id))
    assert out.measured["held"] == [
        {"source_id": CAISO, "run_id": run_public, "started_at": "2026-10-13T03:07:00Z", "where": "database"}
    ]
    assert f"https://admin.{DOMAIN}/admin/source-runs/{run_public}" in out.actions[0]
    assert "Release it with a reason" in out.actions[0]


def test_a_superseded_or_released_hold_does_not_wait(tmp_path: Path, factory: sessionmaker[Session]) -> None:
    run_id = _held(factory)
    with factory() as session:
        run = session.get(SourceRun, uuid.UUID(run_id))
        assert run is not None
        run.released_at = NOW
        session.commit()
    assert preflight.check_held_runs(_ctx(tmp_path, factory)).status == PASS

    other = _factory()
    _held(other)
    jobs.record_source_run(
        other,
        CAISO,
        {"id": str(uuid.uuid4()), "status": "ok", "started_at": "2026-10-14T03:07:00Z"},
    )
    out = preflight.check_held_runs(_ctx(tmp_path, other))
    assert out.status == PASS and out.summary == "no held run waits for release", out


def test_a_hold_in_the_store_without_a_run_row_fails(tmp_path: Path, factory: sessionmaker[Session]) -> None:
    store = _Store(
        {
            CAISO: [
                {"id": "r1", "status": "ok", "outputs": {"normalized": "x.parquet"}},
                {"id": "r2", "status": "partial", "outputs": {"held": "held/x.parquet"}},
                {"id": "r3", "status": "failed"},  # no output: does not supersede the hold
            ]
        }
    )
    out = preflight.check_held_runs(_ctx(tmp_path, factory, open_store=lambda _root: store))
    assert out.status == FAIL and out.measured["held"][0]["where"] == "store", out
    assert "no source_run row" in out.actions[0] and "bootstrap fetch" in out.actions[0]

    # The same hold, once recorded in the database, is judged there and listed once.
    run_id = _held(factory)
    store.by_source[CAISO][1]["id"] = run_id
    out = preflight.check_held_runs(_ctx(tmp_path, factory, open_store=lambda _root: store))
    assert [h["where"] for h in out.measured["held"]] == ["database"], out


# --------------------------------------------------------------------------------- 4. counts
def test_counts_pass_with_live_records_per_loaded_source(
    tmp_path: Path, factory: sessionmaker[Session]
) -> None:
    _seed_store(factory)
    out = preflight.check_counts(_ctx(tmp_path, factory))
    assert out.status == PASS, out
    totals = out.measured["totals"]
    assert totals == {
        "proposals_live": 3,
        "proposals_public": 3,
        "opportunities_live": 1,
        "opportunities_public": 1,
        "events": 1,
        "events_public": 1,
    }
    assert out.measured["per_source"][TEST_SOURCE]["proposals"] == 3
    assert out.measured["per_source"]["us.test.tenders"]["opportunities"] == 1
    assert out.summary.startswith("3 live proposals (3 public), 1 opportunities (1 public), 1 events")
    assert out.actions == ["no previous preflight record to compare against"]


def test_a_loaded_source_with_no_live_record_fails_unless_its_loader_writes_none(
    tmp_path: Path, factory: sessionmaker[Session]
) -> None:
    _seed_store(factory)
    with factory() as session:
        lic = session.get(Licence, "open-lic")
        assert lic is not None
        _source(session, lic, "us.test.empty", loaded="20261013T030700Z")
        # A catalogue watch: its loader writes no proposal by design (services/ingest/large_load_watch.py).
        _source(session, lic, "us.iso.ercot.large_load_queue", loaded="20261013T030700Z")
        session.commit()
    out = preflight.check_counts(_ctx(tmp_path, factory))
    assert out.status == FAIL, out
    assert out.actions[0] == (
        "us.test.empty loaded run 20261013T030700Z but has no live proposal or opportunity"
    )
    assert not any("large_load_queue" in a for a in out.actions)
    assert "specialised_loader" in out.measured["per_source"]["us.iso.ercot.large_load_queue"]


def test_an_empty_store_fails(tmp_path: Path, factory: sessionmaker[Session]) -> None:
    out = preflight.check_counts(_ctx(tmp_path, factory))
    assert out.status == FAIL and "no live proposal" in out.actions[0], out


def test_a_store_with_nothing_public_fails(tmp_path: Path, factory: sessionmaker[Session]) -> None:
    _seed_store(factory)
    with factory() as session:
        for prop in session.scalars(select(Proposal)):
            prop.publish_state = "pending_review"
        session.commit()
    out = preflight.check_counts(_ctx(tmp_path, factory))
    assert out.status == FAIL and "no proposal is public" in out.actions[0], out


def test_counts_far_from_the_previous_preflights_warn(tmp_path: Path, factory: sessionmaker[Session]) -> None:
    _seed_store(factory)
    first = preflight.check_counts(_ctx(tmp_path, factory))
    near = {"started_at": "2026-10-13T09:00:00Z", "checks": [{"id": "counts", "measured": first.measured}]}
    assert preflight.check_counts(_ctx(tmp_path, factory, previous=near)).status == PASS

    far_measured = json.loads(json.dumps(first.measured))
    far_measured["totals"]["proposals_live"] = 400
    far_measured["per_source"][TEST_SOURCE]["proposals"] = 300
    far = {"started_at": "2026-10-13T09:00:00Z", "checks": [{"id": "counts", "measured": far_measured}]}
    out = preflight.check_counts(_ctx(tmp_path, factory, previous=far))
    assert out.status == WARN, out
    assert out.measured["drift"] == ["proposals_live: 400 -> 3", f"{TEST_SOURCE} proposals: 300 -> 3"]
    assert "since the preflight of 2026-10-13T09:00:00Z" in out.summary


# ------------------------------------------------------------------------ 5. visibility audit
def test_the_audit_runs_now_when_none_ran_today(tmp_path: Path, factory: sessionmaker[Session]) -> None:
    audit = _Audit({"m11": 0, "posture": "noncommercial", "served": {"checked": 7, "leaks": 0}})
    out = preflight.check_visibility_audit(_ctx(tmp_path, factory, audit=audit))
    assert out.status == PASS and audit.calls == 1, out
    assert out.summary == "M-11 = 0 (run now, not persisted, posture noncommercial); 7 served checks, 0 leaks"


def test_any_breach_fails(tmp_path: Path, factory: sessionmaker[Session]) -> None:
    breach = {
        "surface": "proposals",
        "public_id": "prop_x",
        "source_id": "us.iso.pjm.gen_queue",
        "reason": "r",
    }
    audit = _Audit({"m11": 2, "breaches": [breach], "served": {"checked": 3, "leaks": 1}})
    out = preflight.check_visibility_audit(_ctx(tmp_path, factory, audit=audit))
    assert out.status == FAIL and "M-11 = 2" in out.summary, out
    assert out.measured["breaches"] == [breach]
    assert "S1 incident" in out.actions[0]


def test_inconclusive_served_checks_warn(tmp_path: Path, factory: sessionmaker[Session]) -> None:
    audit = _Audit({"m11": 0, "served": {"checked": 3, "leaks": 0, "inconclusive": 2}})
    assert preflight.check_visibility_audit(_ctx(tmp_path, factory, audit=audit)).status == WARN


def test_todays_nightly_result_is_read_instead_of_running_again(
    tmp_path: Path, factory: sessionmaker[Session]
) -> None:
    from services.visibility_audit.run import persist_result

    def persist(at: dt.datetime, m11: int) -> None:
        with factory() as session:
            persist_result(
                session,
                {"run_id": str(uuid.uuid4()), "run_at": at.isoformat(), "m11": m11, "served": {}},
            )
            session.commit()

    persist(NOW - dt.timedelta(days=1), 0)
    audit = _Audit({"m11": 0, "served": {}})
    preflight.check_visibility_audit(_ctx(tmp_path, factory, audit=audit))
    assert audit.calls == 1  # yesterday's result does not count

    persist(NOW.replace(hour=4, minute=52), 1)
    audit = _Audit({"m11": 0, "served": {}})
    out = preflight.check_visibility_audit(_ctx(tmp_path, factory, audit=audit))
    assert audit.calls == 0 and out.status == FAIL, out
    assert out.measured["origin"] == "nightly run of 2026-10-14T04:52:00Z"


def test_the_real_audit_on_an_empty_store_passes(tmp_path: Path, factory: sessionmaker[Session]) -> None:
    ctx = _ctx(tmp_path, factory, audit=preflight.default_audit)
    out = preflight.check_visibility_audit(ctx)
    assert out.status == PASS and out.measured["m11"] == 0, out
    with factory() as session:  # persist=False: the run left no audit event behind
        assert session.scalars(select(Event)).first() is None


# ---------------------------------------------------------------------------------- 6. sites
def _site(factory: sessionmaker[Session], *, flag: str | None = None) -> str:
    from services.sites.rules import RULE_VERSION

    with factory() as session:
        members = list(session.scalars(select(Proposal).order_by(Proposal.public_id)))[:2]
        site = Site(
            public_id="site_0000000001",
            slug="test-site",
            name_display="Test Site",
            lead_proposal_id=members[0].id,
            member_count=len(members),
            rule_version=RULE_VERSION,
            review_flag=flag,
            anchors={},
            built_at=NOW - dt.timedelta(hours=5),
        )
        session.add(site)
        session.flush()
        for rank, prop in enumerate(members):
            session.add(
                SiteMember(
                    site_id=site.id,
                    proposal_id=prop.id,
                    is_lead=rank == 0,
                    lead_rank=rank,
                    group_key=f"proposal:{prop.public_id}",
                    grouping_rule="eia_plant",
                    grouping_evidence={},
                    relation="lead" if rank == 0 else "unit_of",
                    relation_rule="t",
                    confidence="high",
                    basis={},
                )
            )
        session.commit()
        return site.public_id


def test_sites_pass_with_counts_and_the_last_rebuild(tmp_path: Path, factory: sessionmaker[Session]) -> None:
    _seed_store(factory)
    _site(factory)
    out = preflight.check_sites(_ctx(tmp_path, factory))
    assert out.status == PASS, out
    assert (
        out.summary == "1 sites, 2 members, 0 oversize; last rebuild that changed a site 2026-10-14T04:00:00Z"
    )
    assert out.measured["member_rows"] == 2 and out.measured["sites_enabled"]


def test_an_oversize_site_fails(tmp_path: Path, factory: sessionmaker[Session]) -> None:
    _seed_store(factory)
    site = _site(factory, flag="oversize")
    out = preflight.check_sites(_ctx(tmp_path, factory, env={**GOOD_ENV, "SITES_ENABLED": "0"}))
    assert out.status == FAIL and out.measured["oversize"] == [{"site": site, "members": 2}], out
    assert site in out.actions[0]
    assert out.summary.endswith("SITES_ENABLED is off, so readers see none")


def test_no_site_over_live_proposals_warns(tmp_path: Path, factory: sessionmaker[Session]) -> None:
    _seed_store(factory)
    out = preflight.check_sites(_ctx(tmp_path, factory))
    assert out.status == WARN and "site rebuild" in out.actions[0], out


# ------------------------------------------------------------------------------- 7. unlisted
def test_records_no_register_lists_are_counted_over_active_states_only(
    tmp_path: Path, factory: sessionmaker[Session]
) -> None:
    _seed_store(factory)  # 1 listed, 1 delisted (filed), 1 delisted but withdrawn
    out = preflight.check_unlisted(_ctx(tmp_path, factory))
    assert out.status == PASS, out
    assert out.measured["unlisted_active"] == 1 and out.measured["active_public"] == 2
    assert out.summary.startswith(
        "1 of 2 active public proposals are no longer listed by any register (50.0%)"
    )


# ------------------------------------------------------------------------- 8. map count cost
class _Web:
    def __init__(self, ms: Mapping[str, float], *, status: int = 200, down: bool = False) -> None:
        self.ms, self.status, self.down = dict(ms), status, down
        self.urls: list[str] = []

    def __call__(self, url: str, timeout_s: float) -> tuple[int, float]:
        self.urls.append(url)
        if self.down:
            raise WebUnreachable("ConnectError: [Errno -2] Name or service not known")
        path = url.removeprefix(preflight.IN_STACK_WEB_URL)
        return self.status, self.ms[path] / 1000


def test_the_hidden_count_within_budget_passes(tmp_path: Path) -> None:
    web = _Web({"/": 430.0, "/?unlisted=include": 330.0})
    out = preflight.check_map_count_cost(_ctx(tmp_path, http_get=web))
    assert out.status == PASS, out
    assert len(web.urls) == 2 + 2 * preflight.MAP_TIMING_REQUESTS
    assert web.urls[0] == "http://web:8001/" and web.urls[1] == "http://web:8001/?unlisted=include"
    assert out.measured["median_ms"] == {"/": 430.0, "/?unlisted=include": 330.0}
    assert out.measured["difference_ms"] == 100.0


def test_the_hidden_count_over_150_ms_warns_and_names_the_alternative(tmp_path: Path) -> None:
    web = _Web({"/": 520.0, "/?unlisted=include": 330.0})
    out = preflight.check_map_count_cost(_ctx(tmp_path, http_get=web))
    assert out.status == WARN and "difference +190 ms" in out.summary, out
    assert "line without one" in out.actions[0] and "docs/30 §5.1" in out.actions[0]


def test_an_unreachable_or_failing_web_service_is_skipped_with_a_warning(tmp_path: Path) -> None:
    out = preflight.check_map_count_cost(_ctx(tmp_path, http_get=_Web({}, down=True)))
    assert out.status == WARN and "unreachable at http://web:8001" in out.summary, out
    assert any(f"https://{DOMAIN}$p" in a and "sed -n 8p" in a for a in out.actions)

    web = _Web({"/": 1.0, "/?unlisted=include": 1.0}, status=500)
    out = preflight.check_map_count_cost(_ctx(tmp_path, http_get=web))
    assert out.status == WARN and "answered 500" in out.summary, out


def test_the_in_stack_url_is_the_one_caddy_proxies() -> None:
    caddyfile = (ROOT / "infra" / "compose" / "Caddyfile").read_text(encoding="utf-8")
    assert "reverse_proxy web:8001" in caddyfile
    assert preflight.IN_STACK_WEB_URL == "http://web:8001"


# ---------------------------------------------------------------------------------- 9. email
def test_email_settings_present_pass(tmp_path: Path) -> None:
    out = preflight.check_email(_ctx(tmp_path))
    assert out.status == PASS, out
    assert (
        out.summary
        == "RESEND_API_KEY set, SENDER_LEGAL_NAME set, SENDER_POSTAL_ADDRESS set; nothing was sent"
    )
    assert "re_test_key" not in json.dumps(out.measured)


def test_no_resend_key_fails_because_nothing_would_send(tmp_path: Path) -> None:
    env = {k: v for k, v in GOOD_ENV.items() if k != "RESEND_API_KEY"}
    out = preflight.check_email(_ctx(tmp_path, env=env))
    assert out.status == FAIL, out
    assert "dry run" in out.actions[0] and "dev_verification_url" in out.actions[0]


def test_the_secrets_examples_placeholder_sender_is_caught(tmp_path: Path) -> None:
    example = yaml.safe_load(
        (ROOT / "infra" / "sops" / "secrets.example.plain.yaml").read_text(encoding="utf-8")
    )
    env = {
        **GOOD_ENV,
        "SENDER_LEGAL_NAME": str(example["SENDER_LEGAL_NAME"]),
        "SENDER_POSTAL_ADDRESS": str(example["SENDER_POSTAL_ADDRESS"]),
    }
    out = preflight.check_email(_ctx(tmp_path, env=env))
    assert out.status == FAIL, out
    assert out.measured["settings"] == {
        "RESEND_API_KEY": "set",
        "SENDER_LEGAL_NAME": "placeholder",
        "SENDER_POSTAL_ADDRESS": "placeholder",
    }
    assert preflight.is_placeholder("[SENDER_LEGAL_NAME unset]")
    assert not preflight.is_placeholder(GOOD_ENV["SENDER_POSTAL_ADDRESS"])

    env = {**GOOD_ENV, "SENDER_POSTAL_ADDRESS": ""}
    out = preflight.check_email(_ctx(tmp_path, env=env))
    assert out.status == FAIL and out.measured["settings"]["SENDER_POSTAL_ADDRESS"] == "unset"


# --------------------------------------------------------------------------- 10. email domain
def test_the_served_domain_matching_the_code_passes(tmp_path: Path) -> None:
    out = preflight.check_email_domain(_ctx(tmp_path))
    assert out.status == PASS, out
    assert f"the Resend sending domain must be {DOMAIN}" in out.summary


def test_a_served_domain_other_than_the_codes_fails_naming_both(tmp_path: Path) -> None:
    out = preflight.check_email_domain(_ctx(tmp_path, env={**GOOD_ENV, "DOMAIN": "beta.example.org"}))
    assert out.status == FAIL, out
    assert out.summary == f"DOMAIN beta.example.org vs the code's {DOMAIN}"
    assert "DOMAIN is beta.example.org" in out.actions[0] and f"= {DOMAIN}" in out.actions[0]
    assert out.actions[-1].startswith(f"the Resend sending domain must be {DOMAIN}")


def test_an_unset_domain_cannot_be_compared(tmp_path: Path) -> None:
    env = {k: v for k, v in GOOD_ENV.items() if k != "DOMAIN"}
    assert preflight.check_email_domain(_ctx(tmp_path, env=env)).status == WARN


# ----------------------------------------------------------------------------- 11. monitoring
def test_monitoring_and_the_gate_configured_pass(tmp_path: Path) -> None:
    out = preflight.check_monitoring(_ctx(tmp_path))
    assert out.status == PASS and out.summary == "SITE_ACCESS basic with login, CSP_MODE report, Sentry on", (
        out
    )
    assert GATE_HASH not in json.dumps(out.measured)


@pytest.mark.parametrize(
    ("change", "status", "phrase"),
    [
        ({"SITE_ACCESS": "open"}, WARN, "public by the owner's decision"),  # deliberate (2026-10-20)
        ({"SITE_ACCESS": ""}, FAIL, "the site is open"),  # unset: open by accident
        ({"SITE_ACCESS_HASH": ""}, FAIL, "needs SITE_ACCESS_USER and SITE_ACCESS_HASH"),
        ({"SITE_ACCESS_HASH": "$2a$14$" + "x" * 53}, FAIL, "not a base64-encoded bcrypt hash"),
        ({"CSP_MODE": "enfroce"}, FAIL, "Caddy refuses"),
        ({"CSP_MODE": "enforce"}, WARN, "before the docs/64 §5 checks"),
        ({"SENTRY_DSN": ""}, WARN, "SENTRY_DSN is unset"),
        ({"CSP_MODE": ""}, PASS, ""),  # absent means report, Caddy's default
    ],
)
def test_monitoring_paths(tmp_path: Path, change: dict[str, str], status: str, phrase: str) -> None:
    out = preflight.check_monitoring(_ctx(tmp_path, env={**GOOD_ENV, **change}))
    assert out.status == status, out
    assert any(phrase in a for a in out.actions), out


# ----------------------------------------------------------------------------- 12. hand check
def test_a_beta_worksheet_passes(tmp_path: Path) -> None:
    folder = tmp_path / "handcheck" / "2026-10-14-beta"
    folder.mkdir(parents=True)
    (folder / "handcheck.xlsx").write_bytes(b"")
    out = preflight.check_handcheck(_ctx(tmp_path))
    assert out.status == PASS and str(folder) in out.summary, out


def test_a_missing_worksheet_warns_with_the_exact_command(tmp_path: Path) -> None:
    (tmp_path / "handcheck" / "2026-10-10-e2e-reference").mkdir(parents=True)  # not a beta worksheet
    out = preflight.check_handcheck(_ctx(tmp_path))
    assert out.status == WARN and "no <date>-beta worksheet" in out.summary, out
    assert out.actions[0].endswith(
        "run --rm --no-deps -T -v /opt/infraque/handcheck:/out worker python -m services.sites.handcheck "
        f"generate --out /out/2026-10-14-beta --seed 20261014 --posture noncommercial --base-url https://{DOMAIN}"
    )
    (tmp_path / "handcheck" / "2026-10-14-beta").mkdir()
    out = preflight.check_handcheck(_ctx(tmp_path))
    assert out.status == WARN and "without its workbook" in out.summary


def test_the_default_places_include_the_data_root_and_the_readme_mount(tmp_path: Path) -> None:
    dirs = preflight.default_handcheck_dirs(tmp_path)
    assert dirs[:3] == [tmp_path / "eval" / "handcheck", Path("/out"), Path("/opt/infraque/handcheck")]


# --------------------------------------------------------------------------------- the runner
def _check(
    n: int, id_: str, run: Callable[[Context], Outcome], *, timeout_s: float = 5, needs_db: bool = False
) -> Check:
    return Check(n, id_, id_.title(), run, timeout_s, needs_db=needs_db, order=n)


def _fixed(status: str) -> Callable[[Context], Outcome]:
    return lambda _ctx: Outcome(status, f"{status.lower()} figure")


def test_a_check_that_times_out_warns_naming_it(tmp_path: Path) -> None:
    release = threading.Event()

    def hangs(_ctx: Context) -> Outcome:
        release.wait(10)
        return Outcome(PASS, "late")

    try:
        results = preflight.run_checks(_ctx(tmp_path), [_check(1, "slow", hangs, timeout_s=0.2)])
    finally:
        release.set()
    assert results[0].status == WARN and results[0].summary == "check 1 (slow) timed out after 0.2 s"
    assert "--only slow --budget 600" in results[0].actions[0]


def test_a_check_that_raises_fails_and_the_budget_bounds_the_run(tmp_path: Path) -> None:
    def broken(_ctx: Context) -> Outcome:
        raise ValueError("bad row")

    results = preflight.run_checks(_ctx(tmp_path), [_check(1, "broken", broken)])
    assert results[0].status == FAIL and "ValueError: bad row" in results[0].summary

    results = preflight.run_checks(_ctx(tmp_path), [_check(1, "late", _fixed(PASS))], budget_s=0.5)
    assert results[0].status == WARN and "budget was spent" in results[0].summary


def test_store_checks_wait_for_the_database_check(tmp_path: Path) -> None:
    calls: list[str] = []

    def store(_ctx: Context) -> Outcome:
        calls.append("store")
        return Outcome(PASS, "read")

    checks = [_check(1, "database", _fixed(FAIL)), _check(2, "store", store, needs_db=True)]
    results = preflight.run_checks(_ctx(tmp_path, database_ok=False), checks, only=["store"])
    assert [r.id for r in results] == ["database", "store"] and calls == []
    assert results[1].status == WARN and "database check failed" in results[1].summary


def test_results_print_in_number_order_whatever_order_they_run(tmp_path: Path) -> None:
    seen: list[str] = []

    def note(id_: str) -> Callable[[Context], Outcome]:
        def run(_ctx: Context) -> Outcome:
            seen.append(id_)
            return Outcome(PASS, id_)

        return run

    checks = [
        Check(1, "one", "One", note("one"), 5, order=2),
        Check(2, "two", "Two", note("two"), 5, order=1),
    ]
    results = preflight.run_checks(_ctx(tmp_path), checks)
    assert seen == ["two", "one"] and [r.n for r in results] == [1, 2]


def test_go_exits_zero_with_warnings_and_stores_the_record(tmp_path: Path) -> None:
    checks = [_check(1, "a", _fixed(PASS)), _check(2, "b", _fixed(WARN))]
    out = io.StringIO()
    code = preflight.execute(_ctx(tmp_path), checks=checks, out=out)
    text_out = out.getvalue()
    assert code == 0, text_out
    assert "  1  PASS  A  pass figure" in text_out and "GO: 0 FAIL, 1 WARN, 1 PASS" in text_out
    stored = sorted((tmp_path / "preflight").glob("*.json"))
    assert [p.name for p in stored] == ["20261014T090000Z.json"]
    record = json.loads(stored[0].read_text(encoding="utf-8"))
    assert record["verdict"] == "GO" and record["record"] == str(stored[0])


def test_no_go_exits_one_and_json_is_the_record(tmp_path: Path) -> None:
    checks = [_check(1, "a", _fixed(PASS)), _check(2, "b", _fixed(FAIL))]
    out = io.StringIO()
    code = preflight.execute(_ctx(tmp_path), checks=checks, as_json=True, out=out)
    report = json.loads(out.getvalue())
    assert code == 1 and report["verdict"] == "NO-GO" and report["exit_code"] == 1
    assert report["totals"] == {"PASS": 1, "WARN": 0, "FAIL": 1}
    assert [c["status"] for c in report["checks"]] == [PASS, FAIL]
    assert json.loads(Path(report["record"]).read_text(encoding="utf-8"))["verdict"] == "NO-GO"


def test_the_next_run_compares_with_the_last_record_that_holds_counts(tmp_path: Path) -> None:
    def counts(_ctx: Context) -> Outcome:
        return Outcome(PASS, "c", {"totals": {"proposals_live": 10}, "per_source": {}})

    preflight.execute(_ctx(tmp_path), checks=[_check(4, "counts", counts)], out=io.StringIO())
    later = _ctx(tmp_path)
    later.now = NOW + dt.timedelta(minutes=5)
    preflight.execute(later, checks=[_check(9, "email", _fixed(PASS))], out=io.StringIO())
    previous = preflight.load_previous(tmp_path)
    assert previous is not None and previous["started_at"] == "2026-10-14T09:00:00Z"


def test_bootstrap_dispatches_preflight_with_its_own_flags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(preflight, "CHECKS", (_check(9, "email", _fixed(WARN)),))
    code = bootstrap.main(
        ["preflight", "--json", "--data-root", str(tmp_path), "--handcheck-dir", str(tmp_path)]
    )
    report = json.loads(capsys.readouterr().out)
    assert code == 0 and report["verdict"] == "GO" and report["data_root"] == str(tmp_path)
    assert [c["id"] for c in report["checks"]] == ["email"]
    with pytest.raises(SystemExit):
        bootstrap.main(["preflight", "--only", "nope"])


# --------------------------------------------------------------------------------- Postgres
POSTGRES_URL = os.environ.get("POSTGIS_TEST_URL")


@pytest.fixture()
def pg_factory() -> Iterator[sessionmaker[Session]]:
    if not POSTGRES_URL:
        pytest.skip("POSTGIS_TEST_URL not set")
    engine = get_engine(POSTGRES_URL)
    yield get_sessionmaker(engine)
    engine.dispose()


def test_on_postgres_every_read_is_read_only_and_bounded(
    tmp_path: Path, pg_factory: sessionmaker[Session]
) -> None:
    ctx = _ctx(tmp_path, pg_factory)
    ctx.timeout_s = 7
    with preflight.read_session(ctx) as session:
        assert session.scalar(text("SELECT current_setting('transaction_read_only')")) == "on"
        assert session.scalar(text("SELECT current_setting('statement_timeout')")) == "7s"
    assert preflight.check_database(ctx).status in (PASS, FAIL)
    for check in (preflight.check_sites, preflight.check_unlisted, preflight.check_counts):
        assert check(ctx).status in (PASS, WARN, FAIL)
