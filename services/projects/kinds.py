"""The tracker's item types and their stages (decision row 2026-10-23, "My pipeline is one central
tracker"; docs/30 §11.4; migration `0042_tracker_kinds`).

One list holds everything a signed-in user is working on. Each item is one of three kinds, each
with its own stage vocabulary, enforced by one `CHECK` on `private_project`:

- `project`: a project the user is developing (docs/19 §2.1, unchanged): prospecting, site_control,
  development, ready_to_build, construction, operating;
- `bid`: a response to an RFP or a funding call, attached to one of the user's projects or standing
  alone: considering, preparing, submitted, shortlisted, won, lost, withdrawn;
- `investment`: a project the user is looking to fund (the funder's private note): screening,
  diligence, term_sheet, committed, closed, passed.

Stages are user-set, never inferred and never shown publicly. **Closed** stages (a bid won, lost or
withdrawn; an investment closed or passed) end the item's interest in deadlines: such an item has
no next deadline, and a closed bid no longer lends its deadline to its project.

The kind is set when the item is created and does not change: a bid's checklist is the bid pack
(`data/checklist/bid-v1.yaml`), a project's and an investment's the bankability pack, and only a
project may have bids attached.

This module imports nothing from the rest of the package, so `models` and the migration can both
build their constraints from it (the migration freezes its own copy; `test_migration.py` pins the
two equal).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

#: In the order the type filter shows them (Project · Bid · Investment).
ITEM_KINDS: Final = ("project", "bid", "investment")
DEFAULT_KIND: Final = "project"

#: docs/19 §2.1. User-set, never inferred, never shown publicly.
PROJECT_STAGES: Final = (
    "prospecting",
    "site_control",
    "development",
    "ready_to_build",
    "construction",
    "operating",
)
BID_STAGES: Final = ("considering", "preparing", "submitted", "shortlisted", "won", "lost", "withdrawn")
INVESTMENT_STAGES: Final = ("screening", "diligence", "term_sheet", "committed", "closed", "passed")

KIND_STAGES: Final[Mapping[str, tuple[str, ...]]] = {
    "project": PROJECT_STAGES,
    "bid": BID_STAGES,
    "investment": INVESTMENT_STAGES,
}
#: Every stage of every kind, kinds in order. No stage name is shared between kinds.
ALL_STAGES: Final = tuple(stage for kind in ITEM_KINDS for stage in KIND_STAGES[kind])
#: A new item's stage when none is given: its kind's first.
DEFAULT_STAGES: Final[Mapping[str, str]] = {kind: stages[0] for kind, stages in KIND_STAGES.items()}
#: Stages after which the item has no next deadline (module docstring).
CLOSED_STAGES: Final[Mapping[str, frozenset[str]]] = {
    "project": frozenset(),
    "bid": frozenset({"won", "lost", "withdrawn"}),
    "investment": frozenset({"closed", "passed"}),
}

KIND_LABELS: Final[Mapping[str, str]] = {"project": "Project", "bid": "Bid", "investment": "Investment"}
#: The filter's plural words ("Projects 3 · Bids 3 · Investments 1").
KIND_PLURALS: Final[Mapping[str, str]] = {"project": "Projects", "bid": "Bids", "investment": "Investments"}
#: One line each, for the new-item form's type choice.
KIND_DESCRIPTIONS: Final[Mapping[str, str]] = {
    "project": "A project you are developing, from prospecting to operating.",
    "bid": "Your response to an RFP or a funding call, from considering to the outcome.",
    "investment": "A project you are looking to fund, from screening to close.",
}
STAGE_LABELS: Final[Mapping[str, str]] = {
    "prospecting": "Prospecting",
    "site_control": "Site control",
    "development": "Development",
    "ready_to_build": "Ready to build",
    "construction": "Construction",
    "operating": "Operating",
    "considering": "Considering",
    "preparing": "Preparing",
    "submitted": "Submitted",
    "shortlisted": "Shortlisted",
    "won": "Won",
    "lost": "Lost",
    "withdrawn": "Withdrawn",
    "screening": "Screening",
    "diligence": "Diligence",
    "term_sheet": "Term sheet",
    "committed": "Committed",
    "closed": "Closed",
    "passed": "Passed",
}

#: Which checklist pack each kind's items are copied from (`services.projects.checklist`).
KIND_PACKS: Final[Mapping[str, str]] = {"project": "bankability", "bid": "bid", "investment": "bankability"}

#: What "Track" may create from each kind of public record (decision row 2026-10-23): an RFP or a
#: funding notice makes a bid; a project page a project or an investment (the user chooses); a
#: site, a plant or a grid connection point a project. Default first.
TRACK_KINDS: Final[Mapping[str, tuple[str, ...]]] = {
    "opportunity": ("bid",),
    "proposal": ("project", "investment"),
    "site": ("project",),
    "asset": ("project",),
    "interconnection_point": ("project",),
}


def is_stage_of(kind: object, stage: object) -> bool:
    return isinstance(kind, str) and isinstance(stage, str) and stage in KIND_STAGES.get(kind, ())


def is_closed(kind: str, stage: str) -> bool:
    return stage in CLOSED_STAGES.get(kind, frozenset())


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def stage_for_kind_condition(kind_column: str = "kind", stage_column: str = "stage") -> str:
    """The `CHECK` that ties a stage to its kind's vocabulary: one disjunct per kind."""
    return " OR ".join(
        f"({kind_column} = {kind!r} AND {_in(stage_column, KIND_STAGES[kind])})" for kind in ITEM_KINDS
    )


def kind_condition(kind_column: str = "kind") -> str:
    return _in(kind_column, ITEM_KINDS)
