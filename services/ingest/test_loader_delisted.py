"""A record that leaves a full-register queue is announced as `delisted` (owner decision 2026-10-10).

ERCOT, CAISO, NYISO and NESO declare `Connector.announce_removals`, a publication choice separate
from what a removal means (`removal_meaning` stays `unknown`). The loader writes their removals as
the public, alertable `delisted` event, worded "No longer in <register>'s report (reason not
stated)", never "withdrawn". EIA-860M and every other source keep the non-public
`removed_from_source` (`test_loader_removals.py`). `link.gone_at` and the lifecycle state behave as
before. The churn gate (`pipeline/connectors/dq.py`) holds a mass re-key before any event is
written, so a re-keyed register never announces its whole queue as gone. A single re-key is held back
by the project check: a removed key whose project (`Connector.project_root`) is still in the frame
under another key is a `removed_from_source` saying so (NESO's unstaged rows becoming stages, a NYISO
queue position's content suffix shifting), never a public departure.

The surfaces the event reaches (event list, detail, history, feeds, alerts, webhooks) and the one it
never reaches (social drafts) are walked in `tests/test_delisted_is_public_and_never_drafted.py`.
"""

from __future__ import annotations

import datetime as dt
import pathlib
from collections.abc import Iterator
from typing import Any, ClassVar

import pandas as pd
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from conftest import connector_for, snapshot
from pipeline.connectors.base import REMOVAL_MEANINGS, Connector, RawSnapshot
from pipeline.connectors.registry import RegistrationError, Registry
from pipeline.connectors.runner import run
from pipeline.connectors.store import Store
from pipeline.diff import diff_snapshots
from services.db.models import (
    DELISTED_EVENT_TYPE,
    DELISTED_REASON,
    DELISTED_WORDING,
    NON_PUBLIC_EVENT_TYPES,
    NON_SOCIAL_EVENT_TYPES,
    REMOVED_FROM_SOURCE_EVENT_TYPE,
    Event,
    Proposal,
    ProposalSource,
)
from services.db.session import get_engine, get_sessionmaker, init_db
from services.ingest import loader
from services.ingest.loader import (
    connector_announces_removals,
    connector_project_root,
    connector_register_name,
    connector_removal_meaning,
    delisted_sentence,
    load_dataframe,
    removal_event_type,
    upsert_licence_and_source,
)
from services.ingest.test_loader import open_source_entry, sample_proposal_row

#: The four full-register queues the owner named, with the display name each event uses (the short
#: name record pages print for the source, `web/viewmodels.py::PROPOSAL_SOURCE_LABELS`).
ANNOUNCED = {
    "us.iso.ercot.gen_queue": "ERCOT",
    "us.iso.caiso.gen_queue": "CAISO",
    "us.iso.nyiso.gen_queue": "NYISO",
    "gb.neso.tec_register": "NESO",
}
EIA_860M = "us.eia.860m"
GONE_AT = "2026-10-09T06:00:00Z"


@pytest.fixture()
def session() -> Iterator[Session]:
    engine = get_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    with get_sessionmaker(engine)() as s:
        yield s


def _row(source_id: str, record_id: str, lifecycle_state: str = "filed") -> dict[str, Any]:
    r = dict(sample_proposal_row(record_id, lifecycle_state=lifecycle_state))
    r.update(
        record_id=f"{source_id}:{record_id}",
        source_id=source_id,
        source_url=f"https://example.org/{source_id}/{record_id}",
        name_canonical=f"Project {record_id}",
        name_norm=f"project {record_id.lower()}",
    )
    return r


def _removed(source_id: str, record_id: str, before: str) -> dict[str, Any]:
    return {
        "event_type": "removed",
        "record_id": f"{source_id}:{record_id}",
        "source_id": source_id,
        "field": "lifecycle_state",
        "before": before,
        "after": None,
        "observed_at": GONE_AT,
    }


