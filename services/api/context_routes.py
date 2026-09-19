"""Backward-compatible re-export point (ADR 0008, 2026-09-18).

The built-infrastructure context layer's route (`GET /v1/context/plants/geo`) and clustering logic
(index cache, feature/cluster builders) moved into `services/api/assets.py` as the `power_plant`
case of the generalised asset endpoints -- `BuiltPlant` (renamed `asset`, migration 0009) gained
columns no `built_plant`-shaped code could read (`unit_count`/`commissioned_year` replace
`generator_count`/`earliest_operating_year`), so the old implementation could not simply keep
running against the renamed table.

This module is kept, not deleted, only because `web/test_map_layers.py` still imports
`TECHNOLOGY_VOCAB` from here; nothing in `services/api` imports it any more (`services/api/assets.py`
defines its own copy of the same constant), and this module registers no router -- `services/api/app.py`
mounts `services.api.assets.router` directly.
"""

from __future__ import annotations

from services.api.assets import TECHNOLOGY_VOCAB

__all__ = ["TECHNOLOGY_VOCAB"]
