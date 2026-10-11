"""One name per grid operator at read time (2026-10-10, lane P).

`proposal.iso` holds the market-operator token the ISO queue connectors write (`ERCOT`, `NYISO`,
`CAISO`, ...) and, on EIA-860M rows loaded before 2026-09-27, the EIA balancing-authority code of
the same grid (`ERCO`, `NYIS`, `CISO`, `ISNE`, `SWPP`, ...): one grid under two names, so
`?iso=ERCOT` missed the `ERCO` rows and a company's pipeline listed ERCOT twice. The connector has
mapped the codes since lane E14 (`pipeline/normalize.py::iso_token_from_eia_ba`); stored rows and
the `normalised` projections of older links keep what was loaded, and are not rewritten.

The alias table is the one the connector uses, `pipeline.normalize.EIA_BA_ISO_TOKENS` (the seven
RTO/ISO balancing authorities, from EIA's balancing-authority reference table), not a copy: a code
added there is read here too. Any other balancing authority (a utility such as TVA or SOCO) is not
an ISO and keeps its own code, as the connector leaves it.

Two readers: the list's `iso` filter matches every stored spelling of the grid it names
(`iso_filter_values`), and the organisation pipeline groups by `canonical_iso`, so a count there is
the total of the `iso=` link beside it.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from pipeline.normalize import EIA_BA_ISO_TOKENS

#: EIA balancing-authority code -> market-operator token. The connector's own table (module docstring).
ISO_ALIASES: Mapping[str, str] = EIA_BA_ISO_TOKENS


def canonical_iso(value: str | None) -> str | None:
    """The operator token for a stored `iso` value: an ISO/RTO balancing-authority code becomes the
    token the queue connectors write (`ERCO` -> `ERCOT`); anything else is returned unchanged."""
    if not value:
        return value
    return ISO_ALIASES.get(value, value)


def iso_filter_values(values: Iterable[str]) -> list[str]:
    """Every stored spelling of the grids `values` name, for `Proposal.iso IN (...)`: each value's
    operator token plus every balancing-authority code that maps to it (`ERCOT` and `ERCO` both
    select `ERCOT` and `ERCO`). A value no alias names matches only itself, exactly as before."""
    tokens = {canonical_iso(v) or v for v in values}
    codes = {code for code, token in ISO_ALIASES.items() if token in tokens}
    return sorted(tokens | codes)