def _load_with_declarations(
    session: Session, registry: Registry, source_id: str
) -> tuple[loader.LoadResult, Proposal, ProposalSource]:
    """Q1 and Q2 appear, then Q2 leaves the file; both loads read the connector's own declarations,
    as `load_from_files` does."""
    src = upsert_licence_and_source(session, registry.get(source_id), registry.version)
    declared: dict[str, Any] = {
        "removal_meaning": connector_removal_meaning(registry, source_id),
        "announce_removals": connector_announces_removals(registry, source_id),
        "register_name": connector_register_name(registry, source_id),
    }
    q1, q2 = _row(source_id, "Q1"), _row(source_id, "Q2", "studied")
    load_dataframe(session, src, "proposal", pd.DataFrame([q1, q2]), None, **declared)
    result = load_dataframe(
        session,
        src,
        "proposal",
        pd.DataFrame([q1]),
        pd.DataFrame([_removed(source_id, "Q2", "studied")]),
        **declared,
    )
    session.flush()
    link = session.scalar(select(ProposalSource).where(ProposalSource.source_record_id == "Q2"))
    assert link is not None
    proposal = session.get(Proposal, link.proposal_id)
    assert proposal is not None
    return result, proposal, link


def _aware(value: dt.datetime | None) -> dt.datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=dt.UTC)


# ------------------------------------------------------------------------------- the event written
@pytest.mark.parametrize(("source_id", "register"), sorted(ANNOUNCED.items()))
def test_each_queue_removal_is_the_public_delisted_event(
    session: Session, registry: Registry, source_id: str, register: str
) -> None:
    result, proposal, link = _load_with_declarations(session, registry, source_id)

    event = session.scalars(select(Event)).one()
    assert event.event_type == DELISTED_EVENT_TYPE == "delisted"
    assert event.event_type not in NON_PUBLIC_EVENT_TYPES
    # Published like every public change event: `public_at` is `published_at` (services/ingest/lag.py).
    assert event.published_at is not None and _aware(event.public_at) == _aware(event.published_at)
    assert event.reason == f"No longer in {register}'s report (reason not stated)"
    assert event.after == {"source_id": source_id, "register_name": register, "reason": "not stated"}
    assert event.before is None and event.changed_keys == []
    assert "withdr" not in (event.reason or "").lower() and "withdr" not in str(event.after).lower()
    assert event.source_id == source_id and event.source_url and event.retrieved_at and event.licence_id
    assert (result.removals_announced, result.removals_unpublished) == (1, 0)
    # `gone_at` exactly as before, and the record keeps the state the source last stated.
    assert _aware(link.gone_at) == dt.datetime(2026, 10, 9, 6, tzinfo=dt.UTC)
    assert proposal.lifecycle_state == "studied"


def test_an_eia_860m_removal_stays_non_public(session: Session, registry: Registry) -> None:
    """A unit leaving EIA's Planned sheet may have started operating: no announcement."""
    result, proposal, link = _load_with_declarations(session, registry, EIA_860M)
    event = session.scalars(select(Event)).one()
    assert event.event_type == REMOVED_FROM_SOURCE_EVENT_TYPE
    assert event.published_at is None and event.public_at is None
    assert event.after == {"removal_meaning": "unknown"}
    assert (result.removals_announced, result.removals_unpublished) == (0, 1)
    assert link.gone_at is not None and proposal.lifecycle_state == "studied"
    assert not session.scalars(select(Event).where(Event.event_type == DELISTED_EVENT_TYPE)).all()


def test_reloading_the_same_departure_is_idempotent(session: Session, registry: Registry) -> None:
    source_id = "us.iso.ercot.gen_queue"
    _load_with_declarations(session, registry, source_id)
    src = upsert_licence_and_source(session, registry.get(source_id), registry.version)
    again = load_dataframe(
        session,
        src,
        "proposal",
        pd.DataFrame([_row(source_id, "Q1")]),
        pd.DataFrame([_removed(source_id, "Q2", "studied")]),
        announce_removals=True,
        register_name="ERCOT",
    )
    assert again.events_created == 0 and again.events_skipped_idempotent == 1
    assert len(session.scalars(select(Event)).all()) == 1


def test_the_wording_is_one_constant() -> None:
    assert DELISTED_WORDING == "No longer in {register}'s report (reason not stated)"
    assert delisted_sentence("NESO") == "No longer in NESO's report (reason not stated)"
    assert DELISTED_REASON == "not stated"
    assert DELISTED_EVENT_TYPE in NON_SOCIAL_EVENT_TYPES
    assert not NON_SOCIAL_EVENT_TYPES & NON_PUBLIC_EVENT_TYPES


