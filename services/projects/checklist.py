"""The bankability checklist (docs/19 §2.3, §2.4).

**The method reused, the content kept private.** Six folders of standard project-finance documents,
read through five pillars: completeness (every item), fundamentals, counterparties, regulatory and
site, sponsor. The public repository carries structure and generic item names only
(`data/checklist/generic-v1.yaml`, schema `data/checklist/pack.schema.json`). Detailed content is a
private content pack in the same schema, loaded at deploy by `services.projects.load_checklist`
into `checklist_template_item`; until one is loaded, the generic names are what users see.

**Statuses are the user's own statement:** not started, in progress, done, not applicable. There
is no `verified` status and no certified label. Readiness is **counts by pillar only** ("N of M
items done (your status) · K with suggested public-record evidence"): no single score, because a
number built from self-reported ticks would read as an assessment (owner, 2026-10-22).

**Suggested evidence is a reference.** `private_project_evidence` stores the record and the rule
that fired (`EVIDENCE_RULE_ITEMS`); the text is rebuilt live from the record by the API. Accepting
or dismissing one never changes the item's status.
"""

from __future__ import annotations

import datetime as dt
import functools
import json
import pathlib
import uuid as _uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

import yaml
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from services.api.errors import ProblemError
from services.projects.authz import ProjectAccess, granted_project_ids, project_not_found, require_action
from services.projects.models import (
    CHECKLIST_FOLDERS,
    EVIDENCE_RULES,
    ITEM_NAME_MAX_CHARS,
    ITEM_PILLARS,
    ITEM_STATUSES,
    LINK_RECORD_TYPES,
    ChecklistTemplateItem,
    PrivateProject,
    PrivateProjectEvidence,
    PrivateProjectItem,
    PrivateProjectLink,
    add_activity,
    has_control_characters,
    is_record_public_id,
    utcnow,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
CHECKLIST_DIR = REPO_ROOT / "data" / "checklist"
GENERIC_PACK_PATH = CHECKLIST_DIR / "generic-v1.yaml"
PACK_SCHEMA_PATH = CHECKLIST_DIR / "pack.schema.json"
GENERIC_PACK_VERSION: Final = "generic-v1"
PACK_SCHEMA_ID: Final = "inframation.checklist-pack/1"

#: docs/19 §2.3: v1 items carry pillar tags by folder; a content pack may override them per item.
FOLDER_PILLARS: Final[Mapping[str, str]] = {
    "contracts": "counterparties",
    "finance": "fundamentals",
    "security": "fundamentals",
    "permits": "regulatory_site",
    "feasibility": "fundamentals",
    "legal_corporate": "sponsor",
}
FOLDER_LABELS: Final[Mapping[str, str]] = {
    "contracts": "Contracts",
    "finance": "Finance",
    "security": "Security",
    "permits": "Permits",
    "feasibility": "Feasibility",
    "legal_corporate": "Legal & Corporate",
}
#: Completeness first: it counts every item.
READINESS_PILLARS: Final = ("completeness", *ITEM_PILLARS)
PILLAR_LABELS: Final[Mapping[str, str]] = {
    "completeness": "Completeness",
    "fundamentals": "Fundamentals",
    "counterparties": "Counterparties",
    "regulatory_site": "Regulatory and site",
    "sponsor": "Sponsor",
}
ITEM_STATUS_LABELS: Final[Mapping[str, str]] = {
    "not_started": "Not started",
    "in_progress": "In progress",
    "done": "Done",
    "not_applicable": "Not applicable",
}
#: Wherever a status is shown to anyone but the person who set it (docs/19 §2.3).
SHARED_STATUS_LABELS: Final[Mapping[str, str]] = {
    **ITEM_STATUS_LABELS,
    "done": "Done (your status)",
}
EVIDENCE_LABEL: Final = "Suggested from public records · not verified"
STATUS_DISCLAIMER: Final = (
    "Statuses are what you and your collaborators have marked. Nothing here is verified."
)

#: docs/19 §2.4 (docs/18 §3.3.5): the only items a public record may suggest evidence for, and the
#: rule that may fire for each. Land rights, land-use and construction permits, offtake, PPA, EPC,
#: finance, security and legal items are never suggested from a public record.
EVIDENCE_RULE_ITEMS: Final[Mapping[str, str]] = {
    "queue_position": "interconnection_agreement",
    "executed_interconnection_agreement": "interconnection_agreement",
    "gb_tec_register": "grid_connection_agreement",
    "air_permit": "air_quality_permit",
    "federal_record": "environmental_approvals",
}
EVIDENCE_ELIGIBLE_KEYS: Final = frozenset(EVIDENCE_RULE_ITEMS.values())


class PackError(ValueError):
    """A content pack that does not validate. The message names where, never what: a private
    pack's text must not reach a log or an error report."""


@dataclass(frozen=True)
class TemplateItem:
    key: str
    folder: str
    pillar: str
    name: str
    sort_order: int
    guidance: str | None = None
    #: Empty: every technology.
    technologies: tuple[str, ...] = ()

    def applies_to(self, technology: str | None) -> bool:
        return not self.technologies or technology is None or technology in self.technologies


@dataclass(frozen=True)
class ChecklistPack:
    pack_version: str
    items: tuple[TemplateItem, ...]

    def keys(self) -> frozenset[str]:
        return frozenset(item.key for item in self.items)


# ------------------------------------------------------------------------------------ the packs
@functools.cache
def pack_schema() -> dict[str, Any]:
    schema: dict[str, Any] = json.loads(PACK_SCHEMA_PATH.read_text(encoding="utf-8"))
    return schema


def _where(path: Iterable[object]) -> str:
    parts = [str(part) for part in path]
    return "/".join(parts) or "(root)"


def validate_pack(document: object, *, generic: ChecklistPack | None = None) -> ChecklistPack:
    """Validates `document` against the public schema and the structural rules, and returns it as
    a `ChecklistPack`. With `generic` (any pack but the generic one), every key must exist in the
    generic pack and keep its folder: "an unmatched key is refused" (docs/19 §2.3)."""
    from jsonschema import Draft202012Validator  # type: ignore[import-untyped]

    validator = Draft202012Validator(pack_schema())
    errors = sorted(validator.iter_errors(document), key=lambda e: list(e.absolute_path))
    if errors:
        first = errors[0]
        raise PackError(
            f"pack does not match the schema at {_where(first.absolute_path)} ({first.validator})"
        )
    if not isinstance(document, dict):  # pragma: no cover - the schema's root type is an object
        raise PackError("pack is not an object")
    items: list[TemplateItem] = []
    seen: set[str] = set()
    for index, raw in enumerate(document["items"]):
        key = raw["key"]
        if key in seen:
            raise PackError(f"pack repeats a key at items/{index}")
        seen.add(key)
        items.append(
            TemplateItem(
                key=key,
                folder=raw["folder"],
                pillar=raw["pillar"],
                name=raw["name"],
                sort_order=int(raw.get("sort_order", (index + 1) * 10)),
                guidance=raw.get("guidance"),
                technologies=tuple(raw.get("technologies", ())),
            )
        )
    if generic is not None:
        if document["pack_version"] == GENERIC_PACK_VERSION:
            raise PackError(f"pack_version {GENERIC_PACK_VERSION} is reserved for the public generic pack")
        folders = {item.key: item.folder for item in generic.items}
        for index, item in enumerate(items):
            if item.key not in folders:
                raise PackError(f"pack key at items/{index} is not in {GENERIC_PACK_VERSION}")
            if item.folder != folders[item.key]:
                raise PackError(f"pack item at items/{index} moves a generic item to another folder")
    return ChecklistPack(pack_version=document["pack_version"], items=tuple(items))


@functools.cache
def generic_pack() -> ChecklistPack:
    """`data/checklist/generic-v1.yaml`, validated. It carries no guidance (the public repository
    holds names only) and every item's pillar follows its folder."""
    document = yaml.safe_load(GENERIC_PACK_PATH.read_text(encoding="utf-8"))
    pack = validate_pack(document)
    if pack.pack_version != GENERIC_PACK_VERSION:
        raise PackError(f"{GENERIC_PACK_PATH.name} must be pack_version {GENERIC_PACK_VERSION}")
    for index, item in enumerate(pack.items):
        if item.guidance is not None:
            raise PackError(
                f"the generic pack carries guidance at items/{index}; that belongs in a private pack"
            )
        if item.pillar != FOLDER_PILLARS[item.folder]:
            raise PackError(f"the generic pack's pillar at items/{index} does not follow its folder")
    return pack


def loaded_pack_version(db: Session) -> str | None:
    """The private pack in use: the most recently loaded one, or `None` before any is loaded."""
    row = db.execute(
        select(ChecklistTemplateItem.pack_version)
        .order_by(ChecklistTemplateItem.loaded_at.desc(), ChecklistTemplateItem.pack_version.desc())
        .limit(1)
    ).scalar_one_or_none()
    return row


def active_pack(db: Session) -> ChecklistPack:
    """The pack new projects get: the loaded private pack, else the generic names."""
    version = loaded_pack_version(db)
    if version is None:
        return generic_pack()
    rows = db.execute(
        select(ChecklistTemplateItem)
        .where(ChecklistTemplateItem.pack_version == version)
        .order_by(ChecklistTemplateItem.sort_order, ChecklistTemplateItem.key)
    ).scalars()
    items = tuple(
        TemplateItem(
            key=row.key,
            folder=row.folder,
            pillar=row.pillar,
            name=row.name,
            sort_order=row.sort_order,
            guidance=row.guidance,
            technologies=tuple(row.technologies or ()),
        )
        for row in rows
    )
    return ChecklistPack(pack_version=version, items=items) if items else generic_pack()


def guidance_for(db: Session, keys: Iterable[str]) -> dict[str, str]:
    """ "What this is" text for the given template keys from the loaded private pack, once there is
    one (docs/19 §6.2). Empty while only the generic pack exists."""
    wanted = {key for key in keys if key}
    version = loaded_pack_version(db)
    if version is None or not wanted:
        return {}
    rows = db.execute(
        select(ChecklistTemplateItem.key, ChecklistTemplateItem.guidance).where(
            ChecklistTemplateItem.pack_version == version, ChecklistTemplateItem.key.in_(sorted(wanted))
        )
    ).all()
    return {key: guidance for key, guidance in rows if guidance}


# ---------------------------------------------------------------------------- a project's items
def instantiate_items(
    db: Session, project: PrivateProject, *, now: dt.datetime | None = None
) -> list[PrivateProjectItem]:
    """The project's checklist, copied from the active pack when the project is created: names,
    folders and pillars become the project's own rows; guidance is read live, never copied."""
    now = now or utcnow()
    pack = active_pack(db)
    rows = [
        PrivateProjectItem(
            project_id=project.id,
            template_key=item.key,
            pack_version=pack.pack_version,
            name=item.name,
            folder=item.folder,
            pillar=item.pillar,
            status="not_started",
            sort_order=item.sort_order,
            updated_at=now,
        )
        for item in pack.items
        if item.applies_to(project.technology)
    ]
    db.add_all(rows)
    db.flush()
    return rows


def project_items(db: Session, access: ProjectAccess) -> list[PrivateProjectItem]:
    """The project's items in folder order, then their own order."""
    require_action(access, "read")
    folder_order = {folder: index for index, folder in enumerate(CHECKLIST_FOLDERS)}
    rows = db.execute(
        select(PrivateProjectItem).where(PrivateProjectItem.project_id == access.project.id)
    ).scalars()
    return sorted(rows, key=lambda r: (folder_order[r.folder], r.sort_order, r.name, str(r.id)))


def _item(db: Session, access: ProjectAccess, item_id: _uuid.UUID) -> PrivateProjectItem:
    item = db.execute(
        select(PrivateProjectItem).where(
            PrivateProjectItem.id == item_id, PrivateProjectItem.project_id == access.project.id
        )
    ).scalar_one_or_none()
    if item is None:
        raise project_not_found()
    return item


def _invalid(field: str, message: str) -> ProblemError:
    return ProblemError(
        "validation_error", "Invalid request", detail=message, errors=[{"field": field, "message": message}]
    )


def set_item_status(
    db: Session, access: ProjectAccess, item_id: _uuid.UUID, status: str, *, now: dt.datetime | None = None
) -> PrivateProjectItem:
    """The user's own statement about one item (editor). `verified` is not a status."""
    require_action(access, "edit")
    if status not in ITEM_STATUSES:
        raise _invalid("status", "status is one of " + ", ".join(ITEM_STATUSES))
    item = _item(db, access, item_id)
    if item.status == status:
        return item
    now = now or utcnow()
    item.status = status
    item.updated_by_user_id = access.user.id
    item.updated_at = now
    add_activity(
        db,
        project_id=access.project.id,
        actor_user_id=access.user.id,
        action="item_status_changed",
        detail={"item_id": str(item.id), "status": status},
        now=now,
    )
    db.flush()
    return item


def add_custom_item(
    db: Session,
    access: ProjectAccess,
    *,
    name: str,
    folder: str,
    pillar: str | None = None,
    now: dt.datetime | None = None,
) -> PrivateProjectItem:
    """A user's own item (editor). Its pillar follows its folder unless given."""
    require_action(access, "edit")
    if not isinstance(name, str) or not name.strip():
        raise _invalid("name", "name is required")
    name = " ".join(name.split())
    if len(name) > ITEM_NAME_MAX_CHARS:
        raise _invalid("name", f"name is at most {ITEM_NAME_MAX_CHARS} characters")
    if has_control_characters(name):
        raise _invalid("name", "name contains a control character")
    if folder not in CHECKLIST_FOLDERS:
        raise _invalid("folder", "folder is one of " + ", ".join(CHECKLIST_FOLDERS))
    pillar = pillar or FOLDER_PILLARS[folder]
    if pillar not in ITEM_PILLARS:
        raise _invalid("pillar", "pillar is one of " + ", ".join(ITEM_PILLARS))
    now = now or utcnow()
    last = db.scalar(
        select(func.max(PrivateProjectItem.sort_order)).where(
            PrivateProjectItem.project_id == access.project.id, PrivateProjectItem.folder == folder
        )
    )
    item = PrivateProjectItem(
        project_id=access.project.id,
        template_key=None,
        pack_version=None,
        name=name,
        folder=folder,
        pillar=pillar,
        status="not_started",
        sort_order=(last or 0) + 1,
        updated_by_user_id=access.user.id,
        updated_at=now,
    )
    db.add(item)
    db.flush()
    add_activity(
        db,
        project_id=access.project.id,
        actor_user_id=access.user.id,
        action="field_changed",
        detail={"fields": ["checklist"], "item_id": str(item.id)},
        now=now,
    )
    db.flush()
    return item


# --------------------------------------------------------------------------------------- evidence
def may_suggest(template_key: str | None, rule: str) -> bool:
    """Whether `rule` may attach a suggestion to the item with `template_key` (docs/19 §2.4)."""
    return template_key is not None and EVIDENCE_RULE_ITEMS.get(rule) == template_key


def record_suggestion(
    db: Session,
    access: ProjectAccess,
    *,
    item_id: _uuid.UUID,
    record_type: str,
    record_public_id: str,
    rule: str,
    now: dt.datetime | None = None,
) -> PrivateProjectEvidence | None:
    """Stores the reference behind one suggestion (state `suggested`), or returns the existing
    row with its decision intact.

    The API calls this from its own live evaluation of the project's links (docs/19 §2.4), never
    with values from a request: that is why any member's view may record one (`read`), since it
    is a reference to a public record, not a user statement. Even so, the record must be one the
    project links to, and the rule one that may attach to the item. On an archived project nothing
    new is written: the existing row is returned, or `None` (show the suggestion without Accept and
    Dismiss, which an archived project refuses anyway)."""
    require_action(access, "read")
    if rule not in EVIDENCE_RULES:
        raise ValueError(f"unknown evidence rule {rule!r}")
    if record_type not in LINK_RECORD_TYPES or not is_record_public_id(record_type, record_public_id):
        raise project_not_found()
    item = _item(db, access, item_id)
    if not may_suggest(item.template_key, rule):
        raise ValueError(f"rule {rule!r} never suggests evidence for item {item.template_key!r}")
    linked = db.scalar(
        select(func.count())
        .select_from(PrivateProjectLink)
        .where(
            PrivateProjectLink.project_id == access.project.id,
            PrivateProjectLink.record_type == record_type,
            PrivateProjectLink.record_public_id == record_public_id,
        )
    )
    if not linked:
        raise project_not_found()
    existing = db.execute(
        select(PrivateProjectEvidence).where(
            PrivateProjectEvidence.item_id == item.id,
            PrivateProjectEvidence.record_type == record_type,
            PrivateProjectEvidence.record_public_id == record_public_id,
            PrivateProjectEvidence.rule == rule,
        )
    ).scalar_one_or_none()
    if existing is not None or access.project.archived_at is not None:
        return existing
    row = PrivateProjectEvidence(
        project_id=access.project.id,
        item_id=item.id,
        record_type=record_type,
        record_public_id=record_public_id,
        rule=rule,
        state="suggested",
        created_at=now or utcnow(),
    )
    db.add(row)
    db.flush()
    return row


def decide_evidence(
    db: Session,
    access: ProjectAccess,
    *,
    item_id: _uuid.UUID,
    evidence_id: _uuid.UUID,
    decision: str,
    now: dt.datetime | None = None,
) -> PrivateProjectEvidence:
    """Accept keeps the suggestion on the item; dismiss hides it (editor). Neither changes the
    item's status (docs/19 §2.4)."""
    require_action(access, "edit")
    if decision not in ("accepted", "dismissed"):
        raise _invalid("decision", "decision is accept or dismiss")
    row = db.execute(
        select(PrivateProjectEvidence).where(
            PrivateProjectEvidence.id == evidence_id,
            PrivateProjectEvidence.item_id == item_id,
            PrivateProjectEvidence.project_id == access.project.id,
        )
    ).scalar_one_or_none()
    if row is None:
        raise project_not_found()
    if row.state == decision:
        return row
    now = now or utcnow()
    row.state = decision
    row.decided_by_user_id = access.user.id
    row.decided_at = now
    add_activity(
        db,
        project_id=access.project.id,
        actor_user_id=access.user.id,
        action="evidence_accepted" if decision == "accepted" else "evidence_dismissed",
        detail={"item_id": str(item_id), "evidence_id": str(row.id)},
        now=now,
    )
    db.flush()
    return row


# -------------------------------------------------------------------------------------- readiness
@dataclass(frozen=True)
class PillarReadiness:
    """One line of "Readiness": "N of M items done (your status) · K with suggested public-record
    evidence". `total` leaves out items marked not applicable. Counts only; there is no score."""

    pillar: str
    label: str
    done: int
    total: int
    not_applicable: int
    with_suggested_evidence: int


@dataclass(frozen=True)
class Progress:
    """ "9 of 32 done, your status" (docs/19 §2.7): done items over items not marked not applicable."""

    done: int
    total: int


def readiness(
    db: Session, access: ProjectAccess, *, evidence_item_ids: Iterable[_uuid.UUID] | None = None
) -> list[PillarReadiness]:
    """Counts by pillar, completeness first. `evidence_item_ids` is the set of items with a live
    suggestion (the API rebuilds suggestions from the linked records at the viewer's tier); without
    it, stored suggestions not dismissed are counted."""
    items = project_items(db, access)
    if evidence_item_ids is None:
        evidenced = set(
            db.execute(
                select(PrivateProjectEvidence.item_id).where(
                    PrivateProjectEvidence.project_id == access.project.id,
                    PrivateProjectEvidence.state != "dismissed",
                )
            ).scalars()
        )
    else:
        evidenced = set(evidence_item_ids)
    return [_pillar_counts(pillar, items, evidenced) for pillar in READINESS_PILLARS]


def _pillar_counts(
    pillar: str, items: Sequence[PrivateProjectItem], evidenced: set[_uuid.UUID]
) -> PillarReadiness:
    chosen = [item for item in items if pillar == "completeness" or item.pillar == pillar]
    applicable = [item for item in chosen if item.status != "not_applicable"]
    return PillarReadiness(
        pillar=pillar,
        label=PILLAR_LABELS[pillar],
        done=sum(1 for item in applicable if item.status == "done"),
        total=len(applicable),
        not_applicable=len(chosen) - len(applicable),
        with_suggested_evidence=sum(1 for item in applicable if item.id in evidenced),
    )


def progress_by_project(db: Session, accesses: Sequence[ProjectAccess]) -> dict[_uuid.UUID, Progress]:
    """One query for many projects (the list page and the map pins), keyed by `access.project.id`.
    Takes the accesses `authorize` or `member_projects` returned, never ids (`granted_project_ids`)."""
    project_ids = granted_project_ids(accesses)
    if not project_ids:
        return {}
    rows = db.execute(
        select(PrivateProjectItem.project_id, PrivateProjectItem.status, func.count())
        .where(PrivateProjectItem.project_id.in_(list(project_ids)))
        .group_by(PrivateProjectItem.project_id, PrivateProjectItem.status)
    ).all()
    counts: dict[_uuid.UUID, dict[str, int]] = {pid: {} for pid in project_ids}
    for project_id, status, count in rows:
        counts.setdefault(project_id, {})[status] = int(count)
    return {
        pid: Progress(
            done=by_status.get("done", 0),
            total=sum(n for status, n in by_status.items() if status != "not_applicable"),
        )
        for pid, by_status in counts.items()
    }
