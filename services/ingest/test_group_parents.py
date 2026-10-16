"""The curated developer and utility groups (`data/vendored/organizations/developer_parents.yaml`,
`services/ingest/organizations.py::load_group_parents`, docs/22 §24).

Held here, on the real file: no edge without provenance; the precision guard (every child's name
carries one of its parent's own names, or the edge quotes the Exhibit 21 row that names it); no child
claimed twice, by this file or by `parents.yaml`. And on a fixture store: loading links what the file
says and nothing else, a re-run changes nothing, a group whose children are not loaded leaves no
empty parent behind, and a GLEIF Level 2 link is never overwritten. That a parent's page then lists
its subsidiaries' proposals is `tests/test_group_parents_api.py` (the API) and
`web/test_org_pipeline.py` (the page).
"""

from __future__ import annotations

import datetime as dt
import pathlib

import pandas as pd
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from pipeline.normalize import org_key
from services.db.models import Organization
from services.db.session import get_engine, get_sessionmaker, init_db
from services.ids import public_id, slugify
from services.ingest.midstream import DEFAULT_PARENTS_PATH, read_parent_rules
from services.ingest.organizations import (
    DEFAULT_GROUP_PARENTS_PATH,
    GLEIF_SOURCE_ID,
    GROUP_EVIDENCE_KINDS,
    PARENTS_SOURCE_ID,
    SEC_ARCHIVES_PREFIX,
    GroupChild,
    GroupRule,
    group_edge_problems,
    load_gleif_parents,
    load_group_parents,
    name_carries_token,
    read_group_rules,
)

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

EX21 = "https://www.sec.gov/Archives/edgar/data/37634/000075330826000015/nee-q42025xex21.htm"


@pytest.fixture()
def session() -> Session:
    engine = get_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    with get_sessionmaker(engine)() as s:
        yield s


def _org(session: Session, name: str) -> Organization:
    org = Organization(
        public_id="", slug="", name_canonical=name, name_normalised=name.lower(), type="other", country="US"
    )
    session.add(org)
    session.flush()
    org.public_id = public_id("org", org.id)
    org.slug = f"{slugify(name)}-{org.public_id[-6:].lower()}"
    session.flush()
    return org


def _write(tmp_path: pathlib.Path, text: str) -> pathlib.Path:
    path = tmp_path / "developer_parents.yaml"
    path.write_text(text, encoding="utf-8")
    return path


GROUPS_YAML = f"""
groups:
  - parent: NextEra Energy, Inc.
    parent_country: US
    parent_names: [NextEra]
    children:
      - name: NextEra Energy Resources - ERCOT
        evidence: sec_ex21
        source_url: {EX21}
        retrieved_at: '2026-10-15T22:09:03Z'
      - name: Florida Power & Light Co
        evidence: sec_ex21
        source_url: {EX21}
        retrieved_at: '2026-10-15T22:09:03Z'
        quote: 1. Florida Power & Light Company (100%-owned) Florida
  - parent: SSE plc
    parent_country: GB
    parent_names: [SSE]
    children:
      - name: SSE COTTERED SOLAR LIMITED
        evidence: company_site
        source_url: https://www.sse.com/media/gugb5kyl/sse_ar26_interactive_18mb.pdf
        retrieved_at: '2026-10-15T22:20:27Z'
        quote: SSE Cottered Solar Limited B 100.0
  - parent: Nobody Loaded Holdings
    parent_country: GB
    parent_names: [Nobody Loaded]
    children:
      - name: Nobody Loaded Solar Limited
        evidence: company_site
        source_url: https://example.org/nobody
        retrieved_at: '2026-10-15T22:20:27Z'
"""


# --------------------------------------------------------------------------- the real file
def test_the_repo_file_parses_and_names_at_least_the_groups_the_beta_needs() -> None:
    rules = read_group_rules(DEFAULT_GROUP_PARENTS_PATH)
    parents = {rule.parent for rule in rules}
    assert "NextEra Energy, Inc." in parents  # the page that started this (docs/65 §(a) Task 1)
    assert len(rules) >= 20
    assert sum(len(rule.children) for rule in rules) >= 100