def test_the_event_type_follows_the_meaning_first_then_the_announcement() -> None:
    expected = {
        ("withdrawn", False): "withdrawn",
        ("withdrawn", True): "withdrawn",
        ("unknown", False): REMOVED_FROM_SOURCE_EVENT_TYPE,
        ("unknown", True): DELISTED_EVENT_TYPE,
        # A source that states why a row left is never announced as "reason not stated".
        ("completed", True): REMOVED_FROM_SOURCE_EVENT_TYPE,
        ("closed", True): REMOVED_FROM_SOURCE_EVENT_TYPE,
        ("completed", False): REMOVED_FROM_SOURCE_EVENT_TYPE,
        ("closed", False): REMOVED_FROM_SOURCE_EVENT_TYPE,
    }
    assert {(m, a): removal_event_type(m, a) for m in REMOVAL_MEANINGS for a in (False, True)} == expected


def test_a_direct_caller_announces_nothing(session: Session, registry: Registry) -> None:
    """`load_dataframe` without the declaration (bench, dev-data and resolver callers) fails closed,
    even for a queue source."""
    source_id = "us.iso.ercot.gen_queue"
    src = upsert_licence_and_source(session, registry.get(source_id), registry.version)
    q1, q2 = _row(source_id, "Q1"), _row(source_id, "Q2")
    load_dataframe(session, src, "proposal", pd.DataFrame([q1, q2]), None)
    load_dataframe(
        session, src, "proposal", pd.DataFrame([q1]), pd.DataFrame([_removed(source_id, "Q2", "filed")])
    )
    assert session.scalars(select(Event)).one().event_type == REMOVED_FROM_SOURCE_EVENT_TYPE


def test_an_announcing_source_with_no_display_name_falls_back_to_its_operator(session: Session) -> None:
    src = upsert_licence_and_source(session, open_source_entry(), "v")
    sid = src.id
    q1, q2 = _row(sid, "Q1"), _row(sid, "Q2")
    load_dataframe(session, src, "proposal", pd.DataFrame([q1, q2]), None)
    load_dataframe(
        session,
        src,
        "proposal",
        pd.DataFrame([q1]),
        pd.DataFrame([_removed(sid, "Q2", "filed")]),
        announce_removals=True,
    )
    event = session.scalars(select(Event)).one()
    register = src.operator or src.name
    assert event.event_type == DELISTED_EVENT_TYPE
    assert event.after is not None and event.after["register_name"] == register
    assert event.reason == f"No longer in {register}'s report (reason not stated)"


# ---------------------------------------------------------------- the declaration and how it is read
def test_the_four_queues_are_the_only_connectors_that_announce_removals() -> None:
    registry = Registry()
    announcing: dict[str, str | None] = {}
    for source_id in registry.ids():
        try:
            cls = registry.connector_class(source_id)
        except RegistrationError:
            continue
        if cls.announce_removals:
            announcing[source_id] = cls.register_name
            # A publication choice, not a meaning: the meaning stays `unknown`.
            assert cls.removal_meaning == "unknown", source_id
    assert announcing == ANNOUNCED
    assert registry.connector_class(EIA_860M).announce_removals is False


def test_the_loader_reads_the_declarations_and_fails_closed() -> None:
    registry = Registry()
    for source_id, register in ANNOUNCED.items():
        assert connector_announces_removals(registry, source_id) is True
        assert connector_register_name(registry, source_id) == register
    assert connector_announces_removals(registry, EIA_860M) is False
    assert connector_announces_removals(registry, "us.grants_gov.search2") is False
    assert connector_announces_removals(registry, "us.test.not_in_the_manifest") is False
    assert connector_register_name(registry, "us.test.not_in_the_manifest") is None

    class Truthy:
        announce_removals: ClassVar[object] = "yes"  # only a literal True announces
        register_name: ClassVar[object] = "  "

    class _StubRegistry:
        def connector_class(self, source_id: str) -> type:
            return Truthy

    assert connector_announces_removals(_StubRegistry(), "s") is False  # type: ignore[arg-type]
    assert connector_register_name(_StubRegistry(), "s") is None  # type: ignore[arg-type]


# ------------------------------------------------------------- a re-key is not a departure
NESO = "gb.neso.tec_register"
NYISO = "us.iso.nyiso.gen_queue"
#: VPI Immingham: an unstaged built row plus an unstaged +50 MW increase on the 2026-09-11 register
#: (`pid#1`, `pid#2`), staged 2 and 3 on the 2026-10-10 one (`pid/2`, `pid/3`).
IMMINGHAM = "a0l4L0000005im7QAA"


