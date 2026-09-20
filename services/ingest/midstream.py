"""Operator/owner edges and curated parent links for the midstream context layers (docs/21 §3.23,
docs/22 §15, ADR 0008) -> `asset_owner`, `organization.parent_org_id`.

Two loaders, both idempotent, both bulk-friendly (one `SELECT` per table up front, one commit):

**`load_operator_edges(session, df, asset_type)`** reads the same asset-shaped frame
`services/ingest/assets.py::load_assets` reads (the `pipeline/context/eia_atlas.py` parquet) and
writes one `asset_owner` row per (asset, organisation, role) from its `operator_name` (role
`operator`) and `owner_name` (role `owner`) strings. `share_pct` is NULL -- the Atlas states no
shares -- and `owner_name_raw` keeps the source spelling. The edge's provenance quartet is the
layer's own source (the frame's `source_id`, validated by
`services.ingest.assets.resolve_source_id`), the same `source`/`licence` rows the asset carries.
Organisations resolve through `services/ingest/ownership.py::_resolve_organization` --
`pipeline.normalize.org_key`, the legal-form-and-punctuation-stripping key, over every
`organization.name_canonical` and `organization_alias.alias`, so "Tallgrass Energy Midstream LLC"
and "TALLGRASS ENERGY MIDSTREAM, L.L.C." are one row, and a string that matches an organisation the
proposal loader already created (a sponsor, a filer) reuses it rather than duplicating it. A raw
string that resolves to nothing creates the organisation plus a `filing_spelling` alias, as that
module documents. An asset the frame names but the table lacks (not loaded yet) is counted in
`unmatched_asset_ids`, never guessed at.

**`load_parents(session, path)`** applies `data/vendored/organizations/parents.yaml`: for each
row, every organisation whose canonical name or alias matches `child_pattern` (a regular
expression, case-insensitive, anchored by the author) gets `parent_org_id` = the organisation
named `parent` (created if absent, resolved by the same `org_key`) and `parent_source_id` =
`curated.organization_parents` (registered in `data/sources.yaml` section K: reuse `open`,
publication `raw_ok`; each YAML row cites the company statement it came from with its
`source_url` and `retrieved_at`). A child that is itself the parent is skipped, so a pattern like
``^tallgrass`` cannot make Tallgrass Energy its own parent. The file is the interim source for
parent links: since 2026-09-20 `services/ingest/organizations.py::load_gleif_parents` applies GLEIF
Level 2 (CC0) over the same column and wins wherever it has a record, and this loader skips any
organisation already carrying `parent_source_id = global.gleif.lei` (`children_deferred_to_gleif`).
That makes the two order-independent and both re-runs no-ops, and `parent_source_id` is what lets
them be told apart on the company page. GLEIF covers none of the nine Tallgrass children today —
five hold an LEI, none is the start node of a Level 2 relationship record — so every curated rule
still does work (docs/22 §17.3).

Re-running either loader against the same inputs writes no duplicate: edges are keyed by the
table's unique constraint `(asset_id, organization_id, role, source_id)` and looked up before
insert; parent links are plain column updates.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import re
import time
from dataclasses import dataclass, field
from typing import Any

import pandas as pd
import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from pipeline.connectors.registry import Registry
from pipeline.normalize import org_key
from services.db.models import Asset, AssetOwner, Organization, OrganizationAlias, Source, new_uuid
from services.db.session import get_engine, get_sessionmaker, init_db
from services.ids import public_id as make_public_id
from services.ids import slugify
from services.ingest.assets import ASSET_TYPE_SOURCE_IDS, resolve_source_id
from services.ingest.loader import upsert_licence_and_source
from services.ingest.ownership import _build_norm_org_index, _resolve_organization

DEFAULT_DB_PATH = pathlib.Path("web/.data/dev.db")
ROOT = pathlib.Path(__file__).resolve().parents[2]
DEFAULT_PARENTS_PATH = ROOT / "data" / "vendored" / "organizations" / "parents.yaml"
PARENTS_SOURCE_ID = "curated.organization_parents"
#: The registry that supersedes this file where it has an answer (module docstring, docs/22 §17).
GLEIF_PARENTS_SOURCE_ID = "global.gleif.lei"

#: frame column -> `asset_owner.role`
_EDGE_COLUMNS: tuple[tuple[str, str], ...] = (("operator_name", "operator"), ("owner_name", "owner"))


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _text(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, float) and pd.isna(value):
        return None
    if value is pd.NA or value is pd.NaT:
        return None
    s = str(value).strip()
    return s or None


def _to_datetime(value: object) -> dt.datetime | None:
    text = _text(value)
    if text is None:
        return None
    ts = pd.Timestamp(text)
    if pd.isna(ts):
        return None
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    result: dt.datetime = ts.to_pydatetime()
    return result


# ------------------------------------------------------------------------------ operator edges
@dataclass
class EdgesLoadResult:
    rows_seen: int = 0
    assets_matched: int = 0
    unmatched_asset_ids: list[str] = field(default_factory=list)
    organizations_created: int = 0
    edges_written: int = 0
    edges_by_role: dict[str, int] = field(default_factory=dict)

    def as_report(self) -> dict[str, object]:
        return {
            "rows_seen": self.rows_seen,
            "assets_matched": self.assets_matched,
            "unmatched_asset_ids": sorted(set(self.unmatched_asset_ids)),
            "organizations_created": self.organizations_created,
            "edges_written": self.edges_written,
            "edges_by_role": dict(sorted(self.edges_by_role.items())),
        }


def load_operator_edges(
    session: Session,
    df: pd.DataFrame,
    asset_type: str,
    *,
    source_id: str | None = None,
    manifest_version: str = "",
) -> EdgesLoadResult:
    """One `asset_owner` edge per (asset, resolved organisation, role) from the frame's
    `operator_name` / `owner_name` columns; module docstring for the rules. The source is the
    frame's own `source_id` (`services.ingest.assets.resolve_source_id`), as for the assets."""
    registry = Registry()
    entry = registry.get(resolve_source_id(df, asset_type, source_id))
    source = upsert_licence_and_source(session, entry, manifest_version or registry.version)

    result = EdgesLoadResult(rows_seen=len(df))
    if not len(df):
        session.commit()
        return result

    rows = df.to_dict("records")
    wanted_ids = sorted({str(r.get("source_asset_id")) for r in rows})
    assets: dict[str, Asset] = {
        a.source_asset_id: a
        for a in session.scalars(
            select(Asset).where(
                Asset.source_id == source.id,
                Asset.asset_type == asset_type,
                Asset.source_asset_id.in_(wanted_ids),
            )
        )
    }
    result.assets_matched = sum(1 for sid in wanted_ids if sid in assets)
    result.unmatched_asset_ids = [sid for sid in wanted_ids if sid not in assets]

    existing_edges: dict[tuple[str, str, str], AssetOwner] = {
        (str(e.asset_id), str(e.organization_id), e.role): e
        for e in session.scalars(
            select(AssetOwner).where(
                AssetOwner.source_id == source.id,
                AssetOwner.asset_id.in_([a.id for a in assets.values()]),
            )
        )
    }

    now = utcnow()
    norm_index = _build_norm_org_index(session)
    created_counter = [0]

    for row in rows:
        asset = assets.get(str(row.get("source_asset_id")))
        if asset is None:
            continue
        source_url = _text(row.get("source_url")) or source.url
        retrieved_at = _to_datetime(row.get("retrieved_at")) or now
        for column, role in _EDGE_COLUMNS:
            raw_name = _text(row.get(column))
            if raw_name is None:
                continue
            org = _resolve_organization(
                session, norm_index, raw_name, source=source, now=now, created_counter=created_counter
            )
            key = (str(asset.id), str(org.id), role)
            edge = existing_edges.get(key)
            if edge is None:
                edge = AssetOwner(
                    asset_id=asset.id,
                    organization_id=org.id,
                    role=role,
                    source_id=source.id,
                    licence_id=source.licence_id,
                )
                session.add(edge)
                existing_edges[key] = edge
            edge.share_pct = None
            edge.as_of = None
            edge.owner_name_raw = raw_name
            edge.source_url = source_url
            edge.retrieved_at = retrieved_at
            result.edges_written += 1
            result.edges_by_role[role] = result.edges_by_role.get(role, 0) + 1

    result.organizations_created = created_counter[0]
    session.commit()
    return result


