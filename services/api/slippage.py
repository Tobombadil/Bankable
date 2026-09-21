"""Schedule slippage: the read-time derived fact "this project's own stated target date has
passed while it is still nominally active" (docs/22 §18).

Not a lifecycle state. `LIFECYCLE_STATES` is unchanged and there is no migration, because nothing
here is stored: "overdue" is a function of `(proposed_online_date, lifecycle_state, today)` and
only one of those three is a column. A materialised `is_overdue` flag would be wrong the day after
it was written -- the record does not change, the calendar does -- and the store has no daily
recompute job to keep one true. The two things a stored column would buy (an index, and a sort
key) are not needed: the predicate below is a plain range comparison on `proposed_online_date`,
which is sargable, and the measured set is 129 rows out of 10,409.

**Why the grace period exists, and why it is 90 days.** A bare `proposed_online_date < today` test
is mostly noise on the current load. Measured 2026-09-21 against `web/.data/dev.db` (10,409
proposals, 8,180 dated):

| grace | proposals flagged | of which `us.eia.860m` |
|------:|------------------:|-----------------------:|
|  0 d  |               469 |          308  (66 %)   |
| 31 d  |               283 |          129  (46 %)   |
| 60 d  |               146 |            5  (3 %)    |
| 90 d  |               129 |            0           |
| 120 d |               121 |            0           |

Two source artefacts produce that cliff, and both are date *granularity*, not slippage:

1. **Month-granularity dates.** Every one of the 2,341 `us.eia.860m` dates and all 201
   `us.iso.nyiso.gen_queue` dates fall on the first of a month, because the source field is a
   (`Planned Operation Month`, `Planned Operation Year`) pair. 182 of the naive 469 carry
   `2026-09-01` -- the *current* month, which has not finished. They cannot be late.
2. **Report vintage.** The loaded EIA-860M file is `july_generator2026.xlsx`: the July 2026
   report, retrieved 2026-09-13. A further 125 rows carry `2026-08-01`, a month the evidence
   itself predates. 111 of the 316 flagged `under_construction` rows have the source status
   `(TS) Construction complete, but not yet in commercial operation` -- construction finished,
   commercial operation not yet declared, which is the reporting lag and not a slip.

90 days is where the curve flattens (129 → 121 over the next 30 days), it is longer than any
month so it clears artefact 1 outright, and it is longer than the two-month EIA publication lag so
it clears artefact 2. It is one number, applied to every source, so no per-source table can rot.

The residual false-positive mode is the one the grace cannot fix: a register that stops
maintaining `proposed_online_date` shows a finished project as overdue for ever. That is why
`built`, `withdrawn` and `cancelled` are excluded, why the signal is rendered with the target date
beside it rather than as a bare badge, and why `docs/22` §18.4 states the residual rate.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import sqlalchemy as sa

from services.db.models import Proposal

#: Days past `proposed_online_date` before a proposal counts as slipped. See the module docstring
#: for the measurement that set it. Changing it changes what `slipped=true` means, so it is a
#: published constant (`GET /v1/meta/vocabularies` -> `slip_bucket[].grace_days`), not a literal.
SLIP_GRACE_DAYS = 90

#: The lifecycle states a slip can be claimed against: a target date only means something while
#: the project is still nominally going ahead. `built` is excluded because a built project's
#: target date is spent, `withdrawn`/`cancelled` because a dead project is not late, and
#: `unknown` because we cannot say it is active. Deliberately a separate tuple from
#: `web.viewmodels.ACTIVE_PROPOSAL_STATES` (services must not import web); the two are asserted
#: equal in `services/api/test_slippage.py`.
SLIP_ACTIVE_STATES = (
    "announced",
    "filed",
    "studied",
    "permitted",
    "contracted",
    "under_construction",
)

#: Upper bound in days for each bucket, in order; the last bucket is open-ended. Day counts rather
#: than calendar years so the Python and SQL twins below agree exactly on every boundary.
SLIP_BUCKET_MAX_DAYS: tuple[tuple[str, int | None], ...] = (
    ("under_1y", 365),
    ("1_to_3y", 1095),
    ("over_3y", None),
)
SLIP_BUCKETS: tuple[str, ...] = tuple(name for name, _ in SLIP_BUCKET_MAX_DAYS)


def today() -> dt.date:
    """The current UTC date. A single chokepoint so every test can pass an explicit `on=`."""
    return dt.datetime.now(dt.UTC).date()


def slip_days(lifecycle_state: str, target: dt.date | None, *, on: dt.date) -> int | None:
    """Days past `target`, or `None` when the record carries no slip signal at all.

    `None` for a proposal with no target date (the commonest case outside the active set: nothing
    was promised, so nothing is late), for one outside `SLIP_ACTIVE_STATES`, and for one whose
    target has not passed by more than `SLIP_GRACE_DAYS`. A target date of exactly today is
    `None` -- a date is not missed on the day it names.
    """
    if target is None or lifecycle_state not in SLIP_ACTIVE_STATES:
        return None
    late = (on - target).days
    return late if late > SLIP_GRACE_DAYS else None


def slip_bucket(days: int) -> str:
    """The bucket a positive day count falls in. Boundaries are inclusive of the upper bound:
    365 days is `under_1y`, 366 is `1_to_3y`, 1,095 is `1_to_3y`, 1,096 is `over_3y`."""
    for name, maximum in SLIP_BUCKET_MAX_DAYS:
        if maximum is None or days <= maximum:
            return name
    raise AssertionError("SLIP_BUCKET_MAX_DAYS must end in an open-ended bucket")  # pragma: no cover


def proposal_slip(proposal: Proposal, *, on: dt.date | None = None) -> dict[str, Any] | None:
    """The `schedule_slip` object `services/api/serialize.py` puts on a proposal, or `None`.

    `None` -- not a zeroed object -- so a caller cannot mistake "no promise on record" for "on
    schedule", and so the field is cheap on the 98 % of rows that carry no signal.
    """
    target = proposal.proposed_online_date
    if target is None:
        return None
    days = slip_days(proposal.lifecycle_state, target, on=on or today())
    if days is None:
        return None
    return {
        "target_date": target.isoformat(),
        "days_late": days,
        "bucket": slip_bucket(days),
        "grace_days": SLIP_GRACE_DAYS,
    }


def _slipped_clause(*, on: dt.date, min_days: int, max_days: int | None) -> sa.ColumnElement[bool]:
    """SQL twin of `slip_days` for one day-count window: slipped by more than `min_days` and, if
    bounded, by at most `max_days`.

    Written as literal date bounds computed in Python rather than any SQL date arithmetic, so the
    clause is identical on SQLite and Postgres and stays a sargable range scan on
    `proposed_online_date`.

    The `is_not(None)` term is load-bearing for the *negated* use (`slipped=false`): SQL three-
    valued logic makes `NULL < date` produce NULL, and `NOT NULL` is NULL, which would silently
    drop every proposal with no target date from the complement. `FALSE AND NULL` is FALSE, so
    leading with the null test keeps the whole conjunction FALSE (never NULL) for those rows, and
    their negation TRUE. `test_slippage.py` pins it.
    """
    terms: list[sa.ColumnElement[bool]] = [
        Proposal.lifecycle_state.in_(SLIP_ACTIVE_STATES),
        Proposal.proposed_online_date.is_not(None),
        Proposal.proposed_online_date < on - dt.timedelta(days=min_days),
    ]
    if max_days is not None:
        terms.append(Proposal.proposed_online_date >= on - dt.timedelta(days=max_days))
    return sa.and_(*terms)


def slip_filter(*, slipped: bool | None, buckets: list[str] | None, on: dt.date) -> sa.ColumnElement[bool]:
    """The `WHERE` term for `?slipped=` / `?slip_bucket=`.

    `buckets` wins when both are given (a bucket list already implies `slipped=true`, and the
    route rejects the one contradictory pairing, `slipped=false` with a bucket list, as a 400
    rather than answering 200 with an empty page).
    """
    if buckets:
        lower = SLIP_GRACE_DAYS
        clauses: list[sa.ColumnElement[bool]] = []
        for name, maximum in SLIP_BUCKET_MAX_DAYS:
            if name in buckets:
                clauses.append(_slipped_clause(on=on, min_days=lower, max_days=maximum))
            lower = maximum if maximum is not None else lower
        return sa.or_(*clauses)
    any_slip = _slipped_clause(on=on, min_days=SLIP_GRACE_DAYS, max_days=None)
    return any_slip if slipped else sa.not_(any_slip)