def test_each_connector_names_the_project_a_key_belongs_to() -> None:
    registry = Registry()
    neso = connector_project_root(registry, NESO)
    assert [neso(k) for k in (IMMINGHAM, f"{IMMINGHAM}/2", f"{IMMINGHAM}#1")] == [IMMINGHAM] * 3
    nyiso = connector_project_root(registry, NYISO)
    assert nyiso("0031#hd80a1050a7") == nyiso("0031#h5ec2d84b20~2") == nyiso("0031#2") == "0031"
    assert nyiso("0225A") == "0225A", "a letter is part of the queue position, not a suffix"
    # No multi-key convention: the key itself (evidence on each connector's declaration).
    assert connector_project_root(registry, "us.iso.ercot.gen_queue")("15INR0064b") == "15INR0064b"
    assert connector_project_root(registry, "us.iso.caiso.gen_queue")("643A") == "643A"
    assert connector_project_root(registry, "us.test.not_in_the_manifest")("x#1") == "x#1"


def _neso_frame(fixture: str) -> pd.DataFrame:
    connector = connector_for(NESO)
    raw = snapshot(fixture, "https://api.neso.energy/dataset/tec/download/tec-register.csv")
    return connector.normalize(connector.parse(raw), raw)


def test_neso_rows_that_become_stages_are_not_announced(session: Session, registry: Registry) -> None:
    """The recorded September and October registers: Immingham's two unstaged keys disappear while
    its stages appear, so the project is still listed. The rows the October sample does not carry
    stand for real departures and are still announced."""
    september, october = _neso_frame("neso_tec_register.csv"), _neso_frame("neso_tec_register_2026-10.csv")
    events = diff_snapshots(september, october, observed_at=GONE_AT)
    removed = events[events["event_type"] == "removed"]
    assert {f"{NESO}:{IMMINGHAM}#1", f"{NESO}:{IMMINGHAM}#2"} <= set(removed["record_id"])
    src = upsert_licence_and_source(session, registry.get(NESO), registry.version)
    declared: dict[str, Any] = {
        "removal_meaning": connector_removal_meaning(registry, NESO),
        "announce_removals": connector_announces_removals(registry, NESO),
        "register_name": connector_register_name(registry, NESO),
        "project_root": connector_project_root(registry, NESO),
    }
    load_dataframe(session, src, "proposal", september, None, **declared)
    result = load_dataframe(session, src, "proposal", october, events, **declared)
    session.flush()

    rekeyed = session.scalars(select(Event).where(Event.event_type == REMOVED_FROM_SOURCE_EVENT_TYPE)).all()
    assert len(rekeyed) == 2 == result.removals_rekeyed == result.removals_unpublished
    for event in rekeyed:
        assert event.published_at is None and event.public_at is None
        assert event.after == {"removal_meaning": "unknown", "project_root": IMMINGHAM}
        assert event.reason is not None and event.reason.startswith("re-keyed within the register")
    delisted = session.scalars(select(Event).where(Event.event_type == DELISTED_EVENT_TYPE)).all()
    assert len(delisted) == len(removed) - 2 == result.removals_announced > 0
    links = session.scalars(
        select(ProposalSource).where(ProposalSource.source_record_id.like(f"{IMMINGHAM}#%"))
    ).all()
    assert len(links) == 2 and all(link.gone_at is not None for link in links), "gone_at as before"


def _nyiso_row(key: str, name: str, *, suffix: str | None = None) -> dict[str, Any]:
    r = _row(NYISO, key)
    r.update(name_canonical=name, name_norm=name.lower())
    if suffix is not None:
        r["record_id"] = f"{NYISO}:{key}#{suffix}"
    return r


