"""Is this organisation row a natural person? (2026-09-30 legal audit L-5; `docs/13` §5.5.)

Registers name people as owners and sponsors in the column where they name companies: an EIA-860
owner, a GHGRP parent with a percentage, an interconnection customer in a queue file. Every such
row becomes an `organization` with its own page, so a person's name, ownership share and, in one
measured case, a street address were published and indexed as if they were a company's. This
module decides, per name, whether the row is probably a natural person, so the store can carry
`organization.personal_data` and the public surfaces can treat those rows conservatively (no
sitemap entry, `noindex`, no ownership share, no street address beside the name).

It is a **heuristic plus a curated file**, not a model, and it is tuned towards flagging: a
company wrongly flagged loses a sitemap entry and a share figure, a person wrongly left unflagged
keeps an indexed page. The measured false-positive and false-negative samples are in `docs/13`
§5.5.

The rule, in order (the first that decides wins):

1. **Curated file** (`data/vendored/organizations/personal_data_overrides.yaml`). `not_personal`
   lists names (plain text: they are companies) that the heuristic flags wrongly; `personal_sha256`
   lists the SHA-256 of the normalised name of people the heuristic misses, so the repository does
   not hold a plain list of people's names. The digest is pseudonymisation, not anonymisation: a
   guessed name can be hashed and compared. `python -m services.personal_names hash "<name>"`
   prints the line to add.
2. **Organisational structure**: a digit, `&`, `/`, `@`, `;` or brackets; or any token in
   `ORGANISATION_WORDS` (legal forms, industry, public bodies, place features) -> organisation.
3. **Person shape**: after dropping generational suffixes (`Jr`, `Sr`, `II`...) and initials,
   two to four name tokens, each letters with an optional hyphen or apostrophe, or a lower-case
   particle (`van`, `de`, `del`...); in a mixed-case name no upper-case acronym; and the first
   name token, or the second after a leading initial, is a given name in the US Census 1990
   first-name lists (`data/vendored/organizations/first_names_us_census_1990.txt`, public domain).
   `Last, First Middle` is read in that order. A trailing "Revocable / Living / Family Trust" is
   cut first and the rest tested the same way: such a trust is named after its grantor.
   Anything else -> organisation.

Pure functions of the name and the two files; no database, no environment. `services/db/models.py`
uses `is_personal_name` as the column default, so every creation path (queue sponsors, EIA-860 and
GHGRP owners, the Atlas operators, intake) classifies at insert without each loader remembering to;
`services/ingest/personal_data.py` re-applies it to existing rows when this file or the curated
list changes.
"""

from __future__ import annotations

import argparse
import functools
import hashlib
import pathlib
import re
import sys
from dataclasses import dataclass
from typing import Literal

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
FIRST_NAMES_PATH = ROOT / "data" / "vendored" / "organizations" / "first_names_us_census_1990.txt"
OVERRIDES_PATH = ROOT / "data" / "vendored" / "organizations" / "personal_data_overrides.yaml"

Basis = Literal["heuristic", "curated_personal", "curated_not_personal", "structure", "no_person_shape"]

#: A token from this set anywhere in the name makes it an organisation. Legal forms, industry words,
#: public bodies and place features: the words that turn "Cherry Valley" or "Hope Solar" into a
#: project or a company rather than a person. Lower case, punctuation stripped.
ORGANISATION_WORDS: frozenset[str] = frozenset(
    """
    llc llp lp lc lcc lly lkc inc incorporated corp corporation co company companies ltd limited plc
    gmbh ag sa sas spa bv nv pty pte kk ab oy as asa holdings holding holdco hold group partners partnership
    associates association trust trustees fund funds capital investments investment ventures venture
    enterprises enterprise industries industrial international global national american america
    services service solutions systems system technologies technology tech consulting consultants
    management development developments developer developers devco projectco project projects spv
    energy energies power solar wind windfarm windpark windpower storage battery bess ess renewables
    renewable electric electricity electrical utility utilities gas oil petroleum pipeline pipelines
    midstream gathering processing transmission distribution generation generating grid hydro hydroelectric
    nuclear geothermal biogas biomass bioenergy biofuels ethanol fuels fuel refining refinery
    biorefining carbon hydrogen lng cng rng landfill waste recycling environmental water sanitation
    mining minerals resources resource operating operations production producers products
    plant plants station stations facility facilities center centre array park farm farms ranch
    ranches dairy dairies agriculture agricultural agri cooperative coop coops cooperatives
    bank banks financial finance insurance realty properties property estates estate land lands
    authority board commission department dept district county city town village township borough
    parish state states federal government municipal municipality public agency bureau office
    council regional region tribe tribal nation university college school schools hospital medical
    church foundation institute society club brothers sons lumber title clearing irr dist clean wave
    agenergy manulife studios studio beverage electronics regents bishop commonwealth
    valley creek creeks hill hills ridge mountain mountains mount lake lakes river rivers springs
    spring falls canyon mesa prairie meadow meadows grove bay harbor harbour island islands peak
    gap run branch fork bluff bluffs hollow flats plains junction crossing point heights field
    fields forest woods road street avenue highway route
    north south east west northern southern eastern western central northeast northwest southeast
    southwest mid upper lower new old big little great grand
    """.split()
)
#: Generational and professional suffixes: dropped before counting name tokens.
SUFFIXES: frozenset[str] = frozenset({"jr", "sr", "ii", "iii", "iv", "v", "md", "phd", "esq", "dds", "cpa"})
#: Surname particles ("Dorothy van Quillfeather" is a name; "van" is not an acronym or a word).
PARTICLES: frozenset[str] = frozenset(
    {
        "van",
        "von",
        "de",
        "der",
        "den",
        "del",
        "della",
        "da",
        "di",
        "du",
        "la",
        "le",
        "st",
        "mac",
        "ter",
        "ten",
    }
)

