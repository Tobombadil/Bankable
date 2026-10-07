"""The natural-person rule (`services/personal_names.py`; docs/13 §5.5; legal audit L-5).

Every name here is invented: given names from the Census list joined to surnames that are not
names, or companies that do not exist. Shapes are taken from what the registers print (EIA-860
owners, GHGRP parents, NYISO interconnection customers), never the rows themselves.
"""

from __future__ import annotations

import pytest

from services import personal_names
from services.personal_names import classify, name_digest, normalise


@pytest.mark.parametrize(
    "name",
    [
        "Margaret Quillfeather",  # title case, two tokens
        "WALTER QUILLFEATHER JR",  # GHGRP's upper case with a suffix
        "Alan Z. Quillfeather",  # middle initial
        "Quillfeather, Alan Z.",  # Last, First Middle
        "Dorothy van Quillfeather",  # lower-case particle
        "Dorothy Van Der Quillfeather",  # capitalised particles
        "Mary-Ann O'Quillfeather",  # hyphen and apostrophe
        "J. Walter Quillfeather",  # leading initial, given name second
        "Thomas Quillfeather III",
        "Harold B Quillfeather Revocable Living Trust",  # a grantor trust names its grantor
        "BETTY QUILLFEATHER FAMILY TRUST",
    ],
)
def test_person_shaped_names_are_flagged(name: str) -> None:
    result = classify(name)
    assert result.personal, name
    assert result.basis == "heuristic"


@pytest.mark.parametrize(
    ("name", "basis"),
    [
        ("Quillfeather Solar, LLC", "structure"),
        ("Margaret Solar 3", "structure"),  # a project named after a given name
        ("Cherry Valley", "structure"),  # a given name that is also a place word
        ("Will County, IL", "structure"),
        ("Hope Creek Storage", "structure"),
        ("Harmony Quillfeather Partners LP", "structure"),
        ("Dorothy Quillfeather Brothers", "structure"),
        ("CONRAD (QUILLFEATHER) LIMITED", "structure"),
        ("AL Quillfeather", "no_person_shape"),  # an acronym in a mixed-case name
        ("Quillfeather", "no_person_shape"),  # one token is never a person here
        ("Zyxwv Quillfeather", "no_person_shape"),  # first token not a given name
        ("Quillfeather Revocable Trust", "structure"),  # a surname alone before "Trust"
        ("", "no_person_shape"),
    ],
)
def test_organisation_shaped_names_are_not_flagged(name: str, basis: str) -> None:
    result = classify(name)
    assert not result.personal, name
    assert result.basis == basis


def test_the_curated_file_overrides_the_heuristic_both_ways(monkeypatch: pytest.MonkeyPatch) -> None:
    """A company the heuristic flags is listed by name; a person it misses is listed by digest only,
    so the repository never holds a plain list of people's names."""
    curated = personal_names.Overrides(
        not_personal=frozenset({normalise("Margaret Quillfeather")}),
        personal_sha256=frozenset({name_digest("Zyxwv Quillfeather")}),
    )
    monkeypatch.setattr(personal_names, "overrides", lambda: curated)
    assert classify("MARGARET  QUILLFEATHER.") == personal_names.Classification(False, "curated_not_personal")
    assert classify("zyxwv quillfeather") == personal_names.Classification(True, "curated_personal")


def test_the_shipped_curated_file_holds_no_plain_person_names() -> None:
    """`personal_sha256` holds 64-hex digests only; nothing else in that list."""
    for digest in personal_names.overrides().personal_sha256:
        assert len(digest) == 64 and all(c in "0123456789abcdef" for c in digest)


def test_given_names_come_from_the_vendored_census_list() -> None:
    names = personal_names.given_names()
    assert {"MARGARET", "WALTER", "DOROTHY"} <= names
    assert len(names) > 5000