def test_a_nyiso_suffix_shift_is_not_announced(session: Session, registry: Registry) -> None:
    """Queue position 0031 is listed twice (Astoria Energy phases 1 and 2), so both rows carry a
    content suffix. Phase 2 leaves: phase 1 is listed alone under the bare position, and the old
    phase-2 key is a re-key of a position that is still listed. 0600 leaving is a real departure."""
    src = upsert_licence_and_source(session, registry.get(NYISO), registry.version)
    declared: dict[str, Any] = {
        "removal_meaning": connector_removal_meaning(registry, NYISO),
        "announce_removals": connector_announces_removals(registry, NYISO),
        "register_name": connector_register_name(registry, NYISO),
        "project_root": connector_project_root(registry, NYISO),
    }
    phase1 = _nyiso_row("0031", "Astoria Energy - Phase 1", suffix="h5ec2d84b20")
    phase2 = _nyiso_row("0031", "Astoria Energy - Phase 2", suffix="hd80a1050a7")
    ball_hill, gone = _nyiso_row("0505", "Ball Hill Wind"), _nyiso_row("0600", "Leaving Solar")
    load_dataframe(
        session, src, "proposal", pd.DataFrame([phase1, phase2, ball_hill, gone]), None, **declared
    )
    removed = [
        {**_removed(NYISO, "0031#hd80a1050a7", "filed")},
        {**_removed(NYISO, "0600", "filed")},
    ]
    phase1_alone = _nyiso_row("0031", "Astoria Energy - Phase 1")
    result = load_dataframe(
        session, src, "proposal", pd.DataFrame([phase1_alone, ball_hill]), pd.DataFrame(removed), **declared
    )
    session.flush()
    links = {link.source_record_id: link for link in session.scalars(select(ProposalSource))}
    by_subject = {e.subject_id: e for e in session.scalars(select(Event))}
    assert len(by_subject) == 2
    rekeyed = by_subject[links["0031#hd80a1050a7"].proposal_id]
    assert rekeyed.event_type == REMOVED_FROM_SOURCE_EVENT_TYPE and rekeyed.published_at is None
    assert rekeyed.after == {"removal_meaning": "unknown", "project_root": "0031"}
    departed = by_subject[links["0600"].proposal_id]
    assert departed.event_type == DELISTED_EVENT_TYPE and departed.published_at is not None
    assert departed.reason == "No longer in NYISO's report (reason not stated)"
    assert (result.removals_rekeyed, result.removals_announced) == (1, 1)


# ------------------------------------------------------------------- through load_from_files
TS1, TS2 = "20261008T060000Z", "20261009T060000Z"


def _write_runs(root: pathlib.Path, source_id: str, fixture: str, url: str) -> str:
    """Two runs of `source_id` over a recorded fixture, the second without its last row, as
    `python -m pipeline.connectors run` writes them. Returns the removed record id."""
    connector = connector_for(source_id)
    raw = snapshot(fixture, url)
    frame = connector.normalize(connector.parse(raw), raw)
    gone = frame.iloc[-1]
    out = root / "normalized" / source_id
    out.mkdir(parents=True)
    frame.to_parquet(out / f"{TS1}.parquet", index=False)
    frame.iloc[:-1].to_parquet(out / f"{TS2}.parquet", index=False)
    events = root / "events" / source_id
    events.mkdir(parents=True)
    row = {
        "event_type": "removed",
        "record_id": str(gone["record_id"]),
        "source_id": source_id,
        "field": "lifecycle_state",
        "before": str(gone["lifecycle_state"]),
        "after": None,
        "observed_at": GONE_AT,
    }
    pd.DataFrame([row]).to_parquet(events / f"{TS2}.parquet", index=False)
    return str(gone["record_id"])


def test_load_from_files_announces_an_ercot_departure(tmp_path: pathlib.Path, session: Session) -> None:
    source_id = "us.iso.ercot.gen_queue"
    _write_runs(
        tmp_path,
        source_id,
        "ercot_gis_report.xlsx",
        "https://www.ercot.com/misdownload/servlets/mirDownload?doclookupId=1269363208",
    )
    registry = Registry()
    loader.load_from_files(session, source_id, TS1, data_root=tmp_path, registry=registry)
    result = loader.load_from_files(session, source_id, TS2, data_root=tmp_path, registry=registry)
    session.flush()
    event = session.scalars(select(Event)).one()
    assert event.event_type == DELISTED_EVENT_TYPE and event.published_at is not None
    assert event.reason == "No longer in ERCOT's report (reason not stated)"
    assert isinstance(result, loader.LoadResult) and result.removals_announced == 1


def test_load_from_files_keeps_an_eia_860m_departure_non_public(
    tmp_path: pathlib.Path, session: Session
) -> None:
    _write_runs(
        tmp_path,
        EIA_860M,
        "eia860m_planned.xlsx",
        "https://www.eia.gov/electricity/data/eia860m/xls/july_generator2026.xlsx",
    )
    registry = Registry()
    loader.load_from_files(session, EIA_860M, TS1, data_root=tmp_path, registry=registry)
    loader.load_from_files(session, EIA_860M, TS2, data_root=tmp_path, registry=registry)
    session.flush()
    event = session.scalars(select(Event)).one()
    assert event.event_type == REMOVED_FROM_SOURCE_EVENT_TYPE
    assert event.published_at is None and event.public_at is None


