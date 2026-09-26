"""The platform posture: `commercial` or `noncommercial` (`docs/26-platform-posture.md`).

Owner decision, 2026-09-25 (`docs/00-PLAN.md` decisions log), verbatim: "Let's move forward as a
noncommercial platform for now for maximum and best data access. Then decide how to proceed once
we're done."

The posture is one environment variable, `PLATFORM_POSTURE`, read the way `SENDER_LEGAL_NAME`
and `MAP_TILE_URL` are read (`os.environ.get` at the point of use, stripped, case-folded) — there
is no settings object in this codebase and this module does not introduce one. It **defaults to
`commercial`**, so a deployment that never sets it behaves exactly as it did before this module
existed; an unrecognised value fails closed to `commercial` too, so a typo cannot widen
publication. Flipping the value takes effect on the next process start: the API, the ingest
runner and the workers each read it once at import (`services/api/visibility.py`,
`pipeline/connectors/registry.py`), which is deliberate — a running process must not have half
its queries under one posture and half under another.

What the posture changes is exactly one thing: whether the reuse class `noncommercial`
(`services.db.models.REUSE_CLASSES`) counts as publishable. Under `commercial`, the publishable
classes are `open` and `attribution`, as they always were. Under `noncommercial`, `noncommercial`
joins them. `restricted` and `unknown` are gated under both. Every gate in the codebase — the
connector registry, the loader, the API visibility predicate, the admin publish checks, the
coverage statement, the social editorial rule and the ORM's own `Licence.is_publishable_class` —
derives its class set from `publishable_reuse_classes` here, so they cannot disagree
(`tests/test_platform_posture.py`).

The two helpers take the posture as an argument rather than reading the environment themselves,
so their behaviour is a pure function of their input and `services/test_posture.py` covers every
branch without touching `os.environ`; `platform_posture()` is the only environment read.
"""

from __future__ import annotations

import os

#: The two postures. The order is not a ranking.
PLATFORM_POSTURES: tuple[str, ...] = ("commercial", "noncommercial")
#: What a deployment gets when `PLATFORM_POSTURE` is unset, empty or unrecognised.
DEFAULT_POSTURE = "commercial"
ENV_VAR = "PLATFORM_POSTURE"

#: The classes that may leave the building under each posture (docs/21 §8; docs/13 §6).
_PUBLISHABLE_BY_POSTURE: dict[str, tuple[str, ...]] = {
    "commercial": ("open", "attribution"),
    "noncommercial": ("open", "attribution", "noncommercial"),
}
#: The full vocabulary, repeated here rather than imported from `services.db.models` so that this
#: module stays importable from `pipeline/` without pulling SQLAlchemy in; `tests/
#: test_platform_posture.py` pins the two against each other.
_ALL_REUSE_CLASSES: tuple[str, ...] = ("open", "attribution", "noncommercial", "restricted", "unknown")

_STATEMENTS: dict[str, str] = {
    "commercial": (
        "This platform operates under a commercial posture: only sources whose terms permit "
        "commercial reuse are published, and sources that permit noncommercial reuse only are withheld."
    ),
    "noncommercial": (
        "This platform operates under a noncommercial posture: sources that permit noncommercial "
        "reuse are published with attribution; they will be withdrawn if the posture changes."
    ),
}


def normalise_posture(raw: str | None) -> str:
    """The posture a raw setting value means. Anything that is not exactly one of
    `PLATFORM_POSTURES` after stripping and case-folding is `DEFAULT_POSTURE` — fail closed."""
    value = (raw or "").strip().lower()
    return value if value in PLATFORM_POSTURES else DEFAULT_POSTURE


def platform_posture() -> str:
    """The posture in force for this process, from `PLATFORM_POSTURE`."""
    return normalise_posture(os.environ.get(ENV_VAR))


def publishable_reuse_classes(posture: str) -> tuple[str, ...]:
    """The reuse classes that may be published under `posture`. An unrecognised posture is
    treated as `DEFAULT_POSTURE` here too, so a caller that bypasses `normalise_posture` still
    fails closed."""
    return _PUBLISHABLE_BY_POSTURE.get(posture, _PUBLISHABLE_BY_POSTURE[DEFAULT_POSTURE])


def gated_reuse_classes(posture: str) -> tuple[str, ...]:
    """The complement of `publishable_reuse_classes` within the vocabulary."""
    publishable = publishable_reuse_classes(posture)
    return tuple(c for c in _ALL_REUSE_CLASSES if c not in publishable)


def posture_statement(posture: str) -> str:
    """The one sentence the public surfaces print (`GET /v1/health`, `/about#tiers`,
    `/methodology`). Derived from the setting; the pages carry no prose of their own."""
    return _STATEMENTS[normalise_posture(posture)]
