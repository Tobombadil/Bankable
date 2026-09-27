"""Names an asset's register text must not print, because they name an organisation that is not
public (docs/00-PLAN.md 2026-09-27, "Organisations can be taken down", left open: "`asset.
operator_name` (register text that can still name a taken-down organisation)").

The organisation-takedown lane made a non-public organisation's *rows* disappear (`services/api/
visibility.py`'s organisation arm: sponsor/issuer embeds `null`, owner edges dropped, every
`/v1/organizations` route a 404). An asset is not the organisation's row -- the register fact that a
plant or pipeline exists stays public -- but it carries the source's own company strings:
`asset.operator_name`, and the attribute keys the registries fill (`operator_raw`, `owner_raw`,
`atlas_operator_name`, `attributes.phmsa.operator_name`/`operator_id`, the anticipated `operator`).
Printing those undoes the takedown by name, so on every asset surface (all of which are non-admin:
ADR 0008 gives assets no tier and there is no admin asset read) they are withheld, silently -- the
same reading docs/21 §8 item 3 gives the edges, where a placeholder would itself disclose.

**When an asset's operator name is withheld.** Either of two links to a non-public organisation:

1. the asset has an `asset_owner` edge with `role = operator` to it -- the resolved identity, which
   holds even where the register spells the company differently; or
2. `org_key(asset.operator_name)` equals `org_key` of its `name_canonical` or of one of its
   `organization_alias` rows -- the same single key every loader resolves organisations with
   (docs/22 §16), so the name is withheld on an asset whose edge was never written.

Then `operator_name` is `null`, and the operator-bearing attribute keys above are dropped. Separately,
**any** attribute string whose `org_key` is a withheld key is dropped wherever it sits (an `owner_raw`
naming a taken-down owner, an RFS `company_name`), and the operator name cannot be searched: `GET
/v1/assets?q=` does not match an asset through a withheld `operator_name` (`operator_name_searchable`).

**Owner edges' raw spellings** (2026-09-27, lane E16). An `owners[]` edge to a non-public organisation
is dropped (above). An edge to a *public* organisation stays, but its `owner_name_raw` is the register's
own spelling, and that spelling can share an `org_key` with a non-public organisation (a register that
spells the public owner the way the taken-down one is known, or a resolver that linked the string to
the public row). Printing it names the taken-down organisation. So, by the same conservative rule as the
operator name ("a key that matches both a hidden and a public organisation is withheld"), an
`owner_name_raw` whose key is a withheld key is `null` on every surface (`owner_name_raw`); the edge
stays and shows the public organisation's canonical name and link, which is what it points to. The
admin surfaces read the row, not this serializer.

**Cost.** One query (the non-public organisations) when nothing is taken down, which is the normal
state. When something is, the aliases and edges of those organisations (a handful of rows) and the
distinct register spellings whose key matches; the last is a scan of `asset.operator_name`, so it is
cached per process against the takedown set and the asset table's `(count, max(last_changed))`
fingerprint (`services/api/assets.py`'s index caches use the same fingerprint) and the per-string
`org_key` is memoised. A republish changes the takedown set, so every surface -- including the cached
line index, whose features are built per request from this -- shows the name again at once.
"""

from __future__ import annotations

import threading
import uuid as _uuid
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

import sqlalchemy as sa
from sqlalchemy import ColumnElement, func, select
from sqlalchemy.orm import Session

from pipeline.normalize import org_key
from services.db.models import Asset, AssetOwner, Organization, OrganizationAlias

#: Top-level attribute keys that hold the operator's name as the register spelt it.
OPERATOR_ATTRIBUTE_KEYS: tuple[str, ...] = ("operator", "operator_raw", "atlas_operator_name")
#: Nested blocks keyed to the operator (`services/ingest/enrich.py` joins PHMSA by operator), and the
#: keys in them that identify it. The block's mileage and incident figures are not names and stay.
NESTED_OPERATOR_KEYS: Mapping[str, tuple[str, ...]] = {"phmsa": ("operator_name", "operator_id")}


@lru_cache(maxsize=65536)
def _key(text: str) -> str:
    return org_key(text)