# ----------------------------------------------------------------- the churn gate holds it first
ERCOT = "us.iso.ercot.gen_queue"
T0 = dt.datetime(2026, 10, 2, 5, 21, tzinfo=dt.UTC)


class Keyed(Connector):
    """ERCOT's declarations on a register whose snapshot says how many rows it has and what their
    keys start with (`"<n>:<prefix>"`), so a re-key is one changed character."""

    kind: ClassVar[Any] = "proposal"
    ext: ClassVar[str] = "csv"
    announce_removals: ClassVar[bool] = True
    register_name: ClassVar[str | None] = "ERCOT"

    def fetch(self) -> RawSnapshot:  # pragma: no cover - every run here injects its snapshot
        raise AssertionError("no fetch in tests")

    def parse(self, raw: RawSnapshot) -> list[dict[str, Any]]:
        n, prefix = raw.content.decode().split(":")
        return [{"Queue ID": f"{prefix}{i}", "Status": "Active"} for i in range(int(n))]

    def normalize(self, rows: list[dict[str, Any]], raw: RawSnapshot) -> pd.DataFrame:
        df = pd.DataFrame(
            {
                "source_record_id": [r["Queue ID"] for r in rows],
                "lifecycle_state": "studied",
                "status_raw": "Active",
                "status_rule": "test.map",
                "capacity_mw": [float(i + 1) for i in range(len(rows))],
                "name_canonical": [f"Project {r['Queue ID']}" for r in rows],
                "technology_raw": "Solar",
                "state": "TX",
            }
        )
        return self.finalize(df, rows, raw)


@pytest.fixture()
def keyed(monkeypatch: pytest.MonkeyPatch) -> Registry:
    registry = Registry()

    def connector_class(source_id: str) -> type[Connector]:
        Keyed.source_id = source_id
        return Keyed

    monkeypatch.setattr(registry, "connector_class", connector_class)
    return registry


def _run(registry: Registry, store: Store, content: str, day: int) -> Any:
    raw = RawSnapshot(
        content=content.encode(),
        content_type="text/csv",
        url="https://example.invalid/gis.csv",
        retrieved_at=T0 + dt.timedelta(days=day),
        http_status=200,
        ext="csv",
    )
    return run(ERCOT, registry=registry, store=store, raw=raw)


def _ts(result: Any) -> str:
    return str(result.paths["run"].stem)


def test_a_mass_re_key_is_held_before_any_delisted_event_is_written(
    tmp_path: pathlib.Path, keyed: Registry, session: Session
) -> None:
    store = Store(tmp_path)
    first = _run(keyed, store, "120:INR", 0)
    assert first.status == "ok"
    loader.load_from_files(session, ERCOT, _ts(first), registry=keyed, store=store)
    session.flush()
    assert session.scalars(select(Event)).all() == []  # a first run emits no events

    # ERCOT renumbers every request: 120 rows "leave" and 120 "arrive". Held, with no events file.
    held = _run(keyed, store, "120:GIR", 1)
    assert held.status == "partial", held.run.get("hold_reasons")
    assert [r for r in held.run["hold_reasons"] if r.startswith("churn:")], held.run["hold_reasons"]
    assert "events" not in held.paths and "normalized" not in held.paths
    with pytest.raises(FileNotFoundError):  # nothing to load: the scheduler loads promoted runs only
        loader.load_from_files(session, ERCOT, _ts(held), registry=keyed, store=store)
    assert session.scalars(select(Event).where(Event.event_type == DELISTED_EVENT_TYPE)).all() == []

    # The next ordinary release, two requests gone, passes the gate against the last promoted run
    # (not the held one) and announces exactly those two.
    third = _run(keyed, store, "118:INR", 2)
    assert third.status == "ok", third.run.get("hold_reasons")
    result = loader.load_from_files(session, ERCOT, _ts(third), registry=keyed, store=store)
    session.flush()
    delisted = session.scalars(select(Event).where(Event.event_type == DELISTED_EVENT_TYPE)).all()
    assert len(delisted) == 2 and isinstance(result, loader.LoadResult) and result.removals_announced == 2
    assert {e.reason for e in delisted} == {"No longer in ERCOT's report (reason not stated)"}
    assert all(e.published_at is not None for e in delisted)
