"""The bankability checklist (docs/19 §2.3, §2.4): the public generic pack, counts by pillar
only, statuses that are the user's own, and evidence that is a suggestion, never a verification."""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
from collections import Counter

import pytest
import sqlalchemy as sa
import yaml
from jsonschema import Draft202012Validator
from sqlalchemy.orm import Session

from services.api.errors import ProblemError
from services.projects import checklist
from services.projects.authz import ProjectAccess, RoleForbidden, authorize, create_project
from services.projects.checklist import (
    EVIDENCE_RULE_ITEMS,
    FOLDER_PILLARS,
    GENERIC_PACK_PATH,
    PACK_SCHEMA_PATH,
    PackError,
    add_custom_item,
    decide_evidence,
    generic_pack,
    progress_by_project,
    project_items,
    readiness,
    record_suggestion,
    set_item_status,
    validate_pack,
)
from services.projects.conftest import UserFactory, session_ctx
from services.projects.models import (
    CHECKLIST_FOLDERS,
    EVIDENCE_RULES,
    ITEM_PILLARS,
    PROJECT_TECHNOLOGIES,
    ChecklistTemplateItem,
    PrivateProjectActivity,
    PrivateProjectEvidence,
    PrivateProjectItem,
    PrivateProjectLink,
    PrivateProjectMember,
)

NOW = dt.datetime(2026, 10, 22, 12, 0, tzinfo=dt.UTC)

#: docs/18 §1.3 names these 18 of Bankable's 32 templates; docs/19 §2.3 keeps the names.
DOCS_18_NAMES = {
    "Power Purchase Agreement",
    "Purchase/Offtake Agreement",
    "Supply/Feedstock Agreement",
    "EPC Contract",
    "O&M Agreement",
    "Environmental Approvals/Licenses",
    "Land-Use Rights/Land Lease Agreement",
    "Land Use Permit",
    "Air Quality Permit",
    "Interconnection Agreement",
    "Construction/Building Permits",
    "Grid Connection Agreement",
    "LOI to Purchase Asset",
    "Financial Model",
    "FEED",
    "Independent Technical Report",
    "Market Studies",
}


@pytest.fixture()
def owner_access(db: Session, make_user: UserFactory) -> ProjectAccess:
    owner = make_user("owner@example.org")
    created = create_project(
        db, session_ctx(owner), fields={"title": "Abilene solar", "technology": "solar"}, now=NOW
    )
    return authorize(db, session_ctx(owner), created.project.public_id, "edit")


def _item(db: Session, access: ProjectAccess, key: str) -> PrivateProjectItem:
    return db.execute(
        sa.select(PrivateProjectItem).where(
            PrivateProjectItem.project_id == access.project.id, PrivateProjectItem.template_key == key
        )
    ).scalar_one()


QUEUED = "prop_0000000001"


def _link(db: Session, access: ProjectAccess, public_id: str = QUEUED, record_type: str = "proposal") -> None:
    """What the API's add-link route stores (lane P2): a reference the project links to."""
    db.add(
        PrivateProjectLink(
            project_id=access.project.id,
            record_type=record_type,
            record_public_id=public_id,
            relation="this_project",
        )
    )
    db.flush()


