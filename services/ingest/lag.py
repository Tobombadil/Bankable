"""Publication-delay policy — **paywall by shape, not by time**.

Owner decision, 2026-09-19 (`docs/00-PLAN.md` decisions log, 2026-09-18 row, item 2), verbatim:
"alerts, exports, API and watchlists are paid; free users see every record; the delay is kept
only on ISO change events. Supersedes the time-delay model in docs/10 and docs/41".

What that means in this module:

* **Records carry no delay.** A `proposal` or an `opportunity` is visible to a free, anonymous
  reader the moment it is published: `public_at == published_at`. `RECORD_LAG_DAYS` is `0` and is
  not configurable per source — the blanket 14-day supply / 7-day opportunity lag that used to
  live here is gone, not merely defaulted to zero.
* **Change events from an ISO queue register keep the delay.** `Event.public_at =
  published_at + lag(source, event_type)`, where the lag comes from the source row
  (`source.lag_days`, seeded from the manifest's `change_event_lag_days`) and
  `source.lag_overrides[event_type]` may override it per event type (e.g. `{"withdrawn": 0}`).
  A source that declares no change-event lag delays nothing.

**The predicate, stated exactly.** An event is delayed on the public tier iff the `source` row it
was ingested under carries a positive change-event lag. That flag is data, not code: it is
declared per source in `data/sources.yaml` as `change_event_lag_days`, mirrored into
`source.lag_days` by `services/ingest/loader.py::upsert_licence_and_source`, and overridable at
runtime by an operator through `PATCH /admin/v1/sources/{id}` (audited, like every admin write).
Today exactly the eight `us.iso.*` interconnection-queue registers carry it — CAISO, ERCOT
(generation and large-load), SPP, NYISO, ISO-NE, PJM and MISO — which is the set the owner's
decision names. Four of those eight (SPP, ISO-NE, PJM, MISO) are `restricted`/`unknown` and
publish nothing on any non-admin tier at all, so the flag is inert for them until a licence
exists; it is set anyway, because the licence gate and the lag are independent questions and the
manifest should answer both. `tests/test_iso_change_event_lag.py` pins the set against the
manifest, so a ninth ISO queue added without the field fails a test rather than silently
publishing live.

Deliberately *not* keyed on `source.category` (`generation_queue` / `load_queue`): that vocabulary
covers fourteen sources, six of which are not ISO queues — `gb.neso.tec_register`,
`au.aemo.connections_scorecard`, `us.oasis.non_iso_queues` (explicitly the non-ISO utilities),
`ie.eirgrid.connections`, `ca.ieso.connection_status` and `ca.aeso.connection_list` — and the
owner's decision says ISO. An explicit field keeps the two apart and leaves the decision auditable
in the registry that records every other per-source term.

The record-level lag is not "0 by default, set it per source": there is no knob. Reintroducing a
record delay is an owner decision that would land here, not a configuration change.
"""

from __future__ import annotations

import datetime as dt
from typing import Literal

Kind = Literal["proposal", "opportunity"]

#: Records are never delayed on any tier (owner, 2026-09-19). Exported as a named constant so the
#: API envelope, the feed titles and the public-site copy all state the same number and a change
#: here is a one-line diff rather than a hunt.
RECORD_LAG_DAYS = 0

#: The delay an ISO queue register's change events carry on the public tier, in days — the one
#: surviving time lever. Seeded into `data/sources.yaml` as `change_event_lag_days` on each ISO
#: queue source; this constant is what that file is generated to agree with and what the
#: migration that backfilled existing rows used.
ISO_CHANGE_EVENT_LAG_DAYS = 14


def record_lag_days() -> int:
    """Lag applied to a `proposal`/`opportunity` row. Always `RECORD_LAG_DAYS` (zero)."""
    return RECORD_LAG_DAYS


def record_public_at(published_at: dt.datetime) -> dt.datetime:
    """`public_at` for a record: publication time itself (`docs/21` §5.4 as amended 2026-09-19)."""
    return published_at + dt.timedelta(days=RECORD_LAG_DAYS)


def change_event_lag_days(
    *,
    source_change_event_lag_days: int | None = None,
    lag_overrides: dict[str, int] | None = None,
    event_type: str | None = None,
) -> int:
    """Effective public-tier delay, in days, for one change event.

    `lag_overrides[event_type]` wins over the source's own lag (so a source can publish, say,
    `withdrawn` immediately while delaying `status_change`); a source with no declared
    change-event lag delays nothing.
    """
    if event_type and lag_overrides and event_type in lag_overrides:
        return max(0, int(lag_overrides[event_type]))
    if source_change_event_lag_days is None:
        return 0
    return max(0, int(source_change_event_lag_days))


def change_event_public_at(
    published_at: dt.datetime,
    *,
    source_change_event_lag_days: int | None = None,
    lag_overrides: dict[str, int] | None = None,
    event_type: str | None = None,
) -> dt.datetime:
    """`public_at = published_at + lag(source, event_type)` for an `event` row."""
    days = change_event_lag_days(
        source_change_event_lag_days=source_change_event_lag_days,
        lag_overrides=lag_overrides,
        event_type=event_type,
    )
    return published_at + dt.timedelta(days=days)