def load_operator_edges_parquet(
    session: Session, path: pathlib.Path, asset_type: str, *, source_id: str | None = None
) -> EdgesLoadResult:
    df = pd.read_parquet(path)
    return load_operator_edges(session, df, asset_type, source_id=source_id)


# ------------------------------------------------------------------------------- parent links
@dataclass
class ParentRule:
    child_pattern: str
    parent: str
    source_url: str
    retrieved_at: str
    note: str = ""

    @property
    def regex(self) -> re.Pattern[str]:
        return re.compile(self.child_pattern, re.IGNORECASE)


@dataclass
class ParentsLoadResult:
    rules: int = 0
    parents_created: int = 0
    children_linked: int = 0
    children_unchanged: int = 0
    children_deferred_to_gleif: int = 0
    rules_without_match: list[str] = field(default_factory=list)
    linked: dict[str, list[str]] = field(default_factory=dict)

    def as_report(self) -> dict[str, object]:
        return {
            "rules": self.rules,
            "parents_created": self.parents_created,
            "children_linked": self.children_linked,
            "children_unchanged": self.children_unchanged,
            "children_deferred_to_gleif": self.children_deferred_to_gleif,
            "rules_without_match": list(self.rules_without_match),
            "linked": {k: sorted(v) for k, v in sorted(self.linked.items())},
        }


