"""Which deployment environment this process runs in, for the fail-closed configuration checks.

`ENVIRONMENT` is the one switch (Compose sets it from the env file deploy.sh writes; unset means a
laptop, CI or the test suite). A check that may fall back to a development default does so only in
the environments below; anything else (`staging`, `production`, `preview`, a typo) must carry the
real setting or the process refuses to start. `services/api/auth.py::session_secret` introduced
the rule for `SESSION_SECRET`; `services/posture.py::platform_posture` applies it to
`PLATFORM_POSTURE` (devops audit 2026-09-30 F3). Standard library only, so `pipeline/` can use it.
"""

from __future__ import annotations

import os

#: `ENVIRONMENT` values that may run on development defaults.
DEV_ENVIRONMENTS = frozenset({"", "dev", "development", "local", "test", "ci"})


def current_environment() -> str:
    return os.environ.get("ENVIRONMENT", "").strip().lower()


def is_dev_environment() -> bool:
    return current_environment() in DEV_ENVIRONMENTS
