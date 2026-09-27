"""Document metadata and link (US-302 AC1; docs/21 §3.8; api/openapi.yaml `getDocument`).

`GET /v1/documents/{document_id}` returns the `Document` shape on every tier: title, `doc_type`,
`published_date`, identifiers, the provenance quartet, the subject, and storage facts. Bytes travel
only when all of these hold (api/openapi.yaml `Document.download_url`; docs/02 §4 news and
personal-data rules): `storage_policy = stored`, the document's licence has
`allows_raw_publication` (a stored copy *is* the raw row), `personal_data_flag = false`, and a
stored object actually exists. Otherwise `download_url` is `null`, the record's `source_url`
("view at source") is the link, and a `redactions[]` entry says why when a stored copy is held
back. News is never `stored` (the model's own rule), so a news item is always link-out.

Visibility, composed from `services/api/visibility.py`'s existing pieces (that module has no
document predicate and is not edited here): the document's licence must be in
`PUBLISHABLE_REUSE_CLASSES`, its source's `publish_state` must be one the caller's tier may read,
and a proposal or opportunity subject must itself pass `proposal_visibility_filter` /
`opportunity_visibility_filter` -- a document never discloses a record its tier cannot see.
Anything else is the same `404` an unknown id gets.

**Nothing writes `document` rows today.** No connector or pipeline step creates them
(`grep -rn "Document(" services pipeline` finds the model and one admin test fixture), so this
route serves what exists -- nothing, on a real load -- and is proven against fixtures
(`tests/test_api_documents.py`). The bytes route reads a local directory, `DOCUMENT_DIR` (default
`data/documents/` under the repository), keyed by `Document.object_key`; the production
follow-up, like exports', is R2 with a pre-signed `download_url` and a real
`download_expires_at` (always `null` here: the link does not expire, it is re-checked on every
request instead).
"""

from __future__ import annotations

import os
import pathlib
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import FileResponse
from sqlalchemy import ColumnElement, and_, exists, or_, select
from sqlalchemy.orm import Session

from services.api.auth import AuthContext, get_auth_context
from services.api.common import API_HOST, iso
from services.api.deps import get_db
from services.api.errors import not_found
from services.api.serialize import (
    build_envelope,
    build_licence_summary,
    build_meta,
    licence_summary_row,
    provenance_quartet,
)
from services.api.visibility import (
    _PERMITTED_SOURCE_STATES,
    PUBLISHABLE_REUSE_CLASSES,
    opportunity_visibility_filter,
    proposal_visibility_filter,
)
from services.db.models import Document, Licence, Opportunity, Organization, Proposal, Source

router = APIRouter()

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
DEFAULT_DOCUMENT_DIR = REPO_ROOT / "data" / "documents"


def document_dir() -> pathlib.Path:
    """`DOCUMENT_DIR` from the environment (docs/60-deployment.md §5), read per call so a test's
    `monkeypatch.setenv` takes effect. Not created here: a missing directory means no stored
    bytes, which is today's truth."""
    return pathlib.Path(os.environ.get("DOCUMENT_DIR") or DEFAULT_DOCUMENT_DIR)


def _stored_path(object_key: str | None) -> pathlib.Path | None:
    """The stored file for `object_key`, or `None` when there is none or the key would resolve
    outside `DOCUMENT_DIR`."""
    if not object_key:
        return None
    root = document_dir().resolve()
    path = (root / object_key).resolve()
    if root not in path.parents or not path.is_file():
        return None
    return path


def document_visibility_clauses(entitlement: str) -> list[ColumnElement[bool]]:
    subject_visible = or_(
        Document.subject_type.in_(("none", "organization")),
        and_(
            Document.subject_type == "proposal",
            exists(
                select(Proposal.id).where(
                    Proposal.id == Document.subject_id, *proposal_visibility_filter(entitlement)
                )
            ),
        ),
        and_(
            Document.subject_type == "opportunity",
            exists(
                select(Opportunity.id).where(
                    Opportunity.id == Document.subject_id, *opportunity_visibility_filter(entitlement)
                )
            ),
        ),
    )
    return [
        exists(
            select(Licence.id).where(
                Licence.id == Document.licence_id, Licence.reuse_class.in_(PUBLISHABLE_REUSE_CLASSES)
            )
        ),
        exists(
            select(Source.id).where(
                Source.id == Document.source_id,
                Source.publish_state.in_(_PERMITTED_SOURCE_STATES.get(entitlement, ("public",))),
            )
        ),
        subject_visible,
    ]