def test_no_edge_exists_without_provenance() -> None:
    """Every edge names its kind of evidence, cites an https document and when it was read; a GLEIF
    edge names the child's LEI and an Exhibit 21 edge cites EDGAR. The source and licence come from
    `curated.organization_parents` in data/sources.yaml, the same pair `parents.yaml` writes under."""
    for rule in read_group_rules(DEFAULT_GROUP_PARENTS_PATH):
        assert len(rule.parent_country) == 2 and rule.parent_names
        for child in rule.children:
            label = f"{child.name} -> {rule.parent}"
            assert child.evidence in GROUP_EVIDENCE_KINDS, label
            assert child.source_url.startswith("https://"), label
            retrieved = pd.Timestamp(child.retrieved_at)
            assert retrieved.tzinfo is not None and retrieved.year >= 2026, label
            if child.evidence == "gleif":
                assert len(child.lei) == 20 and child.source_url.startswith("https://api.gleif.org/"), label
            if child.evidence == "sec_ex21":
                assert child.source_url.startswith(SEC_ARCHIVES_PREFIX), label


def test_precision_guard_every_child_carries_a_parent_name_or_quotes_its_exhibit_21_row() -> None:
    """The guard, stated independently of `group_edge_problems` so a change to one is caught by the
    other: a child's name carries one of the parent's own names as whole words, or the edge quotes the
    Exhibit 21 row (on EDGAR) in which the parent names it. Nothing else -- not a GLEIF record alone, not
    a company page alone -- lets a name without the parent's in it through."""
    for rule in read_group_rules(DEFAULT_GROUP_PARENTS_PATH):
        for child in rule.children:
            named = name_carries_token(child.name, rule.parent_names)
            quoted = (
                child.evidence == "sec_ex21"
                and child.source_url.startswith(SEC_ARCHIVES_PREFIX)
                and bool(child.quote)
            )
            assert named or quoted, f"{child.name} -> {rule.parent}"
            assert group_edge_problems(rule, child) == []


def test_no_child_is_claimed_twice() -> None:
    """One parent per organisation: no child under two groups, none that is its own group's parent,
    and none that a `parents.yaml` pattern also claims (the two files write one column)."""
    seen: dict[str, str] = {}
    pattern_rules = read_parent_rules(DEFAULT_PARENTS_PATH)
    for rule in read_group_rules(DEFAULT_GROUP_PARENTS_PATH):
        for child in rule.children:
            key = org_key(child.name)
            assert key != org_key(rule.parent), child.name
            assert key not in seen, f"{child.name}: under {seen.get(key)} and {rule.parent}"
            seen[key] = rule.parent
            assert not [r.child_pattern for r in pattern_rules if r.regex.search(child.name)], child.name


def test_token_matching_is_by_whole_word() -> None:
    assert name_carries_token("NextEra Energy Resources - ERCOT", ("NextEra",))
    assert name_carries_token("SSE COTTERED SOLAR LIMITED", ("SSE",))
    assert name_carries_token("Mississippi Power / Southern Company", ("Southern Company",))
    assert not name_carries_token("DENGIE MARSHES WIND FARM LIMITED", ("ENGIE",))
    assert not name_carries_token("Old Dominion Electric Coop", ("Dominion Energy",))
    assert not name_carries_token("Excelsior Energy Center, LLC", ("Xcel",))


@pytest.mark.parametrize(
    ("child", "problem"),
    [
        # A project company whose name carries nothing of the parent's, with no filing naming it.
        (
            GroupChild("Flat Hill Solar LLC", "company_site", "https://example.org", "2026-10-15"),
            "carries none",
        ),
        # A quoted row is accepted only from an Exhibit 21 on EDGAR.
        (
            GroupChild("Flat Hill Solar LLC", "company_site", "https://example.org", "2026-10-15", quote="x"),
            "carries none",
        ),
        (
            GroupChild(
                "Flat Hill Solar LLC", "sec_ex21", "https://example.org/ex21.htm", "2026-10-15", quote="x"
            ),
            "sec_ex21 edge cites",
        ),
        (GroupChild("NextEra Energy Resources, LLC", "rumour", EX21, "2026-10-15"), "evidence"),
        (
            GroupChild("NextEra Energy Resources, LLC", "gleif", "https://api.gleif.org/x", "2026-10-15"),
            "LEI",
        ),
        (GroupChild("NextEra Energy Resources, LLC", "sec_ex21", EX21, "last Tuesday"), "retrieved_at"),
    ],
)
def test_the_guard_refuses(child: GroupChild, problem: str) -> None:
    rule = GroupRule("NextEra Energy, Inc.", "US", ("NextEra",), (child,))
    assert any(problem in p for p in group_edge_problems(rule, child))


