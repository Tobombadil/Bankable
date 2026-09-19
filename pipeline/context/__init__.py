"""Built-infrastructure context layer (docs/00-PLAN.md decision 2026-09-14, docs/21 §3.20).

Distinct from `pipeline/connectors/*`: a context source has no lifecycle, no events, no
entity resolution against proposal/opportunity rows. It is drawn beneath the proposals map as
plain "what already exists" reference. `eia_plants.py` is the first (and, this sprint, only)
source: the EIA-860M "Operating" sheet.
"""

from __future__ import annotations