# ----------------------------------------------------------------------------- the generic pack
def test_generic_pack_is_six_folders_and_32_items_in_the_public_schema() -> None:
    document = yaml.safe_load(GENERIC_PACK_PATH.read_text(encoding="utf-8"))
    schema = json.loads(PACK_SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    assert list(Draft202012Validator(schema).iter_errors(document)) == []
    pack = generic_pack()
    assert pack.pack_version == "generic-v1" and len(pack.items) == 32
    assert Counter(item.folder for item in pack.items) == {
        "contracts": 5,
        "finance": 4,
        "security": 3,
        "permits": 8,
        "feasibility": 9,
        "legal_corporate": 3,
    }
    names = {item.name for item in pack.items}
    assert DOCS_18_NAMES <= names
    assert len(names) == 32 and len(pack.keys()) == 32


def test_generic_pack_carries_names_only_and_pillars_follow_folders() -> None:
    for item in generic_pack().items:
        assert item.guidance is None, item.key
        assert item.technologies == ()
        assert item.pillar == FOLDER_PILLARS[item.folder], item.key
    assert set(FOLDER_PILLARS) == set(CHECKLIST_FOLDERS)
    assert set(FOLDER_PILLARS.values()) == set(ITEM_PILLARS)


def test_schema_vocabularies_match_the_tables() -> None:
    schema = json.loads(PACK_SCHEMA_PATH.read_text(encoding="utf-8"))
    item = schema["$defs"]["item"]["properties"]
    assert item["folder"]["enum"] == list(CHECKLIST_FOLDERS)
    assert item["pillar"]["enum"] == list(ITEM_PILLARS)
    assert item["technologies"]["items"]["enum"] == list(PROJECT_TECHNOLOGIES)


def test_evidence_rules_attach_only_to_the_items_a_public_record_can_speak_to() -> None:
    """docs/19 §2.4: never land rights, land-use or construction permits, offtake, PPA, EPC,
    finance, security or legal items."""
    assert set(EVIDENCE_RULE_ITEMS) == set(EVIDENCE_RULES)
    keys = generic_pack().keys()
    assert set(EVIDENCE_RULE_ITEMS.values()) <= keys
    never = {
        "land_rights",
        "land_use_permit",
        "construction_permits",
        "offtake_agreement",
        "ppa",
        "epc_contract",
    }
    never |= {i.key for i in generic_pack().items if i.folder in ("finance", "security", "legal_corporate")}
    assert not never & set(EVIDENCE_RULE_ITEMS.values())
    for key in never:
        for rule in EVIDENCE_RULES:
            assert not checklist.may_suggest(key, rule)
    assert not checklist.may_suggest(None, "queue_position")


@pytest.mark.parametrize(
    "mutate, where",
    [
        (lambda d: d.update(schema="other/1"), "schema"),
        (lambda d: d["items"][0].update(pillar="completeness"), "items/0/pillar"),
        (lambda d: d["items"][1].update(folder="Contracts"), "items/1/folder"),
        (lambda d: d["items"][2].update(status="verified"), "items/2"),
        (lambda d: d["items"][3].update(technologies=["solar", "solar"]), "items/3/technologies"),
        (lambda d: d.update(items=[]), "items"),
    ],
)
def test_validate_pack_names_where_never_what(mutate: object, where: str) -> None:
    document = yaml.safe_load(GENERIC_PACK_PATH.read_text(encoding="utf-8"))
    assert callable(mutate)
    mutate(document)
    with pytest.raises(PackError) as excinfo:
        validate_pack(document)
    assert where in str(excinfo.value)


def test_a_repeated_key_is_refused() -> None:
    document = yaml.safe_load(GENERIC_PACK_PATH.read_text(encoding="utf-8"))
    document["items"].append(dict(document["items"][0]))
    with pytest.raises(PackError, match="repeats a key"):
        validate_pack(document)


# --------------------------------------------------------------------------- a project's checklist
def test_new_project_gets_the_generic_items_all_not_started(db: Session, owner_access: ProjectAccess) -> None:
    items = project_items(db, owner_access)
    assert len(items) == 32
    assert {i.status for i in items} == {"not_started"}
    assert {i.pack_version for i in items} == {"generic-v1"}
    assert [i.folder for i in items][:5] == ["contracts"] * 5
    assert checklist.guidance_for(db, [i.template_key or "" for i in items]) == {}


def test_status_is_the_users_own_and_never_verified(db: Session, owner_access: ProjectAccess) -> None:
    ppa = _item(db, owner_access, "ppa")
    for status in ("verified", "certified", "approved", ""):
        with pytest.raises(ProblemError) as excinfo:
            set_item_status(db, owner_access, ppa.id, status)
        assert excinfo.value.code == "validation_error"
    set_item_status(db, owner_access, ppa.id, "done", now=NOW)
    assert ppa.status == "done" and ppa.updated_by_user_id == owner_access.user.id
    row = (
        db.execute(
            sa.select(PrivateProjectActivity).where(PrivateProjectActivity.action == "item_status_changed")
        )
        .scalars()
        .one()
    )
    assert row.detail == {"item_id": str(ppa.id), "status": "done"}
    assert checklist.SHARED_STATUS_LABELS["done"] == "Done (your status)"
    assert "verified" in checklist.EVIDENCE_LABEL and "not verified" in checklist.EVIDENCE_LABEL
    assert "Nothing here is verified" in checklist.STATUS_DISCLAIMER


def test_a_viewer_reads_but_cannot_mark(
    db: Session, owner_access: ProjectAccess, make_user: UserFactory
) -> None:
    viewer = make_user("viewer@example.org")
    db.add(
        PrivateProjectMember(
            project_id=owner_access.project.id, user_id=viewer.id, role="viewer", added_at=NOW
        )
    )
    db.flush()
    access = authorize(db, session_ctx(viewer), owner_access.project.public_id, "read")
    assert len(project_items(db, access)) == 32
    ppa = _item(db, owner_access, "ppa")
    with pytest.raises(RoleForbidden):
        set_item_status(db, access, ppa.id, "done")
    with pytest.raises(RoleForbidden):
        add_custom_item(db, access, name="Mine", folder="contracts")


def test_an_item_from_another_project_is_not_found(
    db: Session, owner_access: ProjectAccess, make_user: UserFactory
) -> None:
    other = make_user("other@example.org")
    theirs = create_project(db, session_ctx(other), fields={"title": "Theirs"})
    their_item = project_items(db, theirs)[0]
    with pytest.raises(ProblemError) as excinfo:
        set_item_status(db, owner_access, their_item.id, "done")
    assert excinfo.value.status == 404
    assert their_item.status == "not_started"


def test_readiness_is_counts_by_pillar_and_no_score(db: Session, owner_access: ProjectAccess) -> None:
    fields = {f.name for f in dataclasses.fields(checklist.PillarReadiness)}
    assert fields == {"pillar", "label", "done", "total", "not_applicable", "with_suggested_evidence"}
    assert not {name for name in vars(checklist) if "score" in name.lower()}

    lines = readiness(db, owner_access)
    assert [line.pillar for line in lines] == [
        "completeness",
        "fundamentals",
        "counterparties",
        "regulatory_site",
        "sponsor",
    ]
    assert {line.pillar: line.total for line in lines} == {
        "completeness": 32,
        "fundamentals": 16,
        "counterparties": 5,
        "regulatory_site": 8,
        "sponsor": 3,
    }
    set_item_status(db, owner_access, _item(db, owner_access, "ppa").id, "done")
    set_item_status(db, owner_access, _item(db, owner_access, "supply_agreement").id, "not_applicable")
    set_item_status(db, owner_access, _item(db, owner_access, "financial_model").id, "in_progress")
    ia = _item(db, owner_access, "interconnection_agreement")
    _link(db, owner_access)
    record_suggestion(
        db,
        owner_access,
        item_id=ia.id,
        record_type="proposal",
        record_public_id=QUEUED,
        rule="queue_position",
    )
    by_pillar = {line.pillar: line for line in readiness(db, owner_access)}
    assert (by_pillar["counterparties"].done, by_pillar["counterparties"].total) == (1, 4)
    assert by_pillar["counterparties"].not_applicable == 1
    assert (by_pillar["completeness"].done, by_pillar["completeness"].total) == (1, 31)
    assert by_pillar["regulatory_site"].with_suggested_evidence == 1
    assert by_pillar["completeness"].with_suggested_evidence == 1
    assert by_pillar["fundamentals"].done == 0  # in progress is not done
    # The live set from the API wins over stored rows.
    assert {
        line.pillar: line.with_suggested_evidence
        for line in readiness(db, owner_access, evidence_item_ids=[])
    } == dict.fromkeys(checklist.READINESS_PILLARS, 0)


def test_progress_for_many_projects_in_one_query(
    db: Session, owner_access: ProjectAccess, make_user: UserFactory
) -> None:
    other = create_project(db, session_ctx(make_user()), fields={"title": "Other"})
    set_item_status(db, owner_access, _item(db, owner_access, "ppa").id, "done")
    set_item_status(db, owner_access, _item(db, owner_access, "feed").id, "not_applicable")
    progress = progress_by_project(db, [owner_access, other])
    assert progress[owner_access.project.id] == checklist.Progress(done=1, total=31)
    assert progress[other.project.id] == checklist.Progress(done=0, total=32)
    assert progress_by_project(db, []) == {}


def test_suggestions_are_references_and_decisions_never_change_status(
    db: Session, owner_access: ProjectAccess
) -> None:
    ia = _item(db, owner_access, "interconnection_agreement")
    _link(db, owner_access)
    row = record_suggestion(
        db,
        owner_access,
        item_id=ia.id,
        record_type="proposal",
        record_public_id=QUEUED,
        rule="queue_position",
    )
    again = record_suggestion(
        db,
        owner_access,
        item_id=ia.id,
        record_type="proposal",
        record_public_id=QUEUED,
        rule="queue_position",
    )
    assert row is not None and again is not None
    assert again.id == row.id and row.state == "suggested"
    assert {c.name for c in row.__table__.columns} >= {"record_type", "record_public_id", "rule", "state"}
    assert not {c.name for c in row.__table__.columns} & {"text", "statement", "value", "summary"}
    with pytest.raises(ValueError):
        record_suggestion(
            db,
            owner_access,
            item_id=_item(db, owner_access, "ppa").id,
            record_type="proposal",
            record_public_id=QUEUED,
            rule="queue_position",
        )
    with pytest.raises(ValueError):
        record_suggestion(
            db, owner_access, item_id=ia.id, record_type="proposal", record_public_id=QUEUED, rule="made_up"
        )

    decide_evidence(db, owner_access, item_id=ia.id, evidence_id=row.id, decision="accepted", now=NOW)
    assert row.state == "accepted" and ia.status == "not_started"
    decide_evidence(db, owner_access, item_id=ia.id, evidence_id=row.id, decision="dismissed", now=NOW)
    assert row.state == "dismissed" and ia.status == "not_started"
    assert readiness(db, owner_access)[0].with_suggested_evidence == 0
    with pytest.raises(ProblemError):
        decide_evidence(db, owner_access, item_id=ia.id, evidence_id=row.id, decision="verified")
    with pytest.raises(ProblemError) as excinfo:
        decide_evidence(
            db,
            owner_access,
            item_id=_item(db, owner_access, "ppa").id,
            evidence_id=row.id,
            decision="accepted",
        )
    assert excinfo.value.status == 404
    actions = db.execute(sa.select(PrivateProjectActivity.action)).scalars().all()
    assert actions.count("evidence_accepted") == 1 and actions.count("evidence_dismissed") == 1


def test_custom_items(db: Session, owner_access: ProjectAccess) -> None:
    item = add_custom_item(db, owner_access, name="  Water   rights ", folder="permits", now=NOW)
    assert (item.name, item.pillar, item.template_key, item.status) == (
        "Water rights",
        "regulatory_site",
        None,
        "not_started",
    )
    second = add_custom_item(
        db, owner_access, name="Tax equity term sheet", folder="finance", pillar="sponsor"
    )
    assert second.pillar == "sponsor"
    for kwargs in (
        {"name": " ", "folder": "permits"},
        {"name": "x" * 201, "folder": "permits"},
        {"name": "x", "folder": "Permits"},
        {"name": "x", "folder": "permits", "pillar": "completeness"},
    ):
        with pytest.raises(ProblemError):
            add_custom_item(db, owner_access, **kwargs)  # type: ignore[arg-type]
    assert len(project_items(db, owner_access)) == 34


def test_a_loaded_pack_shapes_new_projects_and_lends_its_guidance(
    db: Session, owner_access: ProjectAccess, make_user: UserFactory
) -> None:
    db.add_all(
        [
            ChecklistTemplateItem(
                pack_version="private-1",
                key="ppa",
                folder="contracts",
                pillar="counterparties",
                name="Power Purchase Agreement",
                guidance="What a PPA is.",
                sort_order=1,
            ),
            ChecklistTemplateItem(
                pack_version="private-1",
                key="supply_agreement",
                folder="contracts",
                pillar="fundamentals",
                name="Fuel Supply Agreement",
                sort_order=2,
                technologies=["gas_cc", "biomass"],
            ),
        ]
    )
    db.flush()
    # Existing projects keep their items but see the guidance, read live.
    assert checklist.guidance_for(db, ["ppa", "feed"]) == {"ppa": "What a PPA is."}
    solar = create_project(db, session_ctx(make_user()), fields={"title": "Solar", "technology": "solar"})
    assert [(i.template_key, i.pack_version) for i in project_items(db, solar)] == [("ppa", "private-1")]
    gas = create_project(db, session_ctx(make_user()), fields={"title": "Gas", "technology": "gas_cc"})
    items = {i.template_key: i for i in project_items(db, gas)}
    assert set(items) == {"ppa", "supply_agreement"} and items["supply_agreement"].pillar == "fundamentals"
    unknown = create_project(db, session_ctx(make_user()), fields={"title": "Not decided"})
    assert len(project_items(db, unknown)) == 2
    assert len(project_items(db, owner_access)) == 32


@pytest.mark.parametrize("name", ["Water\x00rights", "Water rights\x7f", "Water \ud800rights", "\x1b[31mred"])
def test_an_item_name_a_database_cannot_store_is_refused(
    db: Session, owner_access: ProjectAccess, name: str
) -> None:
    """NUL in a Postgres text column raises an error whose message carries the statement's
    parameters (other items' names, the project id) into logs and error reports: refused first."""
    with pytest.raises(ProblemError) as excinfo:
        add_custom_item(db, owner_access, name=name, folder="permits")
    assert (excinfo.value.code, excinfo.value.status) == ("validation_error", 400)
    assert "rights" not in (excinfo.value.detail or "")
    assert len(project_items(db, owner_access)) == 32


def test_a_suggestion_must_reference_a_record_the_project_links_to(
    db: Session, owner_access: ProjectAccess
) -> None:
    ia = _item(db, owner_access, "interconnection_agreement")

    def suggest(record_type: str = "proposal", public_id: str = QUEUED) -> object:
        return record_suggestion(
            db,
            owner_access,
            item_id=ia.id,
            record_type=record_type,
            record_public_id=public_id,
            rule="queue_position",
        )

    for record_type, public_id in (
        ("proposal", QUEUED),  # well formed, but not linked yet
        ("proposal", "prop_1"),
        ("proposal", "opp_0000000001"),
        ("proposal", "prop_0000000001\x00"),
        ("project", QUEUED),
    ):
        with pytest.raises(ProblemError) as excinfo:
            suggest(record_type, public_id)
        assert excinfo.value.status == 404
    _link(db, owner_access, public_id="prop_0000000002")
    with pytest.raises(ProblemError):
        suggest()  # another record is linked, not this one
    _link(db, owner_access)
    assert suggest() is not None
    assert db.execute(sa.select(sa.func.count()).select_from(PrivateProjectEvidence)).scalar() == 1


def test_an_archived_project_records_no_new_suggestion(db: Session, owner_access: ProjectAccess) -> None:
    from services.projects.authz import set_archived

    ia = _item(db, owner_access, "interconnection_agreement")
    _link(db, owner_access)
    _link(db, owner_access, public_id="prop_0000000002")
    kept = record_suggestion(
        db,
        owner_access,
        item_id=ia.id,
        record_type="proposal",
        record_public_id=QUEUED,
        rule="queue_position",
    )
    owner = authorize(db, session_ctx(owner_access.user), owner_access.project.public_id, "archive")
    set_archived(db, owner, True, now=NOW)
    reader = authorize(db, session_ctx(owner_access.user), owner_access.project.public_id, "read")
    again = record_suggestion(
        db, reader, item_id=ia.id, record_type="proposal", record_public_id=QUEUED, rule="queue_position"
    )
    assert kept is not None and again is not None and again.id == kept.id
    fresh = record_suggestion(
        db,
        reader,
        item_id=ia.id,
        record_type="proposal",
        record_public_id="prop_0000000002",
        rule="queue_position",
    )
    assert fresh is None
    assert db.execute(sa.select(sa.func.count()).select_from(PrivateProjectEvidence)).scalar() == 1