def read_parent_rules(path: pathlib.Path = DEFAULT_PARENTS_PATH) -> list[ParentRule]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    rows = payload.get("parents") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise ValueError(f"{path}: expected a top-level `parents:` list")
    rules: list[ParentRule] = []
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError(f"{path}: row {i} is not a mapping")
        missing = [k for k in ("child_pattern", "parent", "source_url", "retrieved_at") if not row.get(k)]
        if missing:
            raise ValueError(f"{path}: row {i} lacks {missing}")
        rules.append(
            ParentRule(
                child_pattern=str(row["child_pattern"]),
                parent=str(row["parent"]).strip(),
                source_url=str(row["source_url"]),
                retrieved_at=str(row["retrieved_at"]),
                note=str(row.get("note") or ""),
            )
        )
    return rules


def _parents_source(session: Session, manifest_version: str) -> Source:
    registry = Registry()
    entry = registry.get(PARENTS_SOURCE_ID)
    return upsert_licence_and_source(session, entry, manifest_version or registry.version)


def _get_or_create_parent(
    session: Session, norm_index: dict[str, Organization], name: str, *, source: Source, now: dt.datetime
) -> tuple[Organization, bool]:
    key = org_key(name)  # one function, docs/22 §16 (was an inlined, divergent fallback)
    org = norm_index.get(key)
    if org is not None:
        return org, False
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
            kind="filing_spelling",
            source_id=source.id,
            source_url=source.url,
            retrieved_at=now,
            licence_id=source.licence_id,
            confidence=1.0,
            created_by="pipeline",
        )
    )
    norm_index[key] = org
    return org, True


def load_parents(
    session: Session, path: pathlib.Path = DEFAULT_PARENTS_PATH, *, manifest_version: str = ""
) -> ParentsLoadResult:
    """Apply the curated parent file (module docstring). Matching is over `name_canonical` and
    every alias; an organisation already carrying the same `parent_org_id` counts as unchanged."""
    rules = read_parent_rules(path)
    source = _parents_source(session, manifest_version)
    result = ParentsLoadResult(rules=len(rules))
    now = utcnow()

    norm_index = _build_norm_org_index(session)
    orgs: list[Organization] = list(session.scalars(select(Organization)))
    aliases: dict[Any, list[str]] = {}
    for alias, org_id in session.execute(
        select(OrganizationAlias.alias, OrganizationAlias.organization_id)
    ).all():
        aliases.setdefault(org_id, []).append(alias)

    for rule in rules:
        parent, created = _get_or_create_parent(session, norm_index, rule.parent, source=source, now=now)
        if created:
            result.parents_created += 1
            orgs.append(parent)
        regex = rule.regex
        matched_any = False
        for org in orgs:
            if org.id == parent.id or org.merged_into_id is not None:
                continue
            names = [org.name_canonical, *aliases.get(org.id, [])]
            if not any(regex.search(n) for n in names):
                continue
            matched_any = True
            if org.parent_source_id == GLEIF_PARENTS_SOURCE_ID:
                # A registry beat the curated rule to it. The curated file is the interim source
                # (module docstring); it never overwrites a GLEIF Level 2 link, which is what makes
                # the two loaders order-independent and both re-runs no-ops
                # (`services/ingest/organizations.py::load_gleif_parents`, docs/22 §17.3).
                result.children_deferred_to_gleif += 1
                continue
            if org.parent_org_id == parent.id and org.parent_source_id == source.id:
                result.children_unchanged += 1
                continue
            org.parent_org_id = parent.id
            org.parent_source_id = source.id
            org.last_changed = now
            result.children_linked += 1
            result.linked.setdefault(parent.name_canonical, []).append(org.name_canonical)
        if not matched_any:
            result.rules_without_match.append(rule.child_pattern)

    session.commit()
    return result


# ---------------------------------------------------------------------------------------- CLI
def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    edges = sub.add_parser("edges", help="asset_owner edges from a context parquet's operator/owner strings")
    edges.add_argument("--parquet", type=pathlib.Path, required=True)
    edges.add_argument("--asset-type", required=True, choices=sorted(ASSET_TYPE_SOURCE_IDS))
    parents = sub.add_parser("parents", help="organization.parent_org_id from the curated YAML")
    parents.add_argument("--path", type=pathlib.Path, default=DEFAULT_PARENTS_PATH)
    for p in (edges, parents):
        p.add_argument("--db", type=pathlib.Path, default=DEFAULT_DB_PATH)
    args = parser.parse_args(argv)

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
        if args.command == "edges":
            report = load_operator_edges_parquet(session, args.parquet, args.asset_type).as_report()
        else:
            report = load_parents(session, args.path).as_report()
    finally:
        session.close()
    report["elapsed_s"] = round(time.monotonic() - t0, 2)
    print(json.dumps(report))  # noqa: T201 — CLI summary line


if __name__ == "__main__":
    main()
