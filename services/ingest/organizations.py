"""Organisation-level loaders: GLEIF Level 2 parent links, the curated alias file and the curated
merge file (docs/21 §3.5, §3.6, §3.23; docs/22 §17, §20.5) -> `organization.parent_org_id` /
`organization_alias` / reversible `merged` events.

Three loaders, all idempotent, all one pass over the tables they touch. `load_merges` is described
at its definition; the other two follow.

**`load_gleif_parents(session, df)`** applies the parquet `pipeline/context/gleif.py` writes
(`global.gleif.lei`, CC0) and sets, for every organisation it can match, `parent_org_id`,
`parent_source_id = global.gleif.lei`, `parent_as_of` (the relationship's own start date) and
`ids["lei"]` on both child and parent.

*The match rule, and why it is this narrow.* A candidate is a GLEIF legal entity whose
`pipeline.normalize.org_key` equals an organisation's — the same single key the resolver, the
ownership index and the midstream parent lookup use (docs/22 §16), over `name_canonical` and every
`organization_alias.alias`. On top of that, two gates, both measured on the 272-pair labelled
census in `data/eval/gleif_matches.csv` (docs/22 §17.2):

1. **Country evidence.** `organization.country` is `US` on every row today (the ownership and
   midstream loaders hard-code it), so it carries no information. The organisation's *evidence*
   does: the jurisdictions of the proposals it sponsors and the countries of the assets it owns or
   operates. The GLEIF entity's legal-address country must be one of them. Without this gate the
   key alone matches a US operator against its same-named foreign affiliate — `Ameresco, Inc.`
   against `AMERESCO LIMITED` (GB), `Cargill Inc.` against `CARGILL PLC`, `SPIRE INC` against
   `SPIRE LIMITED` of Trinidad and Tobago — and the gate removes **every one** of the 7 labelled
   `different` pairs, taking precision from 0.948 to **0.985** at the cost of one true match
   (a US-NY queue sponsor spelled `Brookfield Renewable Power` against the Canadian
   `BROOKFIELD RENEWABLE POWER INC.`).
2. **One GLEIF entity per key.** If two GLEIF entities survive the country gate on one key, the
   loader cannot tell which is meant and links neither. (It fires on the bare string
   `Air Products`, which matches a US, a Belgian and a French entity; the country gate already
   leaves one, so the count is 0 today — the guard is there for the day it is not.)

Relationship rows are filtered to `relationship_status = ACTIVE`, `registration_status =
PUBLISHED` and `child_entity_status = ACTIVE` before any of that: a lapsed registration is a
record GLEIF no longer stands behind, and an inactive relationship is a parent that used to be.
`IS_DIRECTLY_CONSOLIDATED_BY` wins over `IS_ULTIMATELY_CONSOLIDATED_BY` for the same child, because
`parent_org_id` is documented as the **direct** accounting parent (docs/21 §3.23); the ultimate one
is used only when GLEIF publishes no direct record.

*Against the curated file.* `services/ingest/midstream.py::load_parents` keeps its rows; this
loader overwrites a curated link only for an organisation it matches, and `load_parents` in turn
refuses to overwrite a GLEIF link (its `children_deferred_to_gleif` count), so the two are
order-independent and a re-run of either changes nothing. Measured 2026-09-20: GLEIF covers none of
the nine Tallgrass children the curated file links — five of them hold an LEI, none is the start
node of any Level 2 relationship record — so all nine curated rules stand (docs/22 §17.3).

**`load_aliases(session, path)`** applies `data/vendored/organizations/aliases.yaml`
(`curated.organization_aliases`): one `organization_alias` row per rule, `kind="filing_spelling"`,
`confidence=1.0` (each rule cites a filing, so the identity is stated, not inferred), carrying the
rule's own `source_url` and `retrieved_at`. A rule whose canonical organisation does not exist yet
is **inert, not an error** — it is reported and re-applies the moment such a row is loaded, which
is the whole point of writing the rule down before the data arrives. A rule whose alias already
resolves, under `org_key`, to an organisation *other* than its canonical one is reported as a
conflict and skipped: merging two live organisations is a `services/resolve/merge.py` decision with
a reversible event behind it, never something an alias loader does silently. The conflict line
names that organisation and counts the `asset_owner` edges and proposals hanging off it — the
footprint a merge event would have to move — because on a store built by `web/dev_up.py` this is
the *normal* outcome, not the exception: the edge and sponsor loaders run first and have already
made an organisation out of the very spelling the rule names (docs/22 §19.3 measures 7 of 10 rules
in that state on the 2026-09-26 dev store). The rule is still worth writing: it carries the
citation a merge needs, it re-applies the moment the absorbed row is merged away, and the report
is the queue of merges to make.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import pathlib
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

import pandas as pd
import sqlalchemy as sa
import yaml
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from pipeline.connectors.registry import Registry
from pipeline.normalize import org_key
from services.db.models import (
    Asset,
    AssetOwner,
    Organization,
    OrganizationAlias,
    Proposal,
    Source,
    new_uuid,
)
from services.db.session import get_engine, get_sessionmaker, init_db
from services.ids import public_id as make_public_id
from services.ids import slugify
from services.ingest.loader import upsert_licence_and_source
from services.resolve.merge import merge_organization

log = logging.getLogger(__name__)

ROOT = pathlib.Path(__file__).resolve().parents[2]
DEFAULT_DB_PATH = pathlib.Path("web/.data/dev.db")
DEFAULT_ALIASES_PATH = ROOT / "data" / "vendored" / "organizations" / "aliases.yaml"
DEFAULT_MERGES_PATH = ROOT / "data" / "vendored" / "organizations" / "merges.yaml"
DEFAULT_GLEIF_PARQUET = ROOT / "data" / "normalized" / "context" / "global.gleif.lei.parents.parquet"

GLEIF_SOURCE_ID = "global.gleif.lei"
ALIASES_SOURCE_ID = "curated.organization_aliases"

#: Direct beats ultimate for `parent_org_id` (module docstring); the order is the preference.
RELATIONSHIP_PREFERENCE: tuple[str, ...] = (
    "IS_DIRECTLY_CONSOLIDATED_BY",
    "IS_ULTIMATELY_CONSOLIDATED_BY",
)


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _text(value: object) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    return str(value).strip()


def _date(value: object) -> dt.date | None:
    if value is None or value is pd.NaT:
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if isinstance(value, float) and pd.isna(value):
        return None
    ts = pd.Timestamp(value)
    return None if pd.isna(ts) else ts.date()


# ------------------------------------------------------------------ shared organisation indexes
def org_key_multimap(session: Session) -> dict[str, list[Organization]]:
    """`org_key(name)` -> every live organisation with that key, over `name_canonical` and every
    alias. `services/ingest/ownership.py::_build_norm_org_index` keeps only the first organisation
    per key, which is right for "resolve this owner string to one row"; a parent loader has to see
    the collision to decide whether it may act on it, so it builds its own multimap."""
    index: dict[str, list[Organization]] = defaultdict(list)
    orgs: dict[Any, Organization] = {}
    for org in session.scalars(select(Organization).where(Organization.merged_into_id.is_(None))):
        orgs[org.id] = org
        key = org_key(org.name_canonical)
        if key and org not in index[key]:
            index[key].append(org)
    for alias, org_id in session.execute(
        select(OrganizationAlias.alias, OrganizationAlias.organization_id)
    ).all():
        alias_org = orgs.get(org_id)
        key = org_key(alias)
        if alias_org is not None and key and alias_org not in index[key]:
            index[key].append(alias_org)
    return dict(index)


def evidence_countries(session: Session) -> dict[Any, set[str]]:
    """`organization.id` -> the countries its own evidence names: the two-letter prefix of every
    `proposal.jurisdiction` it sponsors, and the `asset.country` of every asset it owns or operates.

    This is the country gate's input (module docstring). It is deliberately *not*
    `organization.country`, which the ownership and midstream loaders set to a hard-coded `US` for
    every row they create -- including the 2,198 GB proposals' sponsors, which is exactly the
    population the gate has to get right."""
    out: dict[Any, set[str]] = defaultdict(set)
    for org_id, jurisdiction in session.execute(
        select(Proposal.sponsor_org_id, Proposal.jurisdiction).where(Proposal.sponsor_org_id.is_not(None))
    ).all():
        if jurisdiction:
            out[org_id].add(str(jurisdiction)[:2].upper())
    for org_id, country in session.execute(
        select(AssetOwner.organization_id, Asset.country).join(Asset, Asset.id == AssetOwner.asset_id)
    ).all():
        if country:
            out[org_id].add(str(country).upper())
    return dict(out)


def _get_or_create_org(
    session: Session,
    index: dict[str, list[Organization]],
    name: str,
    *,
    source: Source,
    now: dt.datetime,
    source_url: str,
    alias_kind: str = "legal_name",
) -> tuple[Organization, bool]:
    """The organisation for `name`, created with a `legal_name` alias when nothing matches. Where
    the key already holds several organisations the first is used — for a GLEIF *parent* that is
    safe, because the alternative (creating yet another row for a name that already collides) makes
    the collision worse; the *child* side never reaches here, it goes through the gates."""
    key = org_key(name)
    existing = index.get(key)
    if existing:
        return existing[0], False
    org_id = new_uuid()
    slug = slugify(name)
    if slug in set(session.scalars(select(Organization.slug))):
        slug = f"{slug}-{make_public_id('org', org_id)[-6:].lower()}"
    org = Organization(
        id=org_id,
        public_id=make_public_id("org", org_id),
        slug=slug,
        name_canonical=name.strip(),
        name_normalised=name.strip().lower(),
        type="other",
        country="US",
    )
    session.add(org)
    session.flush()
    session.add(
        OrganizationAlias(
            organization_id=org.id,
            alias=name.strip(),
            alias_normalised=name.strip().lower(),
            kind=alias_kind,
            source_id=source.id,
            source_url=source_url,
            retrieved_at=now,
            licence_id=source.licence_id,
            confidence=1.0,
            created_by="pipeline",
        )
    )
    index.setdefault(key, []).append(org)
    return org, True


def _set_identifier(org: Organization, name: str, value: str) -> None:
    """Write `ids[name]` without mutating the mapped dict in place (SQLAlchemy does not see an
    in-place change to a JSON column unless the attribute is reassigned)."""
    ids = dict(org.ids or {})
    if ids.get(name) == value:
        return
    ids[name] = value
    org.ids = ids


# ----------------------------------------------------------------------------- GLEIF parents
@dataclass
class GleifParentsLoadResult:
    rows_seen: int = 0
    rows_after_status_filter: int = 0
    gleif_children: int = 0
    candidate_keys: int = 0
    candidate_pairs: int = 0
    rejected_country_gate: int = 0
    rejected_ambiguous_gleif: int = 0
    parents_created: int = 0
    parents_matched: int = 0
    children_linked: int = 0
    children_unchanged: int = 0
    children_relinked_from_curated: int = 0
    self_parent_skipped: int = 0
    by_relationship_type: dict[str, int] = field(default_factory=dict)
    linked: dict[str, list[str]] = field(default_factory=dict)

    def as_report(self) -> dict[str, object]:
        return {
            "rows_seen": self.rows_seen,
            "rows_after_status_filter": self.rows_after_status_filter,
            "gleif_children": self.gleif_children,
            "candidate_keys": self.candidate_keys,
            "candidate_pairs": self.candidate_pairs,
            "rejected_country_gate": self.rejected_country_gate,
            "rejected_ambiguous_gleif": self.rejected_ambiguous_gleif,
            "parents_created": self.parents_created,
            "parents_matched": self.parents_matched,
            "children_linked": self.children_linked,
            "children_unchanged": self.children_unchanged,
            "children_relinked_from_curated": self.children_relinked_from_curated,
            "self_parent_skipped": self.self_parent_skipped,
            "by_relationship_type": dict(sorted(self.by_relationship_type.items())),
            # A sample: a full run links hundreds of children and `web/dev_up.py` logs this as one
            # line. The whole mapping stays on the dataclass for a caller that wants it.
            "linked_sample": {k: sorted(v) for k, v in sorted(self.linked.items())[:5]},
        }


def _active_relationships(df: pd.DataFrame, result: GleifParentsLoadResult) -> pd.DataFrame:
    """Live consolidation rows only, one per child, direct preferred over ultimate."""
    if not len(df):
        return df
    live = df[
        (df["relationship_status"].astype(str) == "ACTIVE")
        & (df["registration_status"].astype(str) == "PUBLISHED")
        & (df["child_entity_status"].astype(str) == "ACTIVE")
        & (df["relationship_type"].isin(RELATIONSHIP_PREFERENCE))
    ].copy()
    result.rows_after_status_filter = len(live)
    if not len(live):
        return live
    order = {name: i for i, name in enumerate(RELATIONSHIP_PREFERENCE)}
    live["_rank"] = live["relationship_type"].map(order)
    live = live.sort_values(["child_lei", "_rank"], kind="stable").drop_duplicates("child_lei")
    return live.drop(columns="_rank")


def load_gleif_parents(
    session: Session, df: pd.DataFrame, *, manifest_version: str = ""
) -> GleifParentsLoadResult:
    registry = Registry()
    entry = registry.get(GLEIF_SOURCE_ID)
    source = upsert_licence_and_source(session, entry, manifest_version or registry.version)

    result = GleifParentsLoadResult(rows_seen=len(df))
    if not len(df):
        session.commit()
        return result

    live = _active_relationships(df, result)
    result.gleif_children = len(live)
    if not len(live):
        session.commit()
        return result

    by_key: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in live.to_dict("records"):
        key = org_key(_text(row.get("child_legal_name")))
        if key:
            by_key[key].append(row)

    index = org_key_multimap(session)
    countries = evidence_countries(session)
    now = utcnow()
    curated_source_id = "curated.organization_parents"

    for key, gleif_rows in by_key.items():
        orgs = index.get(key)
        if not orgs:
            continue
        result.candidate_keys += 1
        result.candidate_pairs += len(orgs) * len(gleif_rows)
        for org in orgs:
            evidence = countries.get(org.id, set())
            allowed = [r for r in gleif_rows if _text(r.get("child_country")).upper() in evidence]
            if not allowed:
                result.rejected_country_gate += 1
                continue
            if len({_text(r["child_lei"]) for r in allowed}) > 1:
                result.rejected_ambiguous_gleif += 1
                continue
            row = allowed[0]
            parent_name = _text(row.get("parent_legal_name"))
            if not parent_name:
                continue
            parent, created = _get_or_create_org(
                session,
                index,
                parent_name,
                source=source,
                now=now,
                source_url=_text(row.get("source_url")) or source.url,
            )
            if created:
                result.parents_created += 1
            else:
                result.parents_matched += 1
            if parent.id == org.id:
                # GLEIF's own record says the child consolidates into itself only when our two
                # names collapsed onto one organisation; never write a self-parent.
                result.self_parent_skipped += 1
                continue
            _set_identifier(org, "lei", _text(row["child_lei"]))
            _set_identifier(parent, "lei", _text(row["parent_lei"]))
            as_of = (
                _date(row.get("period_start"))
                or _date(row.get("accounting_period_end"))
                or _date(row.get("last_update_date"))
            )
            if (
                org.parent_org_id == parent.id
                and org.parent_source_id == source.id
                and org.parent_as_of == as_of
            ):
                result.children_unchanged += 1
                continue
            if org.parent_source_id == curated_source_id:
                result.children_relinked_from_curated += 1
            org.parent_org_id = parent.id
            org.parent_source_id = source.id
            org.parent_as_of = as_of
            org.last_changed = now
            result.children_linked += 1
            rel_type = _text(row.get("relationship_type"))
            result.by_relationship_type[rel_type] = result.by_relationship_type.get(rel_type, 0) + 1
            result.linked.setdefault(parent.name_canonical, []).append(org.name_canonical)

    session.commit()
    return result


def load_gleif_parents_parquet(
    session: Session, path: pathlib.Path = DEFAULT_GLEIF_PARQUET
) -> GleifParentsLoadResult:
    return load_gleif_parents(session, pd.read_parquet(path))


# ------------------------------------------------------------------------------ curated aliases
@dataclass
class AliasRule:
    alias: str
    canonical: str
    source_url: str
    retrieved_at: str
    note: str = ""


@dataclass
class AliasesLoadResult:
    rules: int = 0
    aliases_written: int = 0
    aliases_unchanged: int = 0
    rules_without_organization: list[str] = field(default_factory=list)
    rules_already_one_organization: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)

    def as_report(self) -> dict[str, object]:
        return {
            "rules": self.rules,
            "aliases_written": self.aliases_written,
            "aliases_unchanged": self.aliases_unchanged,
            "rules_without_organization": list(self.rules_without_organization),
            "rules_already_one_organization": list(self.rules_already_one_organization),
            "conflicts": list(self.conflicts),
        }


def _footprint(session: Session, org_id: Any) -> tuple[int, int]:
    """(`asset_owner` edges, sponsored proposals) on one organisation: what a merge that absorbed
    it would have to re-point. `services/resolve/merge.py::merge_organization` moves the proposals
    today and not the edges — the count is reported so nobody has to discover that after the fact."""
    edges = session.scalar(
        select(func.count()).select_from(AssetOwner).where(AssetOwner.organization_id == org_id)
    )
    proposals = session.scalar(
        select(func.count()).select_from(Proposal).where(Proposal.sponsor_org_id == org_id)
    )
    return int(edges or 0), int(proposals or 0)


def read_alias_rules(path: pathlib.Path = DEFAULT_ALIASES_PATH) -> list[AliasRule]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    rows = payload.get("aliases") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise ValueError(f"{path}: expected a top-level `aliases:` list")
    rules: list[AliasRule] = []
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError(f"{path}: row {i} is not a mapping")
        missing = [k for k in ("alias", "canonical", "source_url", "retrieved_at") if not row.get(k)]
        if missing:
            raise ValueError(f"{path}: row {i} lacks {missing}")
        rules.append(
            AliasRule(
                alias=str(row["alias"]).strip(),
                canonical=str(row["canonical"]).strip(),
                source_url=str(row["source_url"]),
                retrieved_at=str(row["retrieved_at"]),
                note=str(row.get("note") or ""),
            )
        )
    return rules


def load_aliases(
    session: Session, path: pathlib.Path = DEFAULT_ALIASES_PATH, *, manifest_version: str = ""
) -> AliasesLoadResult:
    """Apply the curated alias file (module docstring). Never creates an organisation: a rule
    whose canonical name is not loaded is inert and reported."""
    rules = read_alias_rules(path)
    registry = Registry()
    entry = registry.get(ALIASES_SOURCE_ID)
    source = upsert_licence_and_source(session, entry, manifest_version or registry.version)
    result = AliasesLoadResult(rules=len(rules))
    index = org_key_multimap(session)

    for rule in rules:
        canonical_orgs = index.get(org_key(rule.canonical))
        if not canonical_orgs:
            result.rules_without_organization.append(f"{rule.alias} -> {rule.canonical}")
            continue
        org = canonical_orgs[0]
        alias_orgs = index.get(org_key(rule.alias)) or []
        other = [o for o in alias_orgs if o.id != org.id]
        if other:
            edges, proposals = _footprint(session, other[0].id)
            result.conflicts.append(
                f"{rule.alias} -> {rule.canonical}: the alias already keys to "
                f"{other[0].name_canonical!r}, a live organisation holding {edges} asset_owner "
                f"edge(s) and {proposals} proposal(s); left alone (a merge is a services/resolve "
                "decision, and that is what it would move)"
            )
            continue
        if alias_orgs and not other:
            # The corrected key already reaches this pair (e.g. the `&`/`and` fold). The row is
            # still written as an alias so the spelling is recorded with its citation.
            result.rules_already_one_organization.append(f"{rule.alias} -> {rule.canonical}")
        alias_normalised = rule.alias.lower()
        existing = session.scalar(
            select(OrganizationAlias).where(
                OrganizationAlias.organization_id == org.id,
                OrganizationAlias.alias_normalised == alias_normalised,
            )
        )
        if existing is not None:
            result.aliases_unchanged += 1
            continue
        session.add(
            OrganizationAlias(
                organization_id=org.id,
                alias=rule.alias,
                alias_normalised=alias_normalised,
                kind="filing_spelling",
                source_id=source.id,
                source_url=rule.source_url,
                retrieved_at=pd.Timestamp(rule.retrieved_at).to_pydatetime(),
                licence_id=source.licence_id,
                # 1.0, unlike the ownership loader's 0.9: that match is made by a key, this one is
                # a statement read off a filing that the row cites.
                confidence=1.0,
                created_by="pipeline",
            )
        )
        result.aliases_written += 1

    session.commit()
    return result


# ------------------------------------------------------------------------------- curated merges
@dataclass
class MergeRule:
    absorb: str
    into: str
    rationale: str
    source_url: str
    retrieved_at: str


@dataclass
class MergesLoadResult:
    rules: int = 0
    merged: list[str] = field(default_factory=list)
    already_merged: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)

    def as_report(self) -> dict[str, object]:
        return {
            "rules": self.rules,
            "merged": list(self.merged),
            "already_merged": list(self.already_merged),
            "missing": list(self.missing),
            "conflicts": list(self.conflicts),
        }


def read_merge_rules(path: pathlib.Path = DEFAULT_MERGES_PATH) -> list[MergeRule]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    rows = payload.get("merges") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise ValueError(f"{path}: expected a top-level `merges:` list")
    rules: list[MergeRule] = []
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError(f"{path}: row {i} is not a mapping")
        missing = [k for k in ("absorb", "into", "rationale", "source_url", "retrieved_at") if not row.get(k)]
        if missing:
            raise ValueError(f"{path}: row {i} lacks {missing}")
        rules.append(
            MergeRule(
                absorb=str(row["absorb"]).strip(),
                into=str(row["into"]).strip(),
                rationale=" ".join(str(row["rationale"]).split()),
                source_url=str(row["source_url"]),
                retrieved_at=str(row["retrieved_at"]),
            )
        )
    return rules


def _orgs_named(session: Session, name: str) -> list[Organization]:
    """Organisations whose `name_canonical` is `name` exactly, else case-insensitively. Merged rows
    included: the caller has to see a row that is already a redirect to report it correctly."""
    exact = list(session.scalars(select(Organization).where(Organization.name_canonical == name)))
    if exact:
        return exact
    return list(
        session.scalars(
            select(Organization).where(sa.func.lower(Organization.name_canonical) == name.lower())
        )
    )


def load_merges(session: Session, path: pathlib.Path = DEFAULT_MERGES_PATH) -> MergesLoadResult:
    """Apply the curated merge file (`data/vendored/organizations/merges.yaml`, docs/22 §20.5): one
    `services.resolve.merge.merge_organization` call per rule, idempotent, never creating an
    organisation. A rule is `missing` when either side is not loaded, `already_merged` when the
    absorbed row already redirects to the survivor, and a `conflict` -- reported, not applied --
    when a name matches more than one row, the absorbed row already redirects elsewhere, the
    survivor is itself a redirect, or the two names are one row.

    The event carries the rule's `source_url` and `retrieved_at`, and its rationale in `reason`.
    It carries no `source_id`: a new `source` row needs a `data/sources.yaml` entry and a docs/13 §6
    register row (R1) first, which this lane does not own (docs/22 §20.5)."""
    rules = read_merge_rules(path)
    result = MergesLoadResult(rules=len(rules))
    for rule in rules:
        label = f"{rule.absorb} -> {rule.into}"
        absorbed_rows = _orgs_named(session, rule.absorb)
        survivor_rows = _orgs_named(session, rule.into)
        if not absorbed_rows or not survivor_rows:
            result.missing.append(label)
            continue
        if len(absorbed_rows) > 1 or len(survivor_rows) > 1:
            result.conflicts.append(f"{label}: a name matches more than one organisation row")
            continue
        absorbed, survivor = absorbed_rows[0], survivor_rows[0]
        if absorbed.id == survivor.id:
            result.conflicts.append(f"{label}: both names are one organisation row")
            continue
        if absorbed.merged_into_id == survivor.id:
            result.already_merged.append(label)
            continue
        if absorbed.merged_into_id is not None:
            result.conflicts.append(f"{label}: {rule.absorb!r} is already merged into another organisation")
            continue
        if survivor.merged_into_id is not None:
            result.conflicts.append(f"{label}: {rule.into!r} is itself merged into another organisation")
            continue
        merge_organization(
            session,
            canonical=survivor,
            absorbed=absorbed,
            rationale=f"curated merge (data/vendored/organizations/merges.yaml): {rule.rationale}",
            actor_type="user",
            source_url=rule.source_url,
            retrieved_at=pd.Timestamp(rule.retrieved_at).to_pydatetime(),
        )
        result.merged.append(label)
    session.commit()
    return result


# ---------------------------------------------------------------------------------------- CLI
def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    parents = sub.add_parser("parents", help="organization.parent_org_id from the GLEIF Level 2 parquet")
    parents.add_argument("--parquet", type=pathlib.Path, default=DEFAULT_GLEIF_PARQUET)
    aliases = sub.add_parser("aliases", help="organization_alias rows from the curated YAML")
    aliases.add_argument("--path", type=pathlib.Path, default=DEFAULT_ALIASES_PATH)
    merges = sub.add_parser("merges", help="reversible organisation merges from the curated YAML")
    merges.add_argument("--path", type=pathlib.Path, default=DEFAULT_MERGES_PATH)
    for p in (parents, aliases, merges):
        p.add_argument("--db", type=pathlib.Path, default=DEFAULT_DB_PATH)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        args.db.parent.mkdir(parents=True, exist_ok=True)
        db_url = f"sqlite+pysqlite:///{args.db}"

    t0 = time.monotonic()
    engine = get_engine(db_url)
    init_db(engine)
    session = get_sessionmaker(engine)()
    try:
        report: dict[str, object]
        if args.command == "parents":
            report = load_gleif_parents_parquet(session, args.parquet).as_report()
        elif args.command == "aliases":
            report = load_aliases(session, args.path).as_report()
        else:
            report = load_merges(session, args.path).as_report()
    finally:
        session.close()
    report["elapsed_s"] = round(time.monotonic() - t0, 2)
    print(json.dumps(report))  # noqa: T201 — CLI summary line


if __name__ == "__main__":
    main()
