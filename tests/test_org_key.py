"""The organisation key: `pipeline.normalize.norm_org` / `org_key` (docs/22 §16).

`norm_org` is the deterministic key every organisation consumer uses -- the resolver
(`services/resolve/merge.py::resolve_organizations`), the ownership loader, the midstream
operator/parent edges and the proposal loader. Until 2026-09-19 it stripped industry and
geography words as well as legal forms, which merged separate legal entities into one company
page. These tests pin both directions of that correction on the exact pairs it was measured on,
and fail if the legal-form list ever grows an industry word again.

The labelled corpus behind the numbers quoted here is `data/eval/organization_pairs.csv`.
"""

from __future__ import annotations

import collections
import csv
import pathlib

import pytest

from pipeline.normalize import (
    CORP_SUFFIXES,
    LEGAL_FORM_TOKENS,
    ORG_PLURALS,
    RESERVED_CONTENT_WORDS,
    norm_org,
    org_key,
)

PAIRS_CSV = pathlib.Path(__file__).resolve().parents[1] / "data" / "eval" / "organization_pairs.csv"

#: The seven over-merges `docs/22` §15.4 measured on the EIA-860 Schedule 4 owner strings. Each is
#: two separate legal entities -- a utility and its affiliate, or two unrelated projects sharing a
#: first word -- that the old key reduced to one company page.
MEASURED_OVER_MERGES = [
    ("MidAmerican Energy Co", "MidAmerican Solar LLC"),
    ("Entergy Corp", "Entergy Power, LLC"),
    ("Prairie Power Inc", "Prairie Solar LLC"),
    ("Shell Renewables", "Shell Wind Energy Inc."),
    ("BP America Inc", "BP Wind Energy North America Inc"),
    ("SunRay Power LLC", "Sunray Energy Inc"),
    ("Anderson Wind Project, LLC", "Anderson North Solar Project, LLC"),
]

#: Spellings of one legal entity that the key must keep merging. The first four are the pairs
#: `services/ingest/ownership.py`'s docstring cites as what the key is for; `Vistra Corp` /
#: `Vistra Energy` is deliberately NOT here -- see `test_vistra_is_an_alias_not_a_key_merge`.
MEASURED_SAME_COMPANY = [
    ("Wellhead Services, Inc.", "Wellhead Services, Inc"),
    ("Nextera Energy Resources", "NextEra Energy Resources, LLC"),
    ("Clearway Energy Group", "Clearway Energy Group LLC"),
    ("NEXTERA ENERGY RESOURCES LLC", "NextEra Energy Resources, LLC"),
    # punctuation, case, spacing, doubled legal forms
    ("Astoria Generating Co.", "Astoria Generating Company, L.P."),
    ("ACE DevCo NC", "ACE DevCo NC,LLC"),
    ("Thunder Creek Gas Services, L.L.C.", "Thunder Creek Gas Services,L.L.C."),
    # "&" against "and" (the split `data/vendored/organizations/aliases.yaml` was seeded for)
    ("Wisconsin Power & Light Co", "Wisconsin Power and Light Co"),
    ("Madison Gas & Electric Co", "Madison Gas & Electric Company"),
    # business-vocabulary plurals
    ("EDF Renewable Development Inc.", "EDF Renewables Development"),
    (
        "NextEra Energy Resources Interconnection Holding, LLC",
        "NextEra Energy Resources Interconnection Holdings, LLC",
    ),
    ("Valero Renewable Fuels", "Valero Renewables Fuels LLC"),
    ("Farm Credit Leasing Service Corp", "Farm Credit Leasing Services Corporation"),
]


@pytest.mark.parametrize(("left", "right"), MEASURED_OVER_MERGES)
def test_measured_over_merges_now_split(left: str, right: str) -> None:
    assert norm_org(left) != norm_org(right), f"{left!r} and {right!r} are different legal entities"


@pytest.mark.parametrize(("left", "right"), MEASURED_SAME_COMPANY)
def test_measured_same_company_pairs_still_merge(left: str, right: str) -> None:
    assert norm_org(left) == norm_org(right) is not None


def test_industry_and_geography_words_survive_the_key() -> None:
    """The regression that put another company's assets on a company page: every one of these was
    stripped, so the distinguishing word vanished from the key."""
    assert norm_org("Shell Energy North America") == "SHELL ENERGY NORTH AMERICA"
    assert norm_org("Invenergy Wind Development LLC") == "INVENERGY WIND DEVELOPMENT"
    assert norm_org("US Solar") == "US SOLAR"
    assert norm_org("Atlantic Power Corporation") != norm_org("Atlantic Wind, LLC")


