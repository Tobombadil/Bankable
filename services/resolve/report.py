#!/usr/bin/env python3
"""Measurement driver for `services/resolve/merge.py` (task step 4; docs/22 §13).

Loads the real 2026-09-12 pull into a store (via the existing, unmodified
`services.ingest.loader`, restricted to sources whose licence is publishable -- SPP and ISO-NE are
`restricted` and are proven gated, not silently skipped), runs `pipeline.resolve.run()`
(unmodified) over the same normalised snapshot, applies the confidence gate and merges via
`services/resolve/merge.py`, resolves organizations, and prints every count this task's step 4 and
step 5 ask for. No number here is estimated; everything printed is read back from the store or
computed directly from the labelled set.

Usage:
    python -m services.resolve.report [--threshold 75]

This script is the source of the verbatim numbers pasted into `services/resolve/README.md` and
`docs/22-entity-resolution-and-change-detection.md` §13.
"""

from __future__ import annotations

import argparse
import datetime as dt
import pathlib
import sys
import uuid as _uuid
from collections import defaultdict
from typing import Any

import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from pipeline import resolve as resolve_module
from pipeline.connectors.registry import Registry
from pipeline.normalize import org_key
from services.db.models import Organization, Proposal, ProposalSource
from services.db.session import get_engine, get_sessionmaker, init_db
from services.ids import public_id, slugify
from services.ingest.loader import GateRefused, load_dataframe, upsert_licence_and_source
from services.resolve import merge as merge_mod

ROOT = pathlib.Path(__file__).resolve().parents[2]
EVAL = ROOT / "data" / "eval"

#: pipeline/resolve.py & pipeline/normalize.py short source tags -> the real data/sources.yaml
#: registry id the store keys on. Every one of these is checked against the registry's own
#: `reuse` at run time (below); this table is not itself the authority on what is publishable.
REGISTRY_ID = {
    "caiso": "us.iso.caiso.gen_queue",
    "ercot": "us.iso.ercot.gen_queue",
    "spp": "us.iso.spp.gen_queue",
    "nyiso": "us.iso.nyiso.gen_queue",
    "isone": "us.iso.isone.gen_queue",
    "eia860m": "us.eia.860m",
}


def _report(message: str = "") -> None:
    """CLI reporting output, the same convention `pipeline/resolve.py` and `pipeline/diff.py`
    use (see their `pyproject.toml` per-file `T20` ignores) -- one `noqa` here instead of one per
    call site, without adding a new entry to `pyproject.toml` (outside this task's assigned
    paths)."""
    print(message)  # noqa: T201


def preseed_organizations(session: Session, df: pd.DataFrame, loadable_shorts: set[str]) -> int:
    """Work around a `services/ingest/loader.py` slug limitation without editing that file (out
    of this task's assigned paths): `_get_or_create_organization` slugifies the raw sponsor name
    with no collision suffix, so two spellings that differ only in punctuation ("CED Development,
    Inc." vs "Ced Development Inc") both slugify to `ced-development-inc` and the second raises a
    unique-constraint error. Pre-creating every distinct (loader's own dedup key =
    `name.strip().lower()`) organization here first, with a collision-proof slug, means the
    loader's lookup always finds an existing row and never reaches its own creation path."""
    sub = df[df["source_id"].isin(loadable_shorts)]
    names = sub["sponsor_name"].dropna().astype(str).str.strip()
    names = names[names != ""]
    seen: set[str] = set()
    created = 0
    for name in names:
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        if session.scalar(select(Organization).where(Organization.name_normalised == key)) is not None:
            continue
        org = Organization(
            public_id="", slug="", name_canonical=name, name_normalised=key, type="other", country="US"
        )
        session.add(org)
        session.flush()
        org.public_id = public_id("org", org.id)
        org.slug = f"{slugify(name)}-{org.public_id[-8:].lower()}"
        created += 1
    session.flush()
    return created


