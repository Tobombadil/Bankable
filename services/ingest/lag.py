"""Public-tier lag policy (docs/21-data-model.md §5.4; docs/04-standards.md §10 pick 1).

Default lag is per source class: **14 days for supply (proposals), 7 days for opportunities**
(the task brief's figures, matching docs/04 §10 pick 1's working default — docs/21 §9 D-1 and
docs/20 §16 A-8 disagree with each other on the *default* number and are logged there as an open
correction; this sprint follows the more specific, more recently reconciled standards doc).
`source.lag_days` overrides the class default; `source.lag_overrides[event_type]` overrides
further per event type (e.g. `{"withdrawn": 0}`).
"""

from __future__ import annotations

import datetime as dt
from typing import Literal

Kind = Literal["proposal", "opportunity"]

LAG_DAYS_BY_KIND: dict[Kind, int] = {"proposal": 14, "opportunity": 7}


def lag_days_for(
    kind: Kind,
    *,
    source_lag_days: int | None = None,
    lag_overrides: dict[str, int] | None = None,
    event_type: str | None = None,
) -> int:
    """Effective lag in days for a record or event of the given `kind`."""
    if event_type and lag_overrides and event_type in lag_overrides:
        return int(lag_overrides[event_type])
    if source_lag_days is not None:
        return int(source_lag_days)
    return LAG_DAYS_BY_KIND[kind]


def compute_public_at(
    published_at: dt.datetime,
    kind: Kind,
    *,
    source_lag_days: int | None = None,
    lag_overrides: dict[str, int] | None = None,
    event_type: str | None = None,
) -> dt.datetime:
    """`public_at = published_at + lag(source, event_type)` (docs/21 §3.10, §5.4)."""
    days = lag_days_for(
        kind, source_lag_days=source_lag_days, lag_overrides=lag_overrides, event_type=event_type
    )
    return published_at + dt.timedelta(days=days)