def test_legal_form_list_holds_no_industry_or_geography_word() -> None:
    """The guard. Adding "energy", "power", "solar", "holdings", "usa"... to the legal-form list
    re-creates the 2026-09-19 over-merge; this fails the moment one appears."""
    offenders = sorted(set(LEGAL_FORM_TOKENS) & RESERVED_CONTENT_WORDS)
    assert offenders == [], f"industry/geography words in LEGAL_FORM_TOKENS: {offenders}"
    matched = sorted(w for w in RESERVED_CONTENT_WORDS if CORP_SUFFIXES.fullmatch(w))
    assert matched == [], f"CORP_SUFFIXES strips content words: {matched}"
    # a legal form is a legal form, not a content word, so each one alone leaves nothing behind
    for token in LEGAL_FORM_TOKENS:
        assert norm_org(token) is None, f"{token!r} is not a bare legal form"


def test_plural_fold_is_a_closed_list_not_a_trailing_s_rule() -> None:
    """A general "drop a trailing s" rule destroys Texas, Kansas, Illinois, Dallas, Hess."""
    for name in ("Texas", "Kansas", "Illinois", "Dallas", "Hess", "Atlas"):
        assert norm_org(f"{name} Energy LLC") == f"{name.upper()} ENERGY"
    for plural, singular in ORG_PLURALS.items():
        assert norm_org(f"Acme {plural}") == norm_org(f"Acme {singular}")


def test_co_op_keeps_its_co() -> None:
    """`\\bco\\b` would otherwise eat the "co" of a hyphenated co-op."""
    assert norm_org("Siouxland Energy & Livestock Co-Op") == norm_org(
        "Siouxland Energy and Livestock Cooperative"
    )
    assert "OP" not in (norm_org("Basin Electric Power Co-op") or "").split()


def test_org_key_is_total_where_norm_org_is_not() -> None:
    assert norm_org("LLC") is None
    assert org_key("LLC") == "LLC"
    assert org_key(None) == ""
    assert org_key(" NextEra Energy Resources, LLC ") == norm_org("NextEra Energy Resources, LLC")


def test_one_function_for_every_consumer() -> None:
    """The index, the lookup and the group keys must all be the same callable (docs/22 §15.4:
    two spellings of one key re-created an organisation and 20 edges on every re-run)."""
    from services.ingest import midstream, ownership
    from services.resolve import report

    assert ownership.org_key is org_key
    assert midstream.org_key is org_key
    assert report.org_key is org_key


#: Same-company spellings no legal-form key can reach, because the difference is a content word
#: or a rename. Each is a citable filing, and each belongs in `organization_alias` (docs/22 §16.5).
#: Pinned here so a future "just loosen the key a bit" is measured against them instead.
ALIAS_NOT_KEY = [
    ("Vistra Corp", "Vistra Energy"),
    ("Enable Midstream", "Enable Midstream Partners"),
    ("Noble Environmental", "Noble Environmental Power, LLC"),
    ("Dominion Energy Transmission, Inc.", "Dominion Transmission Co"),
]


@pytest.mark.parametrize(("left", "right"), ALIAS_NOT_KEY)
def test_rename_and_short_form_pairs_are_alias_work_not_key_work(left: str, right: str) -> None:
    """A key loose enough to reach these also merges MidAmerican Energy with MidAmerican Solar;
    the measured trade is 7 pairs like these against 258 wrong merges (docs/22 §16.3)."""
    assert norm_org(left) != norm_org(right)


def test_labelled_pair_set_reproduces_the_reported_rates() -> None:
    """`data/eval/organization_pairs.csv`, hand-labelled 2026-09-19 (docs/22 §16). Guards the two
    numbers the decision rests on: the corrected key merges no pair labelled `different`, and it
    keeps merging all but 7 of the 299 pairs labelled `same`."""
    rows = list(csv.DictReader(PAIRS_CSV.open()))
    census = [r for r in rows if r["bucket"] == "merged_by_old_key"]
    counts = collections.Counter(r["label"] for r in census)
    assert counts == {"same": 299, "family": 137, "different": 121}, counts

    def merges(row: dict[str, str]) -> bool:
        return norm_org(row["left_name"]) == norm_org(row["right_name"])

    still = collections.Counter(r["label"] for r in census if merges(r))
    assert still["different"] == 0, "the corrected key still merges two different companies"
    assert still["same"] == 292, still["same"]
    assert still["family"] == 13, still["family"]

    sample = [r for r in rows if r["bucket"] == "split_by_old_key_similarity_sample"]
    assert len(sample) == 200
    assert collections.Counter(r["label"] for r in sample)["same"] == 3
    # the corrected key recovers 2 of the 3 over-splits in the sample, and adds none
    assert sum(1 for r in sample if r["label"] == "same" and merges(r)) == 2
    assert sum(1 for r in sample if r["label"] == "different" and merges(r)) == 0
