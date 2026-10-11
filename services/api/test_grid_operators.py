"""`services/api/grid_operators.py`: one name per grid operator at read time (lane P, 2026-10-10)."""

from __future__ import annotations

from pipeline.normalize import EIA_BA_ISO_TOKENS, iso_token_from_eia_ba
from services.api.grid_operators import ISO_ALIASES, canonical_iso, iso_filter_values


def test_the_alias_table_is_the_connectors_own() -> None:
    """One table, in one place: what the EIA-860M connector maps at ingest is what the API reads."""
    assert ISO_ALIASES is EIA_BA_ISO_TOKENS
    for code in ISO_ALIASES:
        assert canonical_iso(code) == iso_token_from_eia_ba(code)


def test_iso_balancing_authorities_read_as_their_iso_and_others_as_themselves() -> None:
    assert canonical_iso("ERCO") == "ERCOT"
    assert canonical_iso("CISO") == "CAISO"
    assert canonical_iso("NYIS") == "NYISO"
    assert canonical_iso("ISNE") == "ISONE"
    assert canonical_iso("SWPP") == "SPP"
    assert canonical_iso("ERCOT") == "ERCOT"
    assert canonical_iso("TVA") == "TVA"  # a utility balancing authority is not an ISO
    assert canonical_iso(None) is None and canonical_iso("") == ""


def test_a_filter_value_matches_every_stored_spelling_of_its_grid() -> None:
    assert iso_filter_values(["ERCOT"]) == ["ERCO", "ERCOT"]
    assert iso_filter_values(["ERCO"]) == ["ERCO", "ERCOT"]
    assert iso_filter_values(["SPP", "CAISO"]) == ["CAISO", "CISO", "SPP", "SWPP"]
    assert iso_filter_values(["PJM"]) == ["PJM"]  # the BA code and the token are the same word
    assert iso_filter_values(["TVA"]) == ["TVA"]
