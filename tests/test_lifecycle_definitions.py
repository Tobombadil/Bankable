"""The published status definitions may not drift from the code that does the mapping.

`/methodology` and `GET /v1/lifecycle-states` publish what each lifecycle state and opportunity
status means. The definitions are prose in `data/vocabulary/lifecycle_states.yaml`; the mappings
under them are read from `pipeline/status_map.yaml` and the per-connector status maps. Three
things can silently pull apart, and each is a failure here rather than a wrong sentence on a
public page:

1. A state added to `services/db/models.py` with no definition written for it — the page would
   render a chip it cannot explain.
2. A definition left behind for a state that no longer exists — the page would define a word
   nothing can carry.
3. A status map emitting a canonical value that neither vocabulary knows — the database CHECK
   constraint would reject the load, and the published definitions would have promised a state
   the code cannot store.

It also pins the text scan `services/api/lifecycle.py` uses to relate a status map's key to a
source id against the real connector classes, since a scan is exactly the thing that drifts.
"""

from __future__ import annotations

import importlib

import pytest
import yaml

from services.api.lifecycle import (
    DEFINITIONS_PATH,
    PRE_CONSTRUCTION,
    connectors_by_status_key,
    mapped_states,
    status_map_paths,
    vocabulary,
)
from services.db.models import LIFECYCLE_STATES, OPPORTUNITY_STATUSES

DOCUMENT = yaml.safe_load(DEFINITIONS_PATH.read_text(encoding="utf-8"))


def test_every_lifecycle_state_has_a_published_definition() -> None:
    assert set(DOCUMENT["lifecycle_states"]) == set(LIFECYCLE_STATES), (
        "data/vocabulary/lifecycle_states.yaml and services/db/models.py::LIFECYCLE_STATES "
        "disagree; a state exists in one and not the other"
    )


def test_every_opportunity_status_has_a_published_definition() -> None:
    assert set(DOCUMENT["opportunity_statuses"]) == set(OPPORTUNITY_STATUSES)


@pytest.mark.parametrize("state", LIFECYCLE_STATES)
def test_lifecycle_definitions_are_not_empty(state: str) -> None:
    assert (DOCUMENT["lifecycle_states"][state].get("definition") or "").strip()


@pytest.mark.parametrize("status", OPPORTUNITY_STATUSES)
def test_opportunity_definitions_are_not_empty(status: str) -> None:
    assert (DOCUMENT["opportunity_statuses"][status].get("definition") or "").strip()


def test_no_status_map_produces_a_state_outside_both_vocabularies() -> None:
    known = set(LIFECYCLE_STATES) | set(OPPORTUNITY_STATUSES)
    unknown = mapped_states() - known
    assert not unknown, f"status maps emit states the database would reject: {sorted(unknown)}"


def test_pre_construction_claim_names_real_states() -> None:
    """The published page claims four distinct pre-construction states where trackers built from
    announcements publish one. The claim cannot outlive the states it names."""
    assert set(PRE_CONSTRUCTION) <= set(LIFECYCLE_STATES)
    assert len(PRE_CONSTRUCTION) == len(set(PRE_CONSTRUCTION)) == 4


def test_status_key_scan_matches_the_real_connector_classes() -> None:
    """`services/api/lifecycle.py` reads `source_id`/`status_key`/`kind` out of the connector
    modules as text rather than importing thirteen connectors into the API process. This is the
    check that the cheap read still agrees with the classes."""
    scanned = connectors_by_status_key()
    for status_key, entry in scanned.items():
        module_path = None
        for path in sorted((DEFINITIONS_PATH.parents[2] / "pipeline" / "connectors").glob("*/connector.py")):
            if f'"{status_key}"' in path.read_text(encoding="utf-8"):
                module_path = path
                break
        assert module_path is not None, status_key
        module = importlib.import_module(f"pipeline.connectors.{module_path.parent.name}.connector")
        connector = module.Connector
        assert connector.status_key == status_key
        assert connector.source_id == entry["source_id"]
        assert connector.kind == entry["kind"]


def test_every_status_map_file_is_read() -> None:
    """A connector that gains its own status map must be picked up without anyone editing the
    published page."""
    paths = status_map_paths()
    assert any(p.name == "status_map.yaml" and p.parent.name == "pipeline" for p in paths)
    assert len(paths) > 1


def test_vocabulary_separates_the_two_meanings_of_shared_words() -> None:
    """`announced`, `cancelled`, `unknown` and `withdrawn` exist in both vocabularies with
    different meanings. A proposal's `announced` must not be explained with a tender notice
    type."""
    vocab = vocabulary()
    lifecycle = {s["state"]: s for s in vocab["lifecycle_states"]}
    opportunity = {s["state"]: s for s in vocab["opportunity_statuses"]}
    proposal_sources = {m["status_key"] for m in lifecycle["announced"]["maps_from"]}
    opportunity_sources = {m["status_key"] for m in opportunity["announced"]["maps_from"]}
    assert proposal_sources
    assert opportunity_sources
    assert not (proposal_sources & opportunity_sources)
    assert "ted" in opportunity_sources
    assert "ted" not in proposal_sources


def test_unknown_is_marked_as_the_fallback_and_has_no_mappings() -> None:
    """`harmonise_status` reaches `unknown` by falling through every map, so an empty
    `maps_from` there is correct and the page must say so rather than reading as a gap."""
    vocab = vocabulary()
    unknown = next(s for s in vocab["lifecycle_states"] if s["state"] == "unknown")
    assert unknown["is_fallback"] is True
    assert unknown["maps_from"] == []


def test_withheld_sources_mappings_are_shown_not_hidden() -> None:
    """SPP and ISO-NE have a written mapping and no connector, because their rows are withheld.
    The page reports them with `rows_published: false`; dropping them would hide that our
    vocabulary already covers those queues."""
    vocab = vocabulary()
    withheld = [
        m for state in vocab["lifecycle_states"] for m in state["maps_from"] if not m["rows_published"]
    ]
    assert {m["status_key"] for m in withheld} == {"spp", "isone"}
    assert all(m["source_id"] is None for m in withheld)


def test_definitions_carry_the_date_they_were_written() -> None:
    """Prose that cannot be derived must say when it was written; the derived half needs no such
    date because it is re-read every request."""
    assert vocabulary()["definitions_written"]