_STRUCTURE = re.compile(r"[0-9&/@;()\[\]{}#%+=]")
_NAME_TOKEN = re.compile(r"^[A-Za-z][A-Za-z'\-]*$")
_INITIAL = re.compile(r"^[A-Za-z]\.?$")
#: A trailing "Revocable Trust", "Living Trust", "Family Trust": a trust named after its grantor is a
#: natural person's estate-planning vehicle, so the name before it is tested as a person's.
_PERSONAL_TRUST = re.compile(
    r"\s+(?:(?:revocable|irrevocable|living|family)\s+)+trust(?:\s+dated\b.*)?\s*$", re.IGNORECASE
)


@dataclass(frozen=True)
class Classification:
    personal: bool
    basis: Basis


def normalise(name: str) -> str:
    """Case-folded, punctuation dropped, whitespace collapsed: the form the curated digests hash."""
    letters = re.sub(r"[^\w\s'-]", " ", name.casefold())
    return " ".join(letters.split())


def name_digest(name: str) -> str:
    return hashlib.sha256(normalise(name).encode("utf-8")).hexdigest()


@functools.lru_cache(maxsize=1)
def given_names() -> frozenset[str]:
    lines = FIRST_NAMES_PATH.read_text(encoding="utf-8").splitlines()
    return frozenset(line.strip() for line in lines if line.strip() and not line.startswith("#"))


@dataclass(frozen=True)
class Overrides:
    not_personal: frozenset[str]
    personal_sha256: frozenset[str]


@functools.lru_cache(maxsize=1)
def overrides() -> Overrides:
    data = yaml.safe_load(OVERRIDES_PATH.read_text(encoding="utf-8")) or {}
    return Overrides(
        not_personal=frozenset(normalise(str(n)) for n in data.get("not_personal") or []),
        personal_sha256=frozenset(str(h).strip().lower() for h in data.get("personal_sha256") or []),
    )


def _bare(token: str) -> str:
    return token.strip(".,'\"").casefold()


def _reorder_last_first(name: str) -> str:
    """`Quillfeather, Alan Z.` -> `Alan Z. Quillfeather`. Anything with more or fewer than one comma
    is left as is, as is a comma followed by a legal form (`Garrett Solar, LLC` is caught by the
    word list anyway)."""
    parts = [p.strip() for p in name.split(",")]
    if len(parts) != 2 or not parts[0] or not parts[1]:
        return name
    return f"{parts[1]} {parts[0]}"


def _has_person_shape(name: str) -> bool:
    tokens = _reorder_last_first(name).replace(",", " ").split()
    mixed_case = name != name.upper() and name != name.lower()
    core: list[str] = []
    initials: list[int] = []
    for i, raw in enumerate(tokens):
        bare = _bare(raw)
        if bare in SUFFIXES and i > 0:
            continue
        if _INITIAL.match(raw):
            initials.append(len(core))
            core.append("")  # placeholder keeps positions; filtered below
            continue
        if bare in PARTICLES and i > 0:
            continue  # "van X", "Van Der X": a surname particle, not a name token of its own
        stripped = raw.strip(".,")
        if not _NAME_TOKEN.match(stripped):
            return False
        if mixed_case and len(stripped) >= 2 and stripped.isupper():
            return False  # an acronym in a mixed-case name: "AL Sandersville", "CHI Energy"
        core.append(stripped)
    names = [t for t in core if t]
    if not 2 <= len(names) <= 4:
        return False
    first_given = core[0] if core[0] else (core[1] if len(core) > 1 else "")
    head = re.split(r"[-']", first_given)[0].upper()
    return head in given_names()


def classify(name: str | None) -> Classification:
    """`personal=True` when `name` is probably a natural person (module docstring for the rule)."""
    if not name or not name.strip():
        return Classification(False, "no_person_shape")
    norm = normalise(name)
    curated = overrides()
    if name_digest(name) in curated.personal_sha256:
        return Classification(True, "curated_personal")
    if norm in curated.not_personal:
        return Classification(False, "curated_not_personal")
    if _STRUCTURE.search(name):
        return Classification(False, "structure")
    personal_trust = _PERSONAL_TRUST.search(name)
    if personal_trust and _has_person_shape(name[: personal_trust.start()]):
        return Classification(True, "heuristic")  # "<person> Revocable Living Trust" names its grantor
    if any(_bare(t) in ORGANISATION_WORDS for t in name.replace(",", " ").split()):
        return Classification(False, "structure")
    if _has_person_shape(name):
        return Classification(True, "heuristic")
    return Classification(False, "no_person_shape")


def is_personal_name(name: str | None) -> bool:
    return classify(name).personal


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_hash = sub.add_parser("hash", help="print the curated-file digest line for a person's name")
    p_hash.add_argument("name")
    p_check = sub.add_parser("check", help="classify one name")
    p_check.add_argument("name")
    args = parser.parse_args(argv)
    if args.cmd == "hash":
        print(f"  - {name_digest(args.name)}")  # noqa: T201 — CLI output
    else:
        result = classify(args.name)
        print(f"personal={result.personal} basis={result.basis}")  # noqa: T201 — CLI output
    return 0


if __name__ == "__main__":
    sys.exit(main())
