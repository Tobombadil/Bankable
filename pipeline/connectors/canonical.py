"""Typed seam onto the canonical normalisers in `pipeline/normalize.py`.

`pipeline/normalize.py` is the Phase 2 prototype: it is pandas-heavy and untyped, and docs/04 E-1
has it moving under this layout in a later sprint. Connectors call it through these wrappers so
the connector layer stays `mypy --strict` clean and there is exactly one place to change when the
prototype is replaced.
The prototype is untyped, so every call into it is a `no-untyped-call` in a strict context; that
error code is disabled *here only*, which is exactly the boundary this module exists to hold.
"""

# mypy: disable-error-code="no-untyped-call"
from __future__ import annotations

from typing import Any

import pandas as pd

from pipeline import normalize as _n

#: Canonical proposal columns produced by the prototype normaliser (docs/21 §3.1).
CANONICAL_COLUMNS: list[str] = list(_n.CANONICAL_COLUMNS)


def classify_tech(raw: Any) -> tuple[str, str]:
    """Raw technology string -> (normalised technology token, proposal kind)."""
    result: tuple[str, str] = _n.classify_tech(raw)
    return result


def harmonise_status(source_key: str, ctx: dict[str, Any], status_map: dict[str, Any]) -> tuple[str, str]:
    """Source status inputs -> (canonical lifecycle/opportunity state, rule id) (docs/04 DA-5)."""
    result: tuple[str, str] = _n.harmonise_status(source_key, ctx, status_map)
    return result


def norm_name(value: Any) -> str | None:
    result: str | None = _n.norm_name(value)
    return result


def norm_org(value: Any) -> str | None:
    result: str | None = _n.norm_org(value)
    return result


def norm_state(value: Any) -> str | None:
    result: str | None = _n.norm_state(value)
    return result


def norm_county(value: Any) -> str | None:
    result: str | None = _n.norm_county(value)
    return result


def to_date(value: Any) -> pd.Timestamp | None:
    result: pd.Timestamp | None = _n.to_date(value)
    return result


def to_float(value: Any) -> float | None:
    result: float | None = _n.to_float(value)
    return result


def normalize_iso(
    df: pd.DataFrame, source_key: str, status_map: dict[str, Any], retrieved_at: str
) -> pd.DataFrame:
    result: pd.DataFrame = _n.normalize_iso(df, source_key, status_map, retrieved_at)
    return result


def normalize_eia(df: pd.DataFrame, status_map: dict[str, Any], retrieved_at: str) -> pd.DataFrame:
    result: pd.DataFrame = _n.normalize_eia(df, status_map, retrieved_at)
    return result


def load_status_map(path: Any = None) -> dict[str, Any]:
    result: dict[str, Any] = _n.load_status_map(path) if path else _n.load_status_map()
    return result