def _visible_document(db: Session, document_id: str, entitlement: str) -> Document | None:
    return db.scalar(
        select(Document).where(Document.public_id == document_id, *document_visibility_clauses(entitlement))
    )


def _source_and_licence(db: Session, doc: Document) -> tuple[Source, Licence]:
    """`document` carries `source_id`/`licence_id` foreign keys but no ORM relationships."""
    source, licence = db.get(Source, doc.source_id), db.get(Licence, doc.licence_id)
    assert source is not None and licence is not None  # noqa: S101 - NOT NULL foreign keys
    return source, licence


def bytes_withheld_reason(doc: Document, licence: Licence) -> str | None:
    """`None` when the stored bytes may travel; otherwise the `RedactionReason` for holding them
    back. Only meaningful for `storage_policy = stored`: a link-only document has no bytes."""
    if doc.personal_data_flag:
        return "personal_data"
    if not licence.allows_raw_publication:
        return "licence"
    return None


def _subject_public_id(db: Session, doc: Document) -> str | None:
    if doc.subject_id is None:
        return None
    row: Proposal | Opportunity | Organization | None = None
    if doc.subject_type == "proposal":
        row = db.get(Proposal, doc.subject_id)
    elif doc.subject_type == "opportunity":
        row = db.get(Opportunity, doc.subject_id)
    elif doc.subject_type == "organization":
        row = db.get(Organization, doc.subject_id)
    return row.public_id if row is not None else None


def serialize_document(db: Session, doc: Document) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """`(Document, redactions)`."""
    source, licence = _source_and_licence(db, doc)
    redactions: list[dict[str, Any]] = []
    download_url = None
    if doc.storage_policy == "stored":
        reason = bytes_withheld_reason(doc, licence)
        if reason is not None:
            redactions.append(
                {
                    "public_id": doc.public_id,
                    "field": "download_url",
                    "reason": reason,
                    "source_id": doc.source_id,
                    "note": "stored copy withheld; follow provenance.source_url",
                }
            )
        elif _stored_path(doc.object_key) is not None:
            download_url = f"{API_HOST}/v1/documents/{doc.public_id}/content"
    data = {
        "document_id": doc.public_id,
        "title": doc.title,
        "doc_type": doc.doc_type,
        "published_date": iso(doc.published_date),
        "url": f"{API_HOST}/v1/documents/{doc.public_id}",
        "provenance": provenance_quartet(
            source, licence, source_url=doc.source_url, retrieved_at=doc.retrieved_at
        ),
        "subject_type": doc.subject_type,
        "subject_id": _subject_public_id(db, doc),
        "identifiers": doc.identifiers or {},
        "storage_policy": doc.storage_policy,
        "content_type": doc.content_type,
        "byte_size": doc.byte_size,
        "sha256": doc.sha256,
        "page_count": doc.page_count,
        "text_extracted": doc.text_extracted,
        "personal_data_flag": doc.personal_data_flag,
        "robots_opt_out": doc.robots_opt_out,
        "download_url": download_url,
        "download_expires_at": None,
    }
    return data, redactions


@router.get("/v1/documents/{document_id}")
def get_document(
    document_id: str,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
) -> Any:
    doc = _visible_document(db, document_id, ctx.entitlement)
    if doc is None:
        raise not_found(request.url.path)
    data, redactions = serialize_document(db, doc)
    source, licence = _source_and_licence(db, doc)
    return build_envelope(
        data,
        meta=build_meta(lag_days=0, tier=ctx.entitlement),
        licence_summary=build_licence_summary([licence_summary_row(source, licence, doc.retrieved_at)]),
        redactions=redactions,
    )


@router.get("/v1/documents/{document_id}/content")
def get_document_content(
    document_id: str,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
) -> Response:
    """The stored bytes, under exactly the conditions `download_url` is non-null; every other
    case is the same `404`, so the route never confirms a withheld copy exists."""
    doc = _visible_document(db, document_id, ctx.entitlement)
    if doc is None or doc.storage_policy != "stored":
        raise not_found(request.url.path)
    _source, licence = _source_and_licence(db, doc)
    if bytes_withheld_reason(doc, licence) is not None:
        raise not_found(request.url.path)
    path = _stored_path(doc.object_key)
    if path is None:
        raise not_found(request.url.path)
    return FileResponse(
        path,
        media_type=doc.content_type or "application/octet-stream",
        headers={"X-Source-Url": doc.source_url, "X-Licence-Id": doc.licence_id},
    )


__all__ = [
    "bytes_withheld_reason",
    "document_dir",
    "document_visibility_clauses",
    "router",
    "serialize_document",
]
