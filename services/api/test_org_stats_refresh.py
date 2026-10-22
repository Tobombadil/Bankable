"""`services/api/org_stats_refresh.py`: the refresh of `organization_stats` (migration 0038).

The API-side contract (the figures equal the portfolio's and pipeline's, the gates, the list) is
`services/api/test_org_stats.py`. Here: what the job writes and when, its staleness check, the
in-memory group walk against `orgtree.org_scope` itself (cycles and caps included), and the CLI on
a SQLite store built before the migration.
"""

from __future__ import annotations

import json
import pathlib
from collections.abc import Iterator
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from services.api import org_stats
from services.api import org_stats_refresh as job
from services.api.conftest import make_open_licence, make_org, make_public_source, make_visible_proposal
from services.api.orgtree import MAX_DEPTH, org_scope
from services.db.models import Organization, OrganizationStats
from services.db.session import get_engine, get_sessionmaker, init_db
from tests.db_template import disposing, fresh_engine


@pytest.fixture()
def db_sessionmaker() -> Iterator[sessionmaker[Session]]:
    with disposing(fresh_engine()) as engine:
        yield get_sessionmaker(engine)


@pytest.fixture()
def db(db_sessionmaker: sessionmaker[Session]) -> Iterator[Session]:
    with db_sessionmaker() as session:
        yield session


def _rows(db: Session) -> dict[tuple[str, str, str], OrganizationStats]:
    return {(str(r.organization_id), r.scope, r.tier): r for r in db.scalars(select(OrganizationStats))}


def test_a_refresh_writes_one_row_per_company_scope_and_tier_then_nothing(db: Session) -> None:
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    parent, child = make_org(db, "Parent Power"), make_org(db, "Child Solar")
    child.parent_org_id = parent.id
    make_visible_proposal(db, src, sponsor=child, lifecycle_state="filed")
    db.commit()
    first = job.refresh(db)
    db.commit()
    assert (first.rows, first.inserted, first.written) == (8, 8, 8)
    rows = _rows(db)
    assert rows[(str(parent.id), "self", "public")].pipeline_count == 0
    assert rows[(str(parent.id), "group", "public")].pipeline_count == 1
    assert rows[(str(parent.id), "group", "pro")].organizations == 2
    second = job.refresh(db)
    assert (second.written, second.unchanged, second.gate_key_changed) == (0, 8, False)


def test_a_gate_change_alone_rekeys_without_rewriting_the_figures(db: Session) -> None:
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    org = make_org(db, "Rekey Energy")
    db.commit()
    job.refresh(db)
    db.commit()
    before = _rows(db)[(str(org.id), "self", "public")].computed_at
    # A source no figure rests on changes state: the key moves, the figures do not.
    other = make_public_source(db, lic, id_="us.test.other")
    other.publish_state = "ingest_only"
    db.commit()
    assert job.is_stale(db)
    report = job.refresh(db)
    db.commit()
    assert (report.rekeyed, report.updated, report.gate_key_changed) == (4, 0, True)
    db.expire_all()
    row = _rows(db)[(str(org.id), "self", "public")]
    assert row.gate_key == org_stats.gate_key(db) and row.computed_at == before
    assert not job.is_stale(db)
    assert src.id  # the counted register stays on the surface


def test_the_staleness_check(db: Session) -> None:
    assert not job.is_stale(db)  # nothing to count, nothing to refresh
    make_org(db, "First Company")
    db.commit()
    assert job.is_stale(db)  # never refreshed
    job.refresh(db)
    db.commit()
    assert not job.is_stale(db)
    org = db.scalar(select(Organization))
    assert org is not None
    org.publish_state = "unpublished"
    db.commit()
    assert job.is_stale(db)


def test_run_reports_counts_and_skips_a_current_store(db_sessionmaker: sessionmaker[Session]) -> None:
    with db_sessionmaker() as db:
        make_org(db, "Run Co")
        db.commit()
    assert job.run(db_sessionmaker)["inserted"] == 4
    assert job.run(db_sessionmaker, if_stale=True) == {"skipped": "current"}
    assert job.run(db_sessionmaker)["written"] == 0


# --------------------------------------------------------------------------- the group walk
def _tree(db: Session, edges: dict[str, str | None], hidden: tuple[str, ...] = ()) -> dict[str, Organization]:
    orgs = {name: make_org(db, name) for name in edges}
    for name, parent in edges.items():
        orgs[name].parent_org_id = orgs[parent].id if parent else None
    for name in hidden:
        orgs[name].publish_state = "unpublished"
    db.commit()
    return orgs


@pytest.mark.parametrize(
    ("edges", "hidden"),
    [
        # A fund over two holding companies, one with a grandchild; an unrelated leaf.
        ({"Fund": None, "Hold A": "Fund", "Hold B": "Fund", "Op A1": "Hold A", "Leaf": None}, ()),
        # A taken-down subsidiary takes everything below it out of the group.
        ({"Top": None, "Gone": "Top", "Under Gone": "Gone", "Kept": "Top"}, ("Gone",)),
        # A two-node cycle and a self-parent: visited once, never hung.
        ({"Cyc A": "Cyc B", "Cyc B": "Cyc A", "Selfish": "Selfish"}, ()),
    ],
)
def test_the_in_memory_walk_is_org_scopes_walk(
    db: Session, edges: dict[str, str | None], hidden: tuple[str, ...]
) -> None:
    orgs = _tree(db, edges, hidden)
    listed = org_stats.listed_organizations(db)
    groups = org_stats.group_scopes(listed)
    for org in orgs.values():
        if org.publish_state != "public":
            assert org.id not in groups
            continue
        walked = org_scope(db, org, "all").ids
        assert set(groups.get(org.id, [org.id])) == set(walked), org.name_canonical
        assert groups.get(org.id, [org.id])[0] == org.id


def test_the_in_memory_walk_stops_where_org_scope_does(db: Session) -> None:
    chain = {"Link 0": None, **{f"Link {i}": f"Link {i - 1}" for i in range(1, MAX_DEPTH + 3)}}
    orgs = _tree(db, chain)
    groups = org_stats.group_scopes(org_stats.listed_organizations(db))
    root = orgs["Link 0"]
    assert groups[root.id] == org_scope(db, root, "all").ids
    assert len(groups[root.id]) == MAX_DEPTH + 1


# ---------------------------------------------------------------------------------- the CLI
def test_the_cli_creates_the_table_on_a_store_built_before_the_migration(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "old.db"
    engine = get_engine(f"sqlite+pysqlite:///{path}")
    init_db(engine)
    with engine.begin() as conn:
        conn.execute(sa.text("DROP TABLE organization_stats"))
    with get_sessionmaker(engine)() as db:
        make_org(db, "Old Store Co")
        db.commit()
    engine.dispose()
    assert job.main(["--db", str(path)]) == 0
    report: dict[str, Any] = json.loads(capsys.readouterr().out)
    assert report["inserted"] == 4
    assert job.main(["--db", str(path), "--if-stale"]) == 0
    assert json.loads(capsys.readouterr().out) == {"skipped": "current"}


def test_the_cli_refuses_to_create_the_table_on_postgres(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Engine:
        class dialect:  # noqa: N801 -- the attribute shape sa.Engine has
            name = "postgresql"

    monkeypatch.setattr(sa, "inspect", lambda _engine: type("I", (), {"has_table": lambda self, _n: False})())
    with pytest.raises(SystemExit, match="alembic upgrade head"):
        job._ensure_table(_Engine())  # type: ignore[arg-type]
