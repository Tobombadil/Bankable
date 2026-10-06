"""Store plumbing for `web/test_alerts_pages.py`: an in-memory database, a proposal to view, and
the counter rows to read back. Kept under `tests/` so the web test module imports nothing from
`services.db` itself (`infra/importlinter.ini`, "web must not import services.db directly")."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from services.api.conftest import make_open_licence, make_public_source, make_visible_proposal
from services.db.models import UiEvent
from services.db.session import get_engine, get_sessionmaker, init_db


def memory_sessionmaker() -> sessionmaker[Session]:
    engine = get_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    return get_sessionmaker(engine)


def seed_proposal(factory: sessionmaker[Session]) -> str:
    """One public proposal; returns its slug."""
    with factory() as session:
        source = make_public_source(session, make_open_licence(session))
        proposal = make_visible_proposal(session, source)
        slug = proposal.slug
        session.commit()
    return slug


def ui_event_props(factory: sessionmaker[Session], name: str) -> list[dict[str, object]]:
    with factory() as session:
        return [dict(row.props) for row in session.scalars(select(UiEvent).where(UiEvent.name == name))]


def init_empty_database(database_url: str) -> None:
    """An empty store at `database_url` for `web/test_e2e_alerts.py`'s server subprocess."""
    init_db(get_engine(database_url))
