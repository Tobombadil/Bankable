"""The sites kill switch (owner, 2026-10-10): one environment variable, `SITES_ENABLED`, read the way
`PLATFORM_POSTURE` and `MAP_TILE_URL` are read (`os.environ.get` at the point of use, stripped,
case-folded; `services/posture.py`).

On by default. `0`, `false`, `off` or `no` turns sites off for readers: the API answers
`GET /v1/sites/{id}` with the `404` an unknown id gets and serves `site: null` on every proposal,
so the web's "At this site" panel and `/sites/{id}` pages disappear with it; the web also checks
it before asking. It takes effect when the API and web processes next start (an environment
change, not a deploy). The builder keeps running either way, so turning sites back on serves
current data. The owner's rule (2026-10-10): if the 100-record hand check finds more than about
one site in ten wrong, sites are turned off for the beta.
"""

from __future__ import annotations

import os

ENV_VAR = "SITES_ENABLED"
_OFF = frozenset({"0", "false", "off", "no"})


def sites_enabled() -> bool:
    return os.environ.get(ENV_VAR, "").strip().lower() not in _OFF
