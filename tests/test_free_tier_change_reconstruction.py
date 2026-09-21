"""Why the ISO change-event delay was dropped — the measurement, kept as a regression test.

Written on 2026-09-20 while a 14-day delay on ISO change events was still in force, to measure
what it actually bought. The answer was: nothing a free reader could not have anyway. The owner
read it and dropped the delay on 2026-09-21 (`docs/00-PLAN.md` decisions log), so the assertions
below now run against a product that withholds nothing — and that is exactly why the file stays.
It is the evidence for the decision, and it fails if the reasoning behind it stops holding.

The method is the one a free reader would have used: read the public API, let a run land, read it
again, diff the two responses, and compare the diff with the event.

Four facts this file establishes, each as an assertion:

1. **Every field `pipeline/diff.py` emits an event for is a public record field.** The diff fires
   on `lifecycle_state`, `capacity_mw` and `proposed_cod` and on presence/absence; all three are
   in `serialize_proposal`'s output on the public tier. This is what made the delay defeatable:
   withholding the event never withheld the information.
2. **The record moves in the same transaction as the event.** After the second run the public
   detail response already carries the new value and `last_changed` already carries the date.
3. **The diff of two public reads equals the event.** `(field, before, after)` recovered from two
   consecutive public responses is identical to the event's own payload.
4. **And now the event itself is public too**, at the same instant as the record
   (`tests/test_publication_is_never_time_delayed.py` pins the rule; here it is asserted on the
   shape that used to be withheld, so the two reads and the event agree rather than the reads
   merely catching up with it).

The cost of running (2) and (3) over the whole register is stated in
`test_polling_cost_for_a_full_public_sweep` in requests per day, from the real page cap and the
real public rate limit: 53 requests a sweep against an allowance of 1,440. That number is what
"the delay is theatre" meant, and it is kept so the argument is reproducible rather than recalled.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pandas as pd
import pytest
from sqlalchemy import select

from services.api.pagination import MAX_LIMIT
from services.api.ratelimit import TIER_LIMITS, WINDOW_SECONDS
from services.db.models import Event, Proposal
from services.ingest.loader import load_dataframe, upsert_licence_and_source
from services.ingest.test_loader import sample_proposal_row
from tests.test_publication_is_never_time_delayed import iso_source_entry

#: `pipeline/diff.py` emits `event_type` in {new, removed, withdrawn, status_change,
#: capacity_change, cod_change}; the `field` it names is one of these three.
DIFF_FIELDS_TO_PUBLIC_KEYS = {
    "lifecycle_state": "lifecycle_state",
    "capacity_mw": "capacity_mw",
    "proposed_cod": "proposed_online_date",
}


def _row(source_id: str, record_id: str, **over: Any) -> dict[str, Any]:
    r = dict(sample_proposal_row(record_id))
    r["record_id"] = f"{source_id}:{record_id}"
    r["source_id"] = source_id
    r.update(over)
    return r


def _diff_event(source_id: str, record_id: str, field: str, before: Any, after: Any) -> dict[str, Any]:
    event_type = {"lifecycle_state": "status_change", "capacity_mw": "capacity_change"}.get(
        field, "cod_change"
    )
    return {
        "event_type": event_type,
        "record_id": f"{source_id}:{record_id}",
        "source_id": source_id,
        "field": field,
        "before": before,
        "after": after,
        "observed_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
    }


@pytest.fixture()
def iso_store(db, client):
    """An ISO queue source loaded through the real loader — the register that used to be delayed."""
    entry = iso_source_entry()
    src = upsert_licence_and_source(db, entry, "2026-09-18")
    load_dataframe(db, src, "proposal", pd.DataFrame([_row(entry.id, "Q1")]), None)
    db.commit()
    return entry, src


def _public_detail(client, public_id: str) -> dict[str, Any]:
    resp = client.get(f"/v1/proposals/{public_id}")
    assert resp.status_code == 200, resp.text
    return dict(resp.json()["data"])


# --------------------------------------------------------------------------------- 1. field cover
def test_every_field_the_diff_fires_on_is_a_public_record_field(client, db, iso_store) -> None:
    proposal = db.scalars(select(Proposal)).one()
    payload = _public_detail(client, proposal.public_id)
    missing = [k for k in DIFF_FIELDS_TO_PUBLIC_KEYS.values() if k not in payload]
    assert missing == [], (
        "a change event names one of these three fields; every one of them is on the free-tier "
        f"record payload, so none of them can be withheld by delaying the event. Missing: {missing}"
    )
    assert "last_changed" in payload, "the free tier is also told *when* the record moved"

    # `new` and `removed` are the two event types that name no canonical field. Both are legible
    # from the public provenance block: `first_seen` dates the arrival and `gone_at` dates the
    # disappearance from the register, on the day it happens.
    provenance = payload["provenance"][0]
    for key in ("first_seen", "last_seen", "gone_at"):
        assert key in provenance, key


# ---------------------------------------------------------------- 2 & 3. reconstruct the withheld event
@pytest.mark.parametrize(
    ("field", "old_value", "new_value", "row_override"),
    [
        ("lifecycle_state", "filed", "studied", {"lifecycle_state": "studied"}),
        ("capacity_mw", 100.0, 250.0, {"capacity_mw": 250.0}),
        ("proposed_cod", "2028-06-30", "2029-06-30", {"proposed_cod": "2029-06-30"}),
    ],
)
def test_two_public_reads_reconstruct_the_withheld_change(
    client,
    db,
    iso_store,
    field: str,
    old_value: Any,
    new_value: Any,
    row_override: dict[str, Any],
) -> None:
    entry, src = iso_store
    proposal = db.scalars(select(Proposal)).one()
    before_read = _public_detail(client, proposal.public_id)

    load_dataframe(
        db,
        src,
        "proposal",
        pd.DataFrame([_row(entry.id, "Q1", **row_override)]),
        pd.DataFrame([_diff_event(entry.id, "Q1", field, old_value, new_value)]),
    )
    db.commit()

    # The event is public the moment it is published (owner, 2026-09-21). Until that decision it
    # waited 14 days here -- and the two reads below recovered it anyway, which is why it does not.
    event = db.scalars(select(Event)).one()
    assert event.public_at == event.published_at
    assert len(client.get("/v1/events").json()["data"]) == 1, "no longer withheld from anyone"

    # The same change, recovered from two reads of the record page, as it was while the event was
    # withheld: the reconstruction still works, and now it is redundant rather than a loophole.
    after_read = _public_detail(client, proposal.public_id)
    public_key = DIFF_FIELDS_TO_PUBLIC_KEYS[field]
    reconstructed = {
        "field": public_key,
        "before": before_read[public_key],
        "after": after_read[public_key],
    }
    assert reconstructed["before"] != reconstructed["after"]
    # The event's `after` value, recovered verbatim from a page that was never delayed.
    assert str(reconstructed["after"]).startswith(str(new_value))
    assert str(reconstructed["before"]).startswith(str(old_value))
    assert before_read["last_changed"] != after_read["last_changed"], (
        "the free tier is told the record moved on the day it moved"
    )


def test_the_public_list_sorts_by_last_changed_by_default(client, db, iso_store) -> None:
    """No polling infrastructure is needed to *find* the records that moved: `-last_changed` is
    the public list's default sort, so "what changed today" is the free tier's front page."""
    resp = client.get("/v1/proposals")
    assert resp.status_code == 200
    assert client.get("/v1/proposals?sort=-last_changed").json()["data"] == resp.json()["data"]


def test_polling_cost_for_a_full_public_sweep() -> None:
    """What defeating the delay costs, in the product's own numbers.

    A sweep of the whole register is `ceil(records / MAX_LIMIT)` requests; the public tier's own
    rate limit says how many are available. These are the constants the API actually enforces, so
    the ratio is measured, not assumed. The register held 10,409 proposals on the 2026-09-15 load
    (`docs/00-PLAN.md`); that figure is the input, and the arithmetic is the assertion.
    """
    register_size = 10_409  # docs/00-PLAN.md, 2026-09-13 "Site on the full dataset"
    requests_per_sweep = -(-register_size // MAX_LIMIT)
    requests_per_day = TIER_LIMITS["public"] * (86_400 // WINDOW_SECONDS)

    assert MAX_LIMIT == 200
    assert requests_per_sweep == 53
    assert requests_per_day == 1440
    assert requests_per_sweep * 2 <= requests_per_day, (
        "two full sweeps a day -- enough to diff every record daily -- fit inside the "
        "unauthenticated public rate limit with room to spare"
    )
