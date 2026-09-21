"""Publication-delay policy — **the paywall is by shape, and nothing is delayed by time**.

Owner decision, 2026-09-19 (`docs/00-PLAN.md` decisions log, 2026-09-18 row, item 2), verbatim:
"alerts, exports, API and watchlists are paid; free users see every record; the delay is kept
only on ISO change events. Supersedes the time-delay model in docs/10 and docs/41".

Owner decision, 2026-09-21 (same log), answering the open question the 2026-09-20 row raised:
drop the ISO change-event delay too. The measurement that settled it is recorded there and in
`tests/test_free_tier_change_reconstruction.py`: `pipeline/diff.py` fires change events on exactly
three fields (`lifecycle_state`, `capacity_mw`, `proposed_cod`), all three are on the free-tier
record payload, the record updated in the same transaction that withheld the event, `last_changed`
is public and `-last_changed` is the public list's default sort — so the withheld `(field, before,
after)` was recoverable from two public reads, and a full daily sweep of the register cost 53
requests against the public tier's own allowance of 1,440.

What that means in this module, for every row it governs:

* **`public_at == published_at`. Always. For records and for events alike.** A `proposal`, an
  `opportunity` and an `event` are visible to a free, anonymous reader the moment they are
  published. `RECORD_LAG_DAYS` is `0` and `record_public_at` is the identity function.
* **There is no knob, at either level.** Not a per-source field in `data/sources.yaml`, not a
  `source.lag_days`/`source.lag_overrides` column (migration `0019` drops them), not an admin
  `PATCH`. This is deliberate and it is the same argument the record-level lag already carried
  here: a dormant per-source lag that an operator could switch back on re-creates exactly the
  defeatable promise the owner decided to remove, and leaves a public page one audited write away
  from claiming a delay the product does not apply. Reintroducing a delay of either kind is an
  owner decision that lands in this module, not a configuration change.

What the paid tiers buy is **shape**: alerts, exports, the API and watchlists (owner, 2026-09-19).
`tests/test_publication_is_never_time_delayed.py` pins the whole statement — manifest, policy
function and loader — and fails if any source declares a change-event lag again.

**Unchanged by any of this, and not this module's business:** the licence and source-state gates.
`services/api/visibility.py` still refuses `restricted`/`unknown` sources on every non-admin
surface, still requires `publish_state = 'public'`, and still reads `public_at` for the public
tier — that column simply always equals `published_at` now.
"""

from __future__ import annotations

import datetime as dt
from typing import Literal

Kind = Literal["proposal", "opportunity"]

#: Nothing is delayed on any tier (owner, 2026-09-19 for records, 2026-09-21 for change events).
#: Exported as a named constant so the API envelope, the feed titles and the public-site copy all
#: state the same number and a change here is a one-line diff rather than a hunt. The
#: `record_lag_days()` accessor that used to sit beside it went with the 2026-09-21 removal: it had
#: no caller once there was one number rather than one per kind, and an uncalled lag function is
#: the shape of a knob.
RECORD_LAG_DAYS = 0


def record_public_at(published_at: dt.datetime) -> dt.datetime:
    """`public_at` for a record: publication time itself (`docs/21` §5.4 as amended 2026-09-21).

    An `event` gets the same treatment — the loader passes its `published_at` through this same
    function — because since 2026-09-21 there is one rule for every row rather than one per shape.
    """
    return published_at + dt.timedelta(days=RECORD_LAG_DAYS)
