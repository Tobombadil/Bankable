"""Cross-source entity resolution applied to the services/db store (docs/22 §13).

`pipeline/resolve.py` and `pipeline/normalize.py` produce candidate clusters and scored pairs
from raw connector output; this package takes those candidates and makes the store hold one
`proposal`/`organization` row per real-world thing, reversibly, per `docs/21-data-model.md` §6.3
and §6.4. It never fetches or parses source data itself.
"""

from __future__ import annotations