def build_store(session: Session, normalized_path: pathlib.Path) -> dict[str, str]:
    """Load every source whose registry entry is `open`/`attribution`; prove every other one is
    refused (CLAUDE.md: "MISO/SPP/NYISO/ISO-NE terms must be read and recorded before their rows
    are published" -- SPP and ISO-NE are recorded as `restricted` in `data/sources.yaml` today).
    Returns `{short_tag: store_source_id}` for the sources actually loaded.
    """
    registry = Registry()
    df = pd.read_parquet(normalized_path)
    loadable_shorts = {
        short for short, rid in REGISTRY_ID.items() if registry.get(rid).reuse in ("open", "attribution")
    }
    preseeded = preseed_organizations(session, df, loadable_shorts)
    _report(f"  pre-seeded {preseeded:,} organizations (collision-proof slugs; see docstring)")
    loaded: dict[str, str] = {}
    for short, registry_id in REGISTRY_ID.items():
        entry = registry.get(registry_id)
        if entry.reuse not in ("open", "attribution"):
            try:
                upsert_licence_and_source(session, entry, registry.version)
            except GateRefused as exc:
                _report(f"  {short:8s} -> {registry_id:26s} GateRefused (reuse={entry.reuse!r}): {exc}")
                continue
            else:
                raise AssertionError(f"{registry_id} has reuse={entry.reuse!r} but was NOT refused")
        source = upsert_licence_and_source(session, entry, registry.version)
        sub = df[df["source_id"] == short].copy()
        sub["raw"] = "{}"  # docs/22 evaluation snapshot carries no separate raw payload per row
        result = load_dataframe(session, source, "proposal", sub, None)
        _report(
            f"  {short:8s} -> {registry_id:26s} reuse={entry.reuse:11s} "
            f"+{result.proposals_created:5,d} created  {result.proposals_updated:4,d} updated"
        )
        loaded[short] = source.id
    session.flush()
    return loaded


def _parse_retrieved_at(value: Any) -> dt.datetime:
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    resolved: dt.datetime = ts.to_pydatetime()
    return resolved


def build_link_index(session: Session) -> dict[tuple[str, str], _uuid.UUID]:
    index: dict[tuple[str, str], _uuid.UUID] = {}
    for ps in session.scalars(select(ProposalSource).where(ProposalSource.active.is_(True))):
        index[(ps.source_id, ps.source_record_id)] = ps.proposal_id
    return index


def build_clusters(
    df: pd.DataFrame,
    matches: pd.DataFrame,
    clusters: pd.DataFrame,
    loaded_sources: dict[str, str],
    link_index: dict[tuple[str, str], _uuid.UUID],
) -> tuple[dict[str, list[merge_mod.ClusterMember]], dict[str, list[merge_mod.ClusterEdge]]]:
    """Turn `pipeline.resolve.run()`'s output into per-cluster `ClusterMember`/`ClusterEdge`
    lists, restricted to records that were actually loaded into the store. `df`'s index is the
    contract `run()` uses for `li`/`ri` and for its own copy of `clusters`; real (non-rollup) rows
    occupy indices `0..len(df)-1` exactly as read from `normalized_path` (`run()` appends the
    synthetic EIA plant-rollup rows after them and only then resets the index) -- rollup rows are
    never loaded into the store as their own proposal, so indices `>= len(df)` are dropped here,
    not resolved."""
    n_real = len(df)

    def proposal_for_index(i: int) -> _uuid.UUID | None:
        if i >= n_real:
            return None
        row = df.iloc[i]
        short = str(row["source_id"])
        registry_id = loaded_sources.get(short)
        if registry_id is None:
            return None
        return link_index.get((registry_id, str(row["source_record_id"])))

    members: dict[str, list[merge_mod.ClusterMember]] = defaultdict(list)
    for i in clusters.index:
        if i >= n_real:
            continue
        row = clusters.loc[i]
        pid = proposal_for_index(int(i))
        if pid is None:
            continue
        queue_id = row.get("queue_id")
        eia_id = row.get("eia_plant_id")
        member = merge_mod.ClusterMember(
            proposal_id=pid,
            source_id=loaded_sources[str(row["source_id"])],
            queue_id=(str(queue_id) if pd.notna(queue_id) and queue_id else None),
            has_eia_id=bool(pd.notna(eia_id) and eia_id),
            retrieved_at=_parse_retrieved_at(row["retrieved_at"]),
        )
        members[str(row["cluster_id"])].append(member)

    edges: dict[str, list[merge_mod.ClusterEdge]] = defaultdict(list)
    accepted = matches[matches["accepted"]]
    for _, row in accepted.iterrows():
        li, ri = int(row["li"]), int(row["ri"])
        lpid, rpid = proposal_for_index(li), proposal_for_index(ri)
        if lpid is None or rpid is None:
            continue
        edges[str(row["cluster_id"])].append(
            merge_mod.ClusterEdge(lpid, rpid, float(row["score"]), str(row["rationale"]))
        )
    return members, edges