def test_a_file_with_one_bad_edge_does_not_load_at_all(session: Session, tmp_path: pathlib.Path) -> None:
    bad = GROUPS_YAML + (
        "      - name: Flat Hill Solar LLC\n"
        "        evidence: company_site\n"
        "        source_url: https://example.org/flat-hill\n"
        "        retrieved_at: '2026-10-15T22:20:27Z'\n"
    )
    _org(session, "Flat Hill Solar LLC")
    _org(session, "SSE COTTERED SOLAR LIMITED")
    with pytest.raises(ValueError, match="Flat Hill Solar LLC"):
        load_group_parents(session, _write(tmp_path, bad))
    assert not [o for o in session.scalars(select(Organization)) if o.parent_org_id is not None]


# ------------------------------------------------------------------------------ the loader
def test_loading_links_the_children_reuses_the_parent_and_is_idempotent(
    session: Session, tmp_path: pathlib.Path
) -> None:
    nee = _org(session, "NEXTERA ENERGY INC")  # the EIA owner spelling: org_key reuses it
    ercot = _org(session, "NextEra Energy Resources - ERCOT")
    fpl = _org(session, "FLORIDA POWER & LIGHT CO")  # another spelling of the file's name
    cottered = _org(session, "SSE COTTERED SOLAR LIMITED")
    unrelated = _org(session, "NextEra Energy Partners LP")  # carries the name, not in the file
    session.commit()

    path = _write(tmp_path, GROUPS_YAML)
    first = load_group_parents(session, path)
    assert (first.groups, first.edges) == (3, 4)
    assert first.children_linked == 3 and first.children_unchanged == 0
    assert first.parents_matched == 1 and first.parents_created == 1  # NextEra reused; SSE plc created
    assert first.groups_without_children == ["Nobody Loaded Holdings"]
    assert first.children_not_loaded == ["Nobody Loaded Solar Limited -> Nobody Loaded Holdings"]

    for child in (ercot, fpl):
        session.refresh(child)
        assert child.parent_org_id == nee.id
        assert child.parent_source_id == PARENTS_SOURCE_ID
        assert child.parent_as_of is None and child.parent_share_pct is None
    session.refresh(unrelated)
    assert unrelated.parent_org_id is None  # a shared name is not a link

    sse = session.scalars(select(Organization).where(Organization.name_canonical == "SSE plc")).one()
    assert sse.country == "GB"  # the file's country, not the US every other loader writes
    session.refresh(cottered)
    assert cottered.parent_org_id == sse.id
    assert not session.scalars(
        select(Organization).where(Organization.name_canonical == "Nobody Loaded Holdings")
    ).all()

    count = len(session.scalars(select(Organization)).all())
    again = load_group_parents(session, path)
    assert again.children_linked == 0 and again.children_unchanged == 3
    assert again.parents_created == 0 and again.parents_matched == 2
    assert len(session.scalars(select(Organization)).all()) == count


def test_a_gleif_link_is_never_overwritten(session: Session, tmp_path: pathlib.Path) -> None:
    """GLEIF Level 2 is a registry record and wins wherever it has one, as it does over
    `parents.yaml` (`children_deferred_to_gleif`); the two loaders are then order-independent."""
    load_gleif_parents(session, pd.DataFrame())  # writes the GLEIF source row the link points at
    _org(session, "NEXTERA ENERGY INC")
    ercot = _org(session, "NextEra Energy Resources - ERCOT")
    holding = _org(session, "NextEra Energy Capital Holdings, Inc.")
    ercot.parent_org_id = holding.id
    ercot.parent_source_id = GLEIF_SOURCE_ID
    ercot.parent_as_of = dt.date(2020, 1, 1)
    session.commit()

    result = load_group_parents(session, _write(tmp_path, GROUPS_YAML))
    assert result.children_deferred_to_gleif == 1
    session.refresh(ercot)
    assert ercot.parent_org_id == holding.id and ercot.parent_source_id == GLEIF_SOURCE_ID