@dataclass(frozen=True)
class WithheldNames:
    """The non-public organisations as the asset surfaces need them. `NONE` when every
    organisation is public; every method is then a no-op."""

    keys: frozenset[str] = frozenset()
    operator_spellings: frozenset[str] = frozenset()
    operator_edge_asset_ids: frozenset[_uuid.UUID] = frozenset()

    @property
    def empty(self) -> bool:
        return not self.keys and not self.operator_edge_asset_ids

    def names_withheld(self, text: str | None) -> bool:
        """True when `text` is a spelling of a non-public organisation (by `org_key`)."""
        return bool(text) and not self.empty and _key(str(text)) in self.keys

    def operator_withheld(self, asset_id: Any, operator_name: str | None) -> bool:
        if self.empty:
            return False
        if _as_uuid(asset_id) in self.operator_edge_asset_ids:
            return True
        return self.names_withheld(operator_name)

    def operator_name(self, asset_id: Any, operator_name: str | None) -> str | None:
        return None if self.operator_withheld(asset_id, operator_name) else operator_name

    def owner_name_raw(self, text: str | None) -> str | None:
        """An owner edge's raw register spelling, or `None` when it spells a non-public organisation
        (module docstring, "Owner edges' raw spellings") -- whichever organisation the edge points to."""
        return None if self.names_withheld(text) else text

    def attributes(
        self, asset_id: Any, operator_name: str | None, attributes: Mapping[str, Any] | None
    ) -> dict[str, Any]:
        """`attributes` without the strings that name a non-public organisation (module docstring)."""
        attrs = dict(attributes or {})
        if self.empty or not attrs:
            return attrs
        scrubbed: dict[str, Any] = self._scrub_mapping(attrs)
        if self.operator_withheld(asset_id, operator_name):
            for key in OPERATOR_ATTRIBUTE_KEYS:
                scrubbed.pop(key, None)
            for block, keys in NESTED_OPERATOR_KEYS.items():
                nested = scrubbed.get(block)
                if isinstance(nested, dict):
                    scrubbed[block] = {k: v for k, v in nested.items() if k not in keys}
        return scrubbed

    def _scrub_mapping(self, value: Mapping[str, Any]) -> dict[str, Any]:
        return {
            k: self._scrub(v) for k, v in value.items() if not (isinstance(v, str) and self.names_withheld(v))
        }

    def _scrub(self, value: Any) -> Any:
        if isinstance(value, Mapping):
            return self._scrub_mapping(value)
        if isinstance(value, list):
            return [self._scrub(v) for v in value if not (isinstance(v, str) and self.names_withheld(v))]
        return value

    def operator_name_searchable(self) -> ColumnElement[bool]:
        """SQL: the asset's `operator_name` may be matched by a search (not withheld)."""
        if self.empty:
            return sa.true()
        clauses: list[ColumnElement[bool]] = []
        if self.operator_spellings:
            clauses.append(Asset.operator_name.in_(sorted(self.operator_spellings)))
        if self.operator_edge_asset_ids:
            clauses.append(Asset.id.in_(sorted(self.operator_edge_asset_ids, key=str)))
        return sa.not_(sa.or_(*clauses)) if clauses else sa.true()


NONE = WithheldNames()

_spellings_lock = threading.Lock()
_spellings_cache: tuple[tuple[Any, ...], frozenset[str]] | None = None


def _as_uuid(value: Any) -> _uuid.UUID | None:
    if isinstance(value, _uuid.UUID):
        return value
    try:
        return _uuid.UUID(str(value))
    except ValueError:
        return None


def _operator_spellings(db: Session, keys: frozenset[str]) -> frozenset[str]:
    """Every distinct `asset.operator_name` whose `org_key` is in `keys`, cached (module docstring)."""
    global _spellings_cache
    count, latest = db.execute(select(func.count(Asset.id), func.max(Asset.last_changed))).one()
    fingerprint = (tuple(sorted(keys)), count, str(latest))
    with _spellings_lock:
        cached = _spellings_cache
    if cached is not None and cached[0] == fingerprint:
        return cached[1]
    names = db.scalars(select(Asset.operator_name).where(Asset.operator_name.is_not(None)).distinct())
    spellings = frozenset(name for name in names if name and _key(name) in keys)
    with _spellings_lock:
        _spellings_cache = (fingerprint, spellings)
    return spellings


def reset_cache() -> None:
    global _spellings_cache
    with _spellings_lock:
        _spellings_cache = None


def withheld_names(db: Session) -> WithheldNames:
    """The current `WithheldNames`; `NONE` (one query) when every organisation is public."""
    hidden = db.execute(
        select(Organization.id, Organization.name_canonical).where(Organization.publish_state != "public")
    ).all()
    if not hidden:
        return NONE
    ids = [row[0] for row in hidden]
    spellings = [row[1] for row in hidden]
    # A row merged into a withheld organisation is one of its spellings too (docs/22 §20: a merge
    # retires the absorbed row, and its edges that collided stay on it), followed down any chain.
    frontier = list(ids)
    while frontier:
        absorbed = db.execute(
            select(Organization.id, Organization.name_canonical).where(
                Organization.merged_into_id.in_(frontier), Organization.id.not_in(ids)
            )
        ).all()
        frontier = [row[0] for row in absorbed]
        ids += frontier
        spellings += [row[1] for row in absorbed]
    spellings += list(
        db.scalars(select(OrganizationAlias.alias).where(OrganizationAlias.organization_id.in_(ids)))
    )
    keys = frozenset(k for k in (_key(s) for s in spellings if s) if k)
    edge_assets = frozenset(
        db.scalars(
            select(AssetOwner.asset_id).where(
                AssetOwner.organization_id.in_(ids), AssetOwner.role == "operator"
            )
        )
    )
    return WithheldNames(
        keys=keys,
        operator_spellings=_operator_spellings(db, keys),
        operator_edge_asset_ids=edge_assets,
    )


__all__ = [
    "NESTED_OPERATOR_KEYS",
    "NONE",
    "OPERATOR_ATTRIBUTE_KEYS",
    "WithheldNames",
    "reset_cache",
    "withheld_names",
]