def apply_all_clusters(
    session: Session,
    members: dict[str, list[merge_mod.ClusterMember]],
    edges: dict[str, list[merge_mod.ClusterEdge]],
) -> list[merge_mod.ClusterApplication]:
    applications = []
    for cluster_key, member_list in members.items():
        application = merge_mod.apply_cluster(
            session, member_list, edges.get(cluster_key, []), cluster_key=cluster_key
        )
        applications.append(application)
    session.flush()
    return applications


def root_proposal_id(session: Session, proposal_id: _uuid.UUID) -> _uuid.UUID:
    seen = {proposal_id}
    current = session.get(Proposal, proposal_id)
    while current is not None and current.merged_into_id is not None:
        current = session.get(Proposal, current.merged_into_id)
        if current is None or current.id in seen:
            break
        seen.add(current.id)
    return current.id if current is not None else proposal_id


def store_path_precision(
    session: Session,
    labels_path: pathlib.Path,
    loaded_sources: dict[str, str],
    link_index: dict[tuple[str, str], _uuid.UUID],
) -> dict[str, float | int]:
    """Task step 4: re-run precision/recall on the 85 hand labels *through the store* -- i.e.
    "would this pair end up as one proposal after every gated merge in this report was applied?"
    -- rather than re-checking `pipeline.resolve.run()`'s own `accepted` column directly. Labels
    touching a gated source (SPP, ISO-NE) are outside what the store can answer and are reported
    separately, with their own count, per CLAUDE.md's "measured numbers with sample sizes."
    """
    labels = pd.read_csv(labels_path)
    labels = labels[labels["label"].isin([0, 1])]

    def resolve_side(record_id: str) -> _uuid.UUID | None:
        short, _, source_record_id = record_id.partition(":")
        registry_id = loaded_sources.get(short)
        if registry_id is None:
            return None
        pid = link_index.get((registry_id, source_record_id))
        if pid is None and "#" in source_record_id:
            pid = link_index.get((registry_id, source_record_id.split("#", 1)[0]))
        return pid

    tp = fp = fn = tn = 0
    unusable = 0
    for _, row in labels.iterrows():
        left, right = resolve_side(str(row["left_id"])), resolve_side(str(row["right_id"]))
        if left is None or right is None:
            unusable += 1
            continue
        predicted_same = root_proposal_id(session, left) == root_proposal_id(session, right)
        truth = bool(row["label"] == 1)
        if predicted_same and truth:
            tp += 1
        elif predicted_same and not truth:
            fp += 1
        elif not predicted_same and truth:
            fn += 1
        else:
            tn += 1
    n = tp + fp + fn + tn
    precision = tp / (tp + fp) if (tp + fp) else float("nan")
    recall = tp / (tp + fn) if (tp + fn) else float("nan")
    return {
        "n_usable": n,
        "n_total_labels": len(labels),
        "unusable_gated_source": unusable,
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "tn": tn,
        "precision": round(precision, 3),
        "recall": round(recall, 3),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--threshold", type=float, default=merge_mod.MERGE_SCORE_THRESHOLD)
    ap.add_argument("--normalized", default=str(EVAL / "normalized.parquet"))
    ap.add_argument("--labels", default=str(EVAL / "labels.csv"))
    args = ap.parse_args()

    engine = get_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)  # registers services.db.models tables including resolution_decision's peers
    from services.db.base import Base

    Base.metadata.create_all(engine)  # picks up services.resolve.models.ResolutionDecision too
    session_factory = get_sessionmaker(engine)

    normalized_path = pathlib.Path(args.normalized)
    with session_factory() as session:
        _report("1. Loading real 2026-09-12 pull into the store (data/sources.yaml gate applied):")
        loaded_sources = build_store(session, normalized_path)

        proposals_before = int(session.scalar(select(func.count()).select_from(Proposal)) or 0)
        orgs_before = int(session.scalar(select(func.count()).select_from(Organization)) or 0)
        _report(f"\nproposals in store before resolution: {proposals_before:,}")
        _report(f"organizations in store before resolution: {orgs_before:,}")

        _report(f"\n2. Running pipeline.resolve.run(threshold={args.threshold}) ...")
        df = pd.read_parquet(normalized_path)
        matches, clusters = resolve_module.run(args.threshold, normalized_path)

        link_index = build_link_index(session)
        members, edges = build_clusters(df, matches, clusters, loaded_sources, link_index)
        multi_member = {k: v for k, v in members.items() if len(v) >= 2}
        _report(f"resolver clusters with >=2 store-loaded members: {len(multi_member):,}")

        _report("\n3. Applying the confidence gate and merging ...")
        applications = apply_all_clusters(session, multi_member, edges)
        merged = [a for a in applications if a.action == "merged"]
        proposed = [a for a in applications if a.action == "proposed"]
        absorbed_total = sum(a.members_merged for a in merged)
        decisions_total = sum(len(a.decisions) for a in proposed)
        _report(f"clusters merged:   {len(merged):,} (absorbing {absorbed_total:,} records)")
        _report(f"clusters proposed (gate failed, filed for review): {len(proposed):,}")
        for a in proposed[:10]:
            _report(f"    cluster {a.cluster_key}: {a.gate_reason}")
        _report(f"resolution_decision rows created: {decisions_total:,}")

        session.commit()

        proposals_after = int(
            session.scalar(
                select(func.count()).select_from(Proposal).where(Proposal.merged_into_id.is_(None))
            )
            or 0
        )
        multi_source = int(
            session.scalar(
                select(func.count())
                .select_from(Proposal)
                .where(Proposal.merged_into_id.is_(None), Proposal.source_count >= 2)
            )
            or 0
        )
        _report(f"\nproposals in store after resolution (surviving/canonical rows): {proposals_after:,}")
        _report(f"  of which with >=2 sources: {multi_source:,}")
        _report(f"proposals absorbed (merged_into_id set): {proposals_before - proposals_after:,}")

        _report("\n4. Organization resolution ...")
        org_report = merge_mod.resolve_organizations(session, org_key)
        session.commit()
        orgs_after = session.scalar(
            select(func.count()).select_from(Organization).where(Organization.merged_into_id.is_(None))
        )
        _report(f"organizations before: {orgs_before:,}  after: {orgs_after:,}")
        _report(f"normalised-name groups considered (size > 1): {org_report.groups_considered:,}")
        _report(f"groups merged: {org_report.groups_merged:,}")
        _report(f"organizations absorbed: {org_report.organizations_absorbed:,}")

        _report("\n5. Precision on the 85 hand labels, through the store path ...")
        stats = store_path_precision(session, pathlib.Path(args.labels), loaded_sources, link_index)
        _report(
            f"usable labels: {stats['n_usable']} of {stats['n_total_labels']} "
            f"({stats['unusable_gated_source']} touch a gated source and are excluded)"
        )
        _report(f"tp={stats['tp']} fp={stats['fp']} fn={stats['fn']} tn={stats['tn']}")
        _report(f"precision={stats['precision']}  recall={stats['recall']}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
