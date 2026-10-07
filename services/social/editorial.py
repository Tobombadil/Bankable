"""Editorial rules and templates (docs/32-social-operating-playbook.md §3).

`SocialEvent` is the structured-field contract templates render from -- "nothing else is passed
to the model" (docs/32 §3.2). It is deliberately richer than the Sprint 1 prototype event shape in
`pipeline/diff.py` (which carries only `event_type, record_id, source_id, field, before, after,
observed_at` -- one changed field, no capacity/technology/place). `services.social.cli` builds a
`SocialEvent` by joining a diff-style event row against the canonical record it changed; see
`from_diff_row` for that adapter and its documented limits.

Nothing here calls a model. Templates are string-formatted in code (docs/32 §4.2: "deterministic
first"); the compression-on-overflow step the playbook reserves for a model is done instead by
dropping optional clauses in a fixed priority order, per §4.2's fallback rule ("the deterministic
template is used with clauses dropped in the order defined in the template file").
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import logging
import os
import re
from typing import Any, Literal
from urllib.parse import urlencode, urlsplit, urlunsplit

from services.api.common import DOMAIN, WEB_HOST
from services.labels import lifecycle_label, technology_label
from services.posture import platform_posture, publishable_reuse_classes
from services.social.models import PostDraft, ValidationResult
from services.social.textgate import BareNoneError, contains_bare_none, reject_bare_none

logger = logging.getLogger(__name__)

EventType = str  # canonical docs/21 §7.3-style names, e.g. "proposal.new"

#: Event types the playbook actually turns into a post (docs/32 §3.1 table). Anything else --
#: including every docs/21 §7.3 type not listed here (`capacity_changed`, `field_changed`,
#: `source_health_changed`, `merged`, ...) -- is default-deny: "never posted: source-health
#: events, resolver merges, enrichment-only changes ... " (docs/32 §3.1).
POSTABLE_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "proposal.new",
        "proposal.status_changed",
        "proposal.withdrawn",
        "opportunity.rfp_opened",
        "opportunity.rfp_closing",
        "opportunity.awarded",
        "funding.cancelled",
        "funding.reinstated",
        "digest.weekly",
    }
)

#: The proposal lifecycle ladder (`data/vocabulary/lifecycle_states.yaml`; docs/22 §23's survivorship
#: order). `withdrawn`, `cancelled` and `unknown` are off the ladder: a withdrawal is its own event type
#: (`proposal.withdrawn`) and `unknown` is not a stage.
LIFECYCLE_LADDER: tuple[str, ...] = (
    "announced",
    "filed",
    "studied",
    "permitted",
    "contracted",
    "under_construction",
    "built",
)

#: docs/32 §3.1 "any transition between canonical stages": every *forward* move on the ladder, skips
#: included (2026-10-07, content audit F2). The old stepwise whitelist had no `announced` and no skips,
#: and EIA-860M routinely jumps stages (permitted -> under_construction is 17 of the 22 status changes
#: in the 2026-09-30 store), so it refused every real transition. A backward move is not posted: it is
#: a correction or a reclassification, never news.
PROPOSAL_CANONICAL_TRANSITIONS: frozenset[tuple[str, str]] = frozenset(
    (a, b) for i, a in enumerate(LIFECYCLE_LADDER) for b in LIFECYCLE_LADDER[i + 1 :]
)

#: States before construction: the only ones a post may call "proposed" (content audit F3).
PRE_CONSTRUCTION_STATES: frozenset[str] = frozenset(LIFECYCLE_LADDER[:5])

#: docs/32 §3.1: LinkedIn joins a status_changed post only "for reaching interconnection
#: agreement, construction or operation".
LINKEDIN_STATUS_TARGETS: frozenset[str] = frozenset({"contracted", "under_construction", "built"})

#: Reuse classes allowed to leave the building at all (docs/21 §8, CLAUDE.md guardrails), from the
#: platform posture (`services/posture.py`, docs/26) so a post can never be drafted for a class the
#: API predicate would not show.
POSTURE: str = platform_posture()
PUBLISHABLE_REUSE_CLASSES: frozenset[str] = frozenset(publishable_reuse_classes(POSTURE))

#: The classes a social post may draw on: docs/32 §4.3 gate 8, "Source `reuse` ∈ {open, attribution}",
#: under either posture (2026-10-07, content audit F6, legal L-9). The `noncommercial` class publishes
#: on the site under the noncommercial posture (docs/26 §1), but a post is a copy handed to a platform
#: whose terms take a licence over it to use, adapt and distribute the content, which is the downstream
#: commercial use docs/26 §3 (iii) says no `noncommercial` row may flow to, and the RRC's grant is for
#: unaltered copies only. A `noncommercial` record is still reachable from the site; it is never the
#: subject of a post until the owner and counsel decide otherwise (docs/13 §7 item 15).
SOCIAL_REUSE_CLASSES: frozenset[str] = frozenset(PUBLISHABLE_REUSE_CLASSES - {"noncommercial"})

#: docs/32 §3.4 banned words (facts-only style guide).
BANNED_WORDS: tuple[str, ...] = (
    "likely",
    "reportedly",
    "appears",
    "sources say",
    "rumoured",
    "rumored",
    "plans to",
    "controversial",
    "surprising",
    "massive",
    "huge",
    "game-changing",
)

#: USPS state names for the subset of jurisdictions the connector set covers (docs/00-PLAN.md
#: repo layout; `data/sources.yaml` US rows). LinkedIn/email use the full name (docs/32 §3.4);
#: short formats use the two-letter code already on the record. An unmapped code falls back to
#: itself rather than guessing.
US_STATE_NAMES: dict[str, str] = {
    "AL": "Alabama",
    "AK": "Alaska",
    "AZ": "Arizona",
    "AR": "Arkansas",
    "CA": "California",
    "CO": "Colorado",
    "CT": "Connecticut",
    "DE": "Delaware",
    "DC": "District of Columbia",
    "FL": "Florida",
    "GA": "Georgia",
    "HI": "Hawaii",
    "ID": "Idaho",
    "IL": "Illinois",
    "IN": "Indiana",
    "IA": "Iowa",
    "KS": "Kansas",
    "KY": "Kentucky",
    "LA": "Louisiana",
    "ME": "Maine",
    "MD": "Maryland",
    "MA": "Massachusetts",
    "MI": "Michigan",
    "MN": "Minnesota",
    "MS": "Mississippi",
    "MO": "Missouri",
    "MT": "Montana",
    "NE": "Nebraska",
    "NV": "Nevada",
    "NH": "New Hampshire",
    "NJ": "New Jersey",
    "NM": "New Mexico",
    "NY": "New York",
    "NC": "North Carolina",
    "ND": "North Dakota",
    "OH": "Ohio",
    "OK": "Oklahoma",
    "OR": "Oregon",
    "PA": "Pennsylvania",
    "RI": "Rhode Island",
    "SC": "South Carolina",
    "SD": "South Dakota",
    "TN": "Tennessee",
    "TX": "Texas",
    "UT": "Utah",
    "VT": "Vermont",
    "VA": "Virginia",
    "WA": "Washington",
    "WV": "West Virginia",
    "WI": "Wisconsin",
    "WY": "Wyoming",
}

CHANNEL_LIMITS: dict[str, int] = {"bluesky": 300, "linkedin": 3000, "x": 280}

#: docs/13-legal-outreach-and-social.md §6.5: the fixed, owner-approved disclosure text for an
#: automated social account. X and Bluesky auto-post (post-review, pre-graduation); LinkedIn
#: never auto-publishes (docs/32 §1.4, §4.6) and relies on the Page's About statement plus the
#: human-reviewed exemption in EU AI Act Art. 50(4) (docs/13 §6.2), so no per-post disclosure
#: line is required there. This is a recorded assumption (services/social/README.md), not a
#: re-derivation of docs/13.
#:
#: The operator identity in the text is read from the environment at call time — `PRODUCT_NAME`,
#: `PRODUCT_URL`, `PRODUCT_CONTACT_EMAIL` — with defaults naming the product (Infraque, the site
#: host `services.api.common.WEB_HOST`, `hello@` on its domain). The previous constant named the
#: old company (docs/50-audit-2026-09-18.md §3.2), and a disclosure that names the wrong operator
#: is a false statement about who runs the account (docs/13 §6.1 FTC; §6.3 B.O.T. Act).
DEFAULT_PRODUCT_NAME = "Infraque"
DEFAULT_PRODUCT_URL = WEB_HOST
DEFAULT_PRODUCT_CONTACT_EMAIL = f"hello@{DOMAIN}"


def product_identity() -> tuple[str, str, str]:
    """`(name, url, contact_email)` for the disclosure texts, environment first."""
    name = os.environ.get("PRODUCT_NAME", "").strip() or DEFAULT_PRODUCT_NAME
    url = os.environ.get("PRODUCT_URL", "").strip() or DEFAULT_PRODUCT_URL
    contact = os.environ.get("PRODUCT_CONTACT_EMAIL", "").strip() or DEFAULT_PRODUCT_CONTACT_EMAIL
    return name, url, contact


#: The posture clause of the automated-account disclosure (docs/13 §6.5, two variants; 2026-10-07,
#: content audit F6, legal L-9). The text used to say "commercial in nature" while the platform runs
#: under the noncommercial posture (docs/26 §1) and says so on `/v1/health`, `/about` and `/pricing`:
#: two public statements that contradict each other, on exactly the question the posture turns on.
#: The clause now follows the posture this process applies (`POSTURE`, read once at import like the
#: reuse-class gate), so the account can never claim a posture the gates are not applying.
POSTURE_DISCLOSURE: dict[str, str] = {
    "commercial": "Posts are generated from public records and are commercial in nature.",
    "noncommercial": (
        "Posts are generated from public records; the platform operates under a noncommercial posture."
    ),
}


def disclosure_texts() -> dict[str, str]:
    """The docs/13 §6.5 texts with the current operator identity filled in. Built per call, not
    at import, so a process that sets `PRODUCT_*` after importing this module still discloses
    the right operator."""
    name, url, contact = product_identity()
    host = url.removeprefix("https://").removeprefix("http://").rstrip("/")
    posture_clause = POSTURE_DISCLOSURE.get(POSTURE, POSTURE_DISCLOSURE["commercial"])
    automated = (
        f"Automated feed run by {name} ({host}). {posture_clause} "
        f"Not monitored for replies — contact: {contact}."
    )
    return {
        "bluesky": automated,
        "x": automated,
        "linkedin": (
            # docs/13 §6.5's human-reviewed text; the internal document reference it used to end
            # with is not something a reader of the page can open, so it is no longer printed.
            f"Human-reviewed: generated from structured public data by {name}'s pipeline and "
            "reviewed by a named editor before publication."
        ),
    }


def automated_line_for(event: SocialEvent) -> str:
    """docs/13 §6.5's trailing line for an item published without human review (the EU AI Act
    Art. 50(4) route): carried in the body of every auto-published post, never on a reviewed one
    (content audit F11, legal L-8). The source is named as the register, not the credit line, which
    may be a mandated statement that names no source."""
    return f"Auto-generated summary from {event.source_name}; not human-reviewed."


def disclosure_text_for(channel: str) -> str:
    return disclosure_texts()[channel]


class _DisclosureTexts(dict[str, str]):
    """Backward-compatible `DISCLOSURE_TEXT[channel]` that resolves the environment on every
    lookup, so existing callers and tests keep their subscript syntax."""

    def __getitem__(self, channel: str) -> str:
        return disclosure_text_for(channel)

    def __contains__(self, channel: object) -> bool:
        return channel in disclosure_texts()


DISCLOSURE_TEXT: dict[str, str] = _DisclosureTexts()

#: docs/32 §1.5: $0.20 per post containing a URL (every post does), plus an amortised
#: metrics-read cost (7 owned reads/post at $0.001 each -- docs/32 §1.1, §4.9).
X_COST_PER_POST_USD = 0.20 + 7 * 0.001
BLUESKY_COST_PER_POST_USD = 0.0
LINKEDIN_COST_PER_POST_USD = 0.0

#: docs/32 §4.2: "per-post model cost budget <= $0.01" -- always $0.00 here since no model is
#: ever called (task rule: deterministic templates only).
MODEL_COST_PER_POST_USD = 0.0


def cost_estimate_usd(channel: str) -> float:
    return {
        "x": X_COST_PER_POST_USD,
        "bluesky": BLUESKY_COST_PER_POST_USD,
        "linkedin": LINKEDIN_COST_PER_POST_USD,
    }[channel] + MODEL_COST_PER_POST_USD


@dataclasses.dataclass(frozen=True)
class EditorialConfig:
    """Thresholds from docs/32 §3.1. Configuration, not code, per that section's header; this
    dataclass is the code-level default until `config/social.yaml` (docs/32 §4.1) exists --
    devops/backend own that file, out of this package's write scope."""

    proposal_new_mw: float = 50.0
    proposal_new_high_volume_iso_mw: float = 100.0
    high_volume_isos: frozenset[str] = frozenset({"CAISO", "ERCOT", "SPP", "MISO"})
    high_volume_technologies: frozenset[str] = frozenset(
        {"solar", "storage", "battery storage", "solar+storage", "solar_storage"}
    )
    proposal_new_load_mw: float = 100.0
    proposal_new_voltage_kv: float = 100.0
    proposal_new_capex_usd: float = 100_000_000.0
    linkedin_mw: float = 200.0
    linkedin_capex_usd: float = 250_000_000.0
    linkedin_technologies: frozenset[str] = frozenset({"nuclear", "lng", "ccs"})
    linkedin_voltage_kv: float = 230.0
    linkedin_withdrawn_mw: float = 200.0
    rfp_closing_windows_days: tuple[int, ...] = (14, 3)
    dedupe_window_days: int = 7


DEFAULT_CONFIG = EditorialConfig()


@dataclasses.dataclass(frozen=True)
class SocialEvent:
    """The structured field set docs/32 §3.2 lists as available to templates, plus the event
    envelope (id/type/date/subject) needed to route and dedupe it. All business fields are
    optional: "omit a clause if its field is null; never invent a value" (docs/32 §3.3)."""

    event_id: str
    event_type: EventType
    event_date: dt.date
    subject_type: Literal["proposal", "opportunity"]
    subject_id: str
    source_id: str
    source_name: str
    source_url: str
    retrieved_at: dt.datetime
    reuse_class: str
    page_url: str
    #: Days this event's public release trails its publication, or `None` for "no delay to
    #: disclose". **Always `None` since 2026-09-21**, when the owner dropped the last delay (the
    #: ISO change-event one) and its per-source knob: `services/social/db_events.py` derives it
    #: from the event's own `public_at - published_at`, which the loader now always writes as
    #: zero. Every `if event.lag_days` below is therefore dormant, and that is deliberate — do
    #: not tidy them away. They are *descriptive* ("this feed runs N days behind"), keyed on a
    #: runtime value rather than a constant, so while the value is zero they say nothing and if a
    #: delay ever returned they would state the true number instead of being reconstructed from
    #: memory. `docs/04` D-28 makes the same argument for the site's tier banner. The one clause
    #: that was *removed* rather than left dormant was the pricing page's "ISO change events as
    #: they happen, unlike Free" tier flag, because that is a *comparative* claim which is
    #: unconditionally false now and would be a false advertisement the moment it rendered.
    lag_days: int | None = None

    proposal_name: str | None = None
    technology: str | None = None
    capacity_mw: float | None = None
    capacity_unit: str = "MW"
    load_mw: float | None = None
    voltage_kv: float | None = None
    capex_usd: float | None = None
    county: str | None = None
    state: str | None = None
    country: str = "US"
    iso_rto: str | None = None
    queue_id: str | None = None
    docket_id: str | None = None
    solicitation_id: str | None = None
    solicitation_title: str | None = None
    status_from: str | None = None
    status_to: str | None = None
    developer_org: str | None = None
    issuer_org: str | None = None
    awardee_org: str | None = None
    award_usd: float | None = None
    award_prior_status: str | None = None
    deadline_date: dt.date | None = None
    withdrawal_reason_code: str | None = None
    funding_program: str | None = None
    digest_items: tuple[dict[str, Any], ...] | None = None

    # 2026-10-07 (content audit F3, F9, F13, F14, L-10): what the copy needs to say what the source
    # is, credit it verbatim and draft one post per project.
    #: The credit every other surface prints for this source (`services.api.serialize.source_credit`:
    #: the manifest's `attribution` verbatim, plus its statement of changes), already prefixed
    #: `Source: ` when it is only the source's name, as the record page does. Empty means
    #: `Source: {source_name}`.
    credit_line: str = ""
    #: `data/sources.yaml` category of the event's source (`generation_queue`, `registry`, ...):
    #: a post says "queue" only for a queue source.
    source_category: str | None = None
    #: The licence's name, printed on LinkedIn instead of the reuse-class token.
    licence_name: str | None = None
    #: The proposal `kind` (`load` for data centres and other large loads).
    kind: str | None = None
    #: EIA plant code when the record is an EIA generator or plant; and how many records of that
    #: plant one post covers (`None` or 1 for a single record).
    eia_plant_id: str | None = None
    member_count: int | None = None


# --------------------------------------------------------------------------------- adapter

#: `pipeline/diff.py` `EVENT_TYPES` -> the docs/21 §7.3 vocabulary name for the same change.
_DIFF_EVENT_TYPE_MAP: dict[str, str] = {
    "new": "created",
    "status_change": "status_change",
    "withdrawn": "withdrawn",
    "capacity_change": "capacity_changed",
    "cod_change": "field_changed",
    "removed": "removed",
}

#: `data/sources.yaml` categories that are demand-side (docs/21 §3.3 `opportunity`), read off
#: the source id prefix so this module never has to parse the YAML itself. Kept narrow and
#: explicit rather than guessed from the id shape.
_OPPORTUNITY_SOURCE_IDS: frozenset[str] = frozenset(
    {
        "us.utility_rfps",
        "us.muni_procurement",
        "us.grants_gov.search2",
        "us.sam_gov.opportunities",
        "us.doe.exchange_portals",
        "us.usda.rd_energy",
        "us.usaspending",
        "gb.find_a_tender",
        "eu.ted.api",
        "in.seci_mnre.tenders",
        "br.aneel.leiloes",
        "za.ipp_office.reipppp",
        "mdb.worldbank.procnotices",
        "mdb.worldbank.projects",
        "mdb.others",
    }
)


def infer_subject_type(source_id: str) -> Literal["proposal", "opportunity"]:
    """docs/22 §2 canonical record has no `kind: proposal|opportunity` column yet; this reads
    the source category instead. Recorded assumption -- see services/social/README.md."""
    return "opportunity" if source_id in _OPPORTUNITY_SOURCE_IDS else "proposal"


def from_diff_row(
    row: dict[str, Any],
    *,
    record: dict[str, Any] | None,
    source_name: str,
    source_url: str,
    reuse_class: str,
    page_url: str,
    lag_days: int | None,
    event_id: str | None = None,
) -> SocialEvent | None:
    """Build a `SocialEvent` from one `pipeline/diff.py` output row.

    `pipeline/diff.py` emits one row per **changed field** (`event_type` in
    `{new, status_change, capacity_change, cod_change, withdrawn, removed}`), not the joined
    canonical record -- a `new` row, for example, carries only
    `{event_type: "new", field: "lifecycle_state", after: "filed", ...}`, with no capacity,
    technology or place. Every size/place field this function needs comes from `record` (the
    canonical row from `pipeline/normalize.py`'s output, keyed by the same `record_id`, looked
    up by the caller -- typically the "after" snapshot passed to `diff_snapshots`).

    Returns `None` when the row cannot be mapped to a docs/32 §3.1 postable event type (field
    -level changes like `capacity_change`/`cod_change` are never posted on their own, and
    `removed` is ambiguous -- docs/32 §3.1's default-deny -- so both return `None` here rather
    than guess).
    """
    diff_type = row.get("event_type")
    if diff_type not in ("new", "status_change", "withdrawn"):
        return None
    subject_id = str(row["record_id"])
    source_id = str(row["source_id"])
    subject_type = infer_subject_type(source_id)
    observed_at = row.get("observed_at")
    event_date = (
        dt.datetime.fromisoformat(observed_at).date()
        if isinstance(observed_at, str)
        else dt.datetime.now(dt.UTC).date()
    )
    retrieved_at_raw = (record or {}).get("retrieved_at")
    retrieved_at = (
        dt.datetime.fromisoformat(retrieved_at_raw)
        if isinstance(retrieved_at_raw, str)
        else dt.datetime.now(dt.UTC)
    )

    canonical_type = (
        "proposal.new"
        if diff_type == "new"
        else ("proposal.withdrawn" if diff_type == "withdrawn" else "proposal.status_changed")
    )
    if subject_type == "opportunity":
        # diff.py has no opportunity-specific lifecycle awareness yet; map the closest
        # docs/32 §3.1 analogue and let channels_for_event's threshold-less opportunity rules
        # decide postability. `withdrawn`/`new` on an opportunity record reads as cancelled/
        # announced, not a proposal event -- never fabricate a `proposal.*` type for it.
        canonical_type = {
            "new": "opportunity.rfp_opened",
            "withdrawn": "funding.cancelled",
            "status_change": "opportunity.awarded",
        }[diff_type]

    rec = record or {}
    return SocialEvent(
        event_id=event_id or _default_event_id(row),
        event_type=canonical_type,
        event_date=event_date,
        subject_type=subject_type,
        subject_id=subject_id,
        source_id=source_id,
        source_name=source_name,
        source_url=source_url,
        retrieved_at=retrieved_at,
        reuse_class=reuse_class,
        page_url=page_url,
        lag_days=lag_days,
        proposal_name=rec.get("name_canonical"),
        technology=rec.get("technology"),
        capacity_mw=_as_float(rec.get("capacity_mw")),
        county=rec.get("county"),
        state=rec.get("state"),
        iso_rto=rec.get("iso"),
        queue_id=rec.get("queue_id"),
        status_from=row.get("before") if row.get("field") == "lifecycle_state" else None,
        status_to=row.get("after") if row.get("field") == "lifecycle_state" else None,
        developer_org=rec.get("sponsor_name"),
        issuer_org=rec.get("sponsor_name") if subject_type == "opportunity" else None,
        deadline_date=_as_date(rec.get("proposed_cod")) if subject_type == "opportunity" else None,
    )


def _default_event_id(row: dict[str, Any]) -> str:
    raw = f"{row.get('record_id')}|{row.get('event_type')}|{row.get('field')}|{row.get('observed_at')}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _as_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _as_date(value: Any) -> dt.date | None:
    if value is None:
        return None
    if isinstance(value, dt.date):
        return value
    try:
        return dt.datetime.fromisoformat(str(value)).date()
    except ValueError:
        return None


# --------------------------------------------------------------------------------- eligibility


def _tech_is_high_volume(technology: str | None, config: EditorialConfig) -> bool:
    return technology is not None and technology.strip().lower() in config.high_volume_technologies


def meets_proposal_size_threshold(event: SocialEvent, config: EditorialConfig = DEFAULT_CONFIG) -> bool:
    """docs/32 §3.1 `proposal.new` row, reused for `status_changed`/`withdrawn` ("on a proposal
    that met the size threshold"). True only when at least one qualifying field is present and
    clears its bar -- an event with no size field at all never qualifies (never invent a value).
    A large-load record's MW is a load, judged against the load bar, not the generation one."""
    if event.kind == "load":
        load = event.load_mw if event.load_mw is not None else event.capacity_mw
        return load is not None and load >= config.proposal_new_load_mw
    if event.capacity_mw is not None:
        bar = (
            config.proposal_new_high_volume_iso_mw
            if (event.iso_rto in config.high_volume_isos and _tech_is_high_volume(event.technology, config))
            else config.proposal_new_mw
        )
        if event.capacity_mw >= bar:
            return True
    if event.load_mw is not None and event.load_mw >= config.proposal_new_load_mw:
        return True
    if event.voltage_kv is not None and event.voltage_kv >= config.proposal_new_voltage_kv:
        return True
    if event.capex_usd is not None and event.capex_usd >= config.proposal_new_capex_usd:
        return True
    return False


def _unsized_load(event: SocialEvent) -> bool:
    """A large-load record (a data centre, mostly) that states no size at all. Every one of the
    581 live load records in the 2026-09-30 store has no MW (content audit F13): the size rule could
    never admit one. Such a record clears the size gate on Bluesky and X only, and its post says no
    MW is stated; a load record that does state a size meets the ordinary `load_mw` bar instead."""
    return (
        event.kind == "load"
        and event.capacity_mw is None
        and event.load_mw is None
        and event.voltage_kv is None
        and event.capex_usd is None
    )


def meets_linkedin_size_threshold(event: SocialEvent, config: EditorialConfig = DEFAULT_CONFIG) -> bool:
    if event.capacity_mw is not None and event.capacity_mw >= config.linkedin_mw:
        return True
    if event.capex_usd is not None and event.capex_usd >= config.linkedin_capex_usd:
        return True
    if event.technology and event.technology.strip().lower() in config.linkedin_technologies:
        return True
    if event.voltage_kv is not None and event.voltage_kv >= config.linkedin_voltage_kv:
        return True
    return False


def is_forward_transition(status_from: str | None, status_to: str | None) -> bool:
    return (status_from, status_to) in PROPOSAL_CANONICAL_TRANSITIONS


def channels_for_event(event: SocialEvent, config: EditorialConfig = DEFAULT_CONFIG) -> tuple[str, ...]:
    """Which channels (of `services.social.models.CHANNELS`) this event earns a post on, per
    docs/32 §3.1. Returns `()` for anything not itemised there (default-deny), for a source whose
    class may not be posted (`SOCIAL_REUSE_CLASSES`), or for a proposal event below its size
    threshold."""
    if event.event_type not in POSTABLE_EVENT_TYPES:
        return ()
    if event.reuse_class not in SOCIAL_REUSE_CLASSES:
        return ()

    if event.event_type == "proposal.new":
        if _unsized_load(event):
            return ("bluesky", "x")
        if not meets_proposal_size_threshold(event, config):
            return ()
        channels = ["bluesky", "x"]
        if meets_linkedin_size_threshold(event, config):
            channels.append("linkedin")
        return tuple(channels)

    if event.event_type == "proposal.status_changed":
        if event.status_to not in LIFECYCLE_LADDER:
            return ()
        if event.status_from and not is_forward_transition(event.status_from, event.status_to):
            return ()
        if _unsized_load(event):
            return ("bluesky", "x")
        if not meets_proposal_size_threshold(event, config):
            return ()
        channels = ["bluesky", "x"]
        if event.status_to in LINKEDIN_STATUS_TARGETS:
            channels.append("linkedin")
        return tuple(channels)

    if event.event_type == "proposal.withdrawn":
        if not meets_proposal_size_threshold(event, config):
            return ()
        channels = ["bluesky", "x"]
        if event.capacity_mw is not None and event.capacity_mw >= config.linkedin_withdrawn_mw:
            channels.append("linkedin")
        return tuple(channels)

    if event.event_type == "opportunity.rfp_opened":
        return ("bluesky", "linkedin", "x")

    if event.event_type == "opportunity.rfp_closing":
        days_left = (event.deadline_date - event.event_date).days if event.deadline_date else None
        if days_left not in config.rfp_closing_windows_days:
            return ()
        return ("bluesky", "linkedin", "x") if days_left == 14 else ("bluesky", "x")

    if event.event_type == "opportunity.awarded":
        return ("bluesky", "linkedin", "x") if event.awardee_org else ()

    if event.event_type in ("funding.cancelled", "funding.reinstated"):
        return ("bluesky", "linkedin", "x")

    if event.event_type == "digest.weekly":
        return ("bluesky", "linkedin")

    return ()  # pragma: no cover -- unreachable, POSTABLE_EVENT_TYPES is exhaustive above


def idempotency_key(event: SocialEvent, channel: str) -> str:
    """docs/32 §4.8: `sha256(channel | subject_id | event_type | status_to | event_date[:10])`."""
    parts = "|".join(
        [
            channel,
            event.subject_id,
            event.event_type,
            event.status_to or "",
            event.event_date.isoformat()[:10],
        ]
    )
    return hashlib.sha256(parts.encode()).hexdigest()


def dedupe_key(event: SocialEvent, channel: str) -> str:
    """Cross-event suppression key (docs/32 §4.8, second bullet): same subject + channel within
    the dedupe window is one slot regardless of which field changed."""
    return f"{channel}|{event.subject_id}"


EVENT_PRIORITY: dict[str, int] = {
    "proposal.withdrawn": 3,
    "funding.cancelled": 3,
    "proposal.status_changed": 2,
    "funding.reinstated": 2,
    "proposal.new": 1,
    "opportunity.rfp_opened": 1,
    "opportunity.awarded": 1,
    "opportunity.rfp_closing": 4,  # "rfp_closing always posts" (docs/32 §4.8)
    "digest.weekly": 0,
}


def outranks(new_event_type: str, existing_event_type: str) -> bool:
    """docs/32 §4.8: "unless the new event is higher-priority (withdrawn > status_changed > new;
    rfp_closing always posts)"."""
    if new_event_type == "opportunity.rfp_closing":
        return True
    return EVENT_PRIORITY.get(new_event_type, 0) > EVENT_PRIORITY.get(existing_event_type, 0)


# --------------------------------------------------------------------------------- formatting


def fmt_mw(value: float | None, unit: str = "MW") -> str:
    if value is None:
        return ""
    text = f"{value:.1f}" if value < 10 else f"{value:,.0f}"
    return f"{text} {unit}"


def fmt_usd(value: float | None) -> str:
    """docs/32 §3.4: `$1.2bn`, `$450m`, `$8.5m` -- one decimal, trailing `.0` dropped."""
    if value is None:
        return ""
    if value >= 1_000_000_000:
        magnitude, suffix = 1_000_000_000, "bn"
    elif value >= 1_000_000:
        magnitude, suffix = 1_000_000, "m"
    else:
        magnitude, suffix = 1_000, "k"
    scaled = value / magnitude
    text = f"{scaled:.1f}".rstrip("0").rstrip(".")
    return f"${text}{suffix}"


def fmt_date(value: dt.date | None) -> str:
    if value is None:
        return ""
    return value.strftime("%-d %b %Y") if hasattr(value, "strftime") else str(value)


def fmt_state(state: str | None, *, full: bool) -> str:
    if not state:
        return ""
    return US_STATE_NAMES.get(state.upper(), state) if full else state.upper()


_NUMBER_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")
_URL_RE = re.compile(r"https?://\S+")
_MENTION_RE = re.compile(r"(?<!\w)@\w+")


def strip_trailing_zero(text: str) -> str:
    return text


# --------------------------------------------------------------------------------- links

#: X counts every link as 23 characters whatever its length (docs/32 §3.3, §4.3 gate 1).
X_LINK_WEIGHT = 23


def bare_url(url: str) -> str:
    """`url` without its query string and fragment: the record page a UTM-tagged link points at."""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def utm_url(page_url: str, *, channel: str, event_type: str, event_id: str) -> str:
    """docs/32 §3.2 item 3's tagged link (content audit F15): what a reader clicks, so clicks and
    alert sign-ups can be counted per channel and per event (§4.9, §6.1)."""
    query = urlencode(
        {
            "utm_source": channel,
            "utm_medium": "social",
            "utm_campaign": event_type,
            "utm_content": event_id,
        }
    )
    separator = "&" if "?" in page_url else "?"
    return f"{page_url}{separator}{query}"


def link_for(event: SocialEvent, channel: str) -> str:
    """The tagged link a post carries (`PostDraft.link_url`)."""
    return utm_url(event.page_url, channel=channel, event_type=event.event_type, event_id=event.event_id)


def body_link(event: SocialEvent, channel: str) -> str:
    """The link text in the body. X and LinkedIn carry the tagged link itself (X shortens every
    link to 23 characters; LinkedIn has room). Bluesky shows the bare page address and the post's
    link facet carries the tagged link (`publishers/bluesky.py`), so the visible text stays short."""
    return event.page_url if channel == "bluesky" else link_for(event, channel)


def channel_length(text: str, channel: str) -> int:
    """Length as the channel counts it: X weighs each link at 23 characters; Bluesky and LinkedIn
    count characters (graphemes are a follow-up, `services/api/admin_posts.py` decision 8)."""
    if channel == "x":
        return len(_URL_RE.sub("x" * X_LINK_WEIGHT, text))
    return len(text)


# --------------------------------------------------------------------------------- words

#: Sources whose own name does not say what they are, phrased for a sentence (content audit F3: an
#: EIA-860M row was posted as "New in TEPC queue"; EIA-860M is a generator inventory, not a queue).
SOURCE_PHRASES: dict[str, tuple[str, str]] = {
    # (long form for LinkedIn, short form for Bluesky and X, where the credit line that follows
    # spells the register's full name: "EIA-860M Preliminary Monthly Electric Generator Inventory")
    "us.eia.860m": ("EIA's monthly generator inventory (EIA-860M)", "EIA-860M"),
}

#: `data/sources.yaml` categories whose rows are interconnection requests: the only sources a post
#: may describe as a queue.
QUEUE_CATEGORIES: frozenset[str] = frozenset({"generation_queue", "load_queue"})


def is_queue_source(event: SocialEvent) -> bool:
    if event.source_category is not None:
        return event.source_category in QUEUE_CATEGORIES
    # An event built without the source row (`from_diff_row`, the CLI) carries no category; a
    # queue position is then the only evidence the row came from a queue.
    return bool(event.iso_rto and event.queue_id)


def source_phrase(event: SocialEvent, *, short: bool = False) -> str:
    """What the source is, in words a reader can place, never a queue it is not."""
    if event.source_id in SOURCE_PHRASES:
        long_form, short_form = SOURCE_PHRASES[event.source_id]
        return short_form if short else long_form
    if is_queue_source(event):
        if short:
            return f"the {event.iso_rto} queue" if event.iso_rto else "an interconnection queue"
        return f"the {event.iso_rto} interconnection queue" if event.iso_rto else "an interconnection queue"
    return event.source_name


def _in_sentence(label: str) -> str:
    """A sentence-case label as it reads mid-sentence: "Gas, combined cycle" -> "gas (combined
    cycle)"; an acronym-led label ("LNG export", "EV charging") keeps its capitals."""
    if ", " in label:
        head, tail = label.split(", ", 1)
        label = f"{head} ({tail})"
    if len(label) > 1 and label[1].isupper():
        return label
    return label[:1].lower() + label[1:]


def tech_words(event: SocialEvent) -> str | None:
    """The technology as words (`services/labels.py`, the site's own table; content audit F14:
    `150 MW bess_li_ion`)."""
    if event.kind == "load" and event.technology in (None, "load"):
        return "large load"
    label = technology_label(event.technology)
    return _in_sentence(label) if label else None


def state_words(token: str | None) -> str | None:
    """A lifecycle token as words ("under_construction" -> "under construction"). `None` stays
    `None`, so a template that interpolates a missing state still trips the bare-None gate."""
    label = lifecycle_label(token)
    return _in_sentence(label) if label else None


_COUNTY_SUFFIXES = (" County", " Parish", " Borough", " Census Area", " Municipality", " city", " City")


def county_words(event: SocialEvent) -> str | None:
    """The county as a place name: "Loudoun" -> "Loudoun County", and a name that already carries
    its suffix is left alone (content audit F14: "Loudoun County County" on 39 records). Outside
    the US the stored name is printed as it stands."""
    if not event.county:
        return None
    county = event.county.strip()
    if event.country != "US" or county.endswith(_COUNTY_SUFFIXES):
        return county
    return f"{county} County"


def _size_tech(event: SocialEvent) -> str:
    """`{capacity_mw} MW {technology}`, the technology alone when no size is stated, and the number
    of generators when one post covers several records of one EIA plant (content audit F9)."""
    words = " ".join(p for p in (fmt_mw(event.capacity_mw), tech_words(event) or "") if p)
    if event.member_count and event.member_count > 1:
        words += f" across {event.member_count} generators"
    if _unsized_load(event):
        words += ", no MW stated"
    return words


def _place(event: SocialEvent, *, full_state: bool) -> str:
    state = fmt_state(event.state, full=full_state)
    return ", ".join(p for p in (county_words(event), state) if p)


def _subject(event: SocialEvent, *, full_state: bool) -> str:
    """`{name}, {size} {technology}, {county}, {state}`: every proposal post states the size and
    technology, with or without a name (content audit F14: status posts dropped the MW)."""
    parts = (event.proposal_name, _size_tech(event), _place(event, full_state=full_state))
    return ", ".join(p for p in parts if p)


def _identifier_clauses(event: SocialEvent) -> list[str]:
    clauses = []
    if event.queue_id and is_queue_source(event):
        clauses.append(f"Queue {event.queue_id}.")
    if event.eia_plant_id:
        clauses.append(f"EIA plant {event.eia_plant_id}.")
    return clauses


def _org_clause(event: SocialEvent) -> str | None:
    """The organisation the record names, labelled by what the source says it is (content audit F3:
    EIA's "Entity Name" was printed as "Developer"). A queue names the interconnection customer;
    any other register is quoted only as naming the party."""
    if not event.developer_org:
        return None
    if is_queue_source(event):
        return f"Interconnection customer per the record: {event.developer_org}."
    return f"Named in the record: {event.developer_org}."


def attribution_line_for(event: SocialEvent) -> str:
    """The credit line, verbatim, as every other surface prints it (2026-10-07, content audit
    credits, legal L-10/L-12): `services.api.serialize.source_credit` -- the manifest's
    `attribution` (NESO's mandated "Supported by National Energy SO Open Data", OGL's statement, a
    CC BY citation) followed by its statement of changes -- and `Source: {source_name}` only when
    the source has no credit of its own, as the record page does. Gate 3 checks it is in the body
    unchanged, and a reviewer's edit cannot remove it."""
    return event.credit_line or f"Source: {event.source_name}"


def credit_clause(event: SocialEvent, *, full: bool = False, dated: bool = True) -> str:
    """The attribution clause: the credit line verbatim, then the retrieval date; on LinkedIn also
    the source URL and the licence's name (docs/32 §3.2 item 4; content audit F14 printed the class
    token `open` where the licence belongs)."""
    credit = attribution_line_for(event)
    stop = "" if credit.endswith(".") else "."
    date = fmt_date(event.retrieved_at.date())
    if not full:
        return f"{credit}{stop} Retrieved {date}." if dated else f"{credit}{stop}"
    url = f" {event.source_url}" if event.source_url else ""
    licence = f"; licence: {event.licence_name}" if event.licence_name else ""
    return f"{credit}{stop}{url} (retrieved {date}{licence})."


# --------------------------------------------------------------------------------- templates


def render_proposal_new(event: SocialEvent, channel: str, *, automated: bool = False) -> str:
    full = channel == "linkedin"
    subject = _subject(event, full_state=full)
    lead = (
        f"New in {source_phrase(event, short=not full)}"
        if is_queue_source(event)
        else f"Newly listed in {source_phrase(event, short=not full)}"
    )
    head = f"{lead}: {subject}"
    state = state_words(event.status_to)
    if state:
        head += f"; status: {state}"
    head += "."
    optional = _identifier_clauses(event)
    if org := _org_clause(event):
        optional.append(org)
    link = body_link(event, channel)
    if not full:
        parts = [head, *optional, link, credit_clause(event)]
        if event.lag_days:
            parts.append(f"Public feed {event.lag_days}d behind; live alerts on the page.")
        return _join_and_fit(
            parts, channel, event, tail_count=2 + (1 if event.lag_days else 0), automated=automated
        )

    body_lines = [
        head,
        *optional,
        f"Details, provenance and alert sign-up: {link}",
        credit_clause(event, full=True),
    ]
    if event.lag_days:
        body_lines.append(f"This public feed runs {event.lag_days} days behind our live tier.")
    return _join_and_fit(
        body_lines,
        channel,
        event,
        tail_count=2 + (1 if event.lag_days else 0),
        sep="\n",
        automated=automated,
    )


def render_proposal_status_changed(event: SocialEvent, channel: str, *, automated: bool = False) -> str:
    full = channel == "linkedin"
    subject = _subject(event, full_state=full)
    # Both states are interpolated unguarded on purpose: a missing one is a bare `None` the gate
    # refuses (services/social/textgate.py), never a guessed stage.
    head = f"{subject}: {state_words(event.status_from)} → {state_words(event.status_to)}."
    optional = [f"Per {source_phrase(event, short=not full)}, observed {fmt_date(event.event_date)}."]
    optional += _identifier_clauses(event)
    link = body_link(event, channel)
    if not full:
        parts = [head, *optional, link, credit_clause(event)]
        if event.lag_days:
            parts.append(f"Public feed {event.lag_days}d behind.")
        return _join_and_fit(
            parts, channel, event, tail_count=2 + (1 if event.lag_days else 0), automated=automated
        )

    body_lines = [head, *optional]
    if org := _org_clause(event):
        body_lines.append(org)
    body_lines += [f"Details, provenance and alert sign-up: {link}", credit_clause(event, full=True)]
    if event.lag_days:
        body_lines.append(f"This public feed runs {event.lag_days} days behind our live tier.")
    return _join_and_fit(
        body_lines,
        channel,
        event,
        tail_count=2 + (1 if event.lag_days else 0),
        sep="\n",
        automated=automated,
    )


def render_proposal_withdrawn(event: SocialEvent, channel: str, *, automated: bool = False) -> str:
    full = channel == "linkedin"
    subject = _subject(event, full_state=full)
    lead = (
        f"Withdrawn from {source_phrase(event, short=not full)}"
        if is_queue_source(event)
        else f"Withdrawn per {source_phrase(event, short=not full)}"
    )
    head = f"{lead}: {subject}."
    optional = []
    if event.withdrawal_reason_code:
        optional.append(f"Reason per record: {event.withdrawal_reason_code}.")
    optional += _identifier_clauses(event)
    parts = [head, *optional, body_link(event, channel), credit_clause(event, full=full)]
    if event.lag_days:
        parts.append(f"Public feed {event.lag_days}d behind.")
    return _join_and_fit(
        parts, channel, event, tail_count=2 + (1 if event.lag_days else 0), automated=automated
    )


def render_opportunity_rfp_opened(event: SocialEvent, channel: str, *, automated: bool = False) -> str:
    scope = _size_tech(event) if (event.technology or event.capacity_mw) else ""
    link = body_link(event, channel)
    if channel in ("bluesky", "x"):
        head = f"RFP open: {event.issuer_org} — {event.solicitation_title}."
        parts = [head]
        if scope:
            parts.append(f"{scope}.")
        parts.append(f"Responses due {fmt_date(event.deadline_date)}.")
        parts.append(link)
        parts.append(credit_clause(event))
        return _join_and_fit(parts, channel, event, tail_count=2, automated=automated)

    deadline = fmt_date(event.deadline_date)
    body_lines = [f"{event.issuer_org} opens {event.solicitation_title}: responses due {deadline}."]
    if scope:
        body_lines.append(f"Scope: {scope}.")
    body_lines.append(f"Details and alert sign-up: {link}")
    body_lines.append(credit_clause(event, full=True))
    return _join_and_fit(body_lines, channel, event, tail_count=2, sep="\n", automated=automated)


def render_opportunity_rfp_closing(event: SocialEvent, channel: str, *, automated: bool = False) -> str:
    days_left = (event.deadline_date - event.event_date).days if event.deadline_date else None
    parts = [
        f"Closing in {days_left} days: {event.issuer_org} — {event.solicitation_title}.",
        f"Due {fmt_date(event.deadline_date)}.",
        body_link(event, channel),
        credit_clause(event, dated=False),
    ]
    return _join_and_fit(parts, channel, event, tail_count=2, automated=automated)


def render_opportunity_awarded(event: SocialEvent, channel: str, *, automated: bool = False) -> str:
    award = f", {fmt_usd(event.award_usd)}" if event.award_usd else ""
    head = f"Awarded: {event.issuer_org} selects {event.awardee_org} for {event.solicitation_title}{award}."
    parts = [head, body_link(event, channel), credit_clause(event, full=channel == "linkedin")]
    if channel == "linkedin" and event.award_prior_status:
        parts.insert(1, f"Prior status per record: {event.award_prior_status}.")
    return _join_and_fit(parts, channel, event, tail_count=2, automated=automated)


def render_funding_change(event: SocialEvent, channel: str, *, automated: bool = False) -> str:
    verb = "Cancelled" if event.event_type == "funding.cancelled" else "Reinstated"
    full = channel == "linkedin"
    head = (
        f"{verb}: {fmt_usd(event.award_usd)} {event.funding_program} award to {event.awardee_org} "
        f"for {event.proposal_name}, {fmt_state(event.state, full=full)}, "
        f"per {event.source_name} record dated {fmt_date(event.event_date)}."
    )
    parts = [head, body_link(event, channel), credit_clause(event, full=full)]
    if channel == "linkedin" and event.award_prior_status:
        parts.insert(1, f"Prior status per record: {event.award_prior_status}.")
    return _join_and_fit(parts, channel, event, tail_count=2, automated=automated)


def render_digest_weekly(event: SocialEvent, channel: str, *, automated: bool = False) -> str:
    items = event.digest_items or ()
    n_new = sum(1 for i in items if i.get("event_type") == "proposal.new")
    sum_mw = sum(float(i.get("capacity_mw") or 0) for i in items if i.get("event_type") == "proposal.new")
    n_rfps = sum(1 for i in items if i.get("event_type") == "opportunity.rfp_opened")
    n_awards = sum(1 for i in items if i.get("event_type") == "opportunity.awarded")
    head = (
        f"This week in US energy proposals: {n_new} new proposals ({fmt_mw(sum_mw)}), "
        f"{n_rfps} RFPs open, {n_awards} awards."
    )
    parts = [head, body_link(event, channel)]
    return _join_and_fit(parts, channel, event, tail_count=1, automated=automated)


_RENDERERS = {
    "proposal.new": render_proposal_new,
    "proposal.status_changed": render_proposal_status_changed,
    "proposal.withdrawn": render_proposal_withdrawn,
    "opportunity.rfp_opened": render_opportunity_rfp_opened,
    "opportunity.rfp_closing": render_opportunity_rfp_closing,
    "opportunity.awarded": render_opportunity_awarded,
    "funding.cancelled": render_funding_change,
    "funding.reinstated": render_funding_change,
    "digest.weekly": render_digest_weekly,
}

#: v2 (2026-10-07): source-aware copy, verbatim credits, words for tokens, tagged links.
_TEMPLATE_VERSION = "v2"


def render(event: SocialEvent, channel: str, *, automated: bool = False) -> tuple[str, str, str]:
    """Render a draft body for `(event.event_type, channel)`. `automated` adds docs/13 §6.5's
    trailing line, for a post that will publish without human review.

    Returns `(body, template_id, template_version)`.
    """
    renderer = _RENDERERS.get(event.event_type)
    if renderer is None:
        raise ValueError(f"no template for event_type {event.event_type!r}")
    template_id = f"{event.event_type}.{channel}"
    # The "None" gate (services/social/textgate.py; docs/50 §3.2): a template that interpolated a
    # null field is refused here, before attribution, validation or the review queue see it.
    body = reject_bare_none(renderer(event, channel, automated=automated), template_id=template_id)
    return body, template_id, _TEMPLATE_VERSION


def delayed_tier_notice_for(event: SocialEvent, channel: str) -> str | None:
    """The lag notice (docs/32 §3.2 item 5), only for proposal-graph events, as the substring
    guaranteed present regardless of which of a channel's own template variants rendered it.
    Only `proposal.new` and `proposal.status_changed` have a distinct LinkedIn wording in
    docs/32 §3.3; `proposal.withdrawn` has no LinkedIn-specific template, so every channel gets
    the short-form phrasing for it."""
    if event.lag_days is None:
        return None
    if event.event_type not in ("proposal.new", "proposal.status_changed", "proposal.withdrawn"):
        return None
    if channel == "linkedin" and event.event_type in ("proposal.new", "proposal.status_changed"):
        return f"runs {event.lag_days} days behind our live tier"
    return f"Public feed {event.lag_days}d behind"


def _join_and_fit(
    head_then_optional_then_tail: list[str],
    channel: str,
    event: SocialEvent,
    *,
    tail_count: int,
    sep: str = " ",
    automated: bool = False,
) -> str:
    """Join clauses and, on overflow, drop optional clauses first -- docs/32 §4.2's deterministic
    fallback ("the deterministic template is used with clauses dropped in the order defined in
    the template file") -- before falling back to a hard truncation of the fact line.

    `head_then_optional_then_tail[0]` (the fact line) and its last `tail_count` items (the link,
    the attribution line, the lag notice when present, and the automated-item line when
    `automated`) are never dropped, only truncated as a last resort; everything strictly between
    them is optional and is dropped one at a time, the one nearest the tail first, until the body
    fits or nothing optional is left. Length is measured as the channel counts it
    (`channel_length`).
    """
    parts = list(head_then_optional_then_tail)
    if automated:
        parts.append(automated_line_for(event))
        tail_count += 1
    limit = CHANNEL_LIMITS[channel]
    protected = 1 + tail_count  # head + mandatory tail
    text = sep.join(parts)
    while channel_length(text, channel) > limit and len(parts) > protected:
        del parts[-tail_count - 1]  # the optional clause immediately before the mandatory tail
        text = sep.join(parts)
    if channel_length(text, channel) > limit:
        # last resort: trim the fact clause at a word boundary; never trim the link, attribution or
        # the notices.
        overflow = channel_length(text, channel) - limit
        keep = max(0, len(parts[0]) - overflow - 1)
        cut = parts[0][:keep]
        if " " in cut and keep < len(parts[0]) and parts[0][keep] != " ":
            cut = cut.rsplit(" ", 1)[0]
        parts[0] = cut.rstrip(" ,;:") + "…"
        text = sep.join(parts)
    return text


# --------------------------------------------------------------------------------- validation


def _numbers_in(text: str) -> set[str]:
    return {m.replace(",", "") for m in _NUMBER_RE.findall(text)}


_TRAILING_UNIT_RE = re.compile(r"[^\d.,]+$")


def _numeric_part(formatted: str) -> str:
    """Strip a leading `$` and trailing unit letters from a `fmt_usd`/`fmt_mw` result, and any
    comma grouping, leaving the bare digits `_numbers_in` would also produce from the body."""
    return _TRAILING_UNIT_RE.sub("", formatted).lstrip("$").replace(",", "")


def _allowed_numbers(event: SocialEvent) -> set[str]:
    """Numbers a rendered body may legitimately contain: the exact field value, and the same
    value as our own `fmt_mw`/`fmt_usd` would render it -- computed by calling those functions,
    so the allow-list can never drift from what the templates actually emit."""
    allowed: set[str] = set()
    for value in (event.load_mw, event.voltage_kv):
        if value is not None:
            allowed.add(str(int(value)) if float(value).is_integer() else str(value))
    if event.capacity_mw is not None:
        allowed.add(_numeric_part(fmt_mw(event.capacity_mw)))
    for value in (event.capex_usd, event.award_usd):
        if value is not None:
            allowed.add(_numeric_part(fmt_usd(value)))
    if event.deadline_date:
        allowed.add(str(event.deadline_date.day))
        allowed.add(str(event.deadline_date.year))
    if event.retrieved_at:
        allowed.add(str(event.retrieved_at.day))
        allowed.add(str(event.retrieved_at.year))
    allowed.add(str(event.event_date.day))
    allowed.add(str(event.event_date.year))
    if event.lag_days is not None:
        allowed.add(str(event.lag_days))
    if event.deadline_date and event.event_date:
        allowed.add(str((event.deadline_date - event.event_date).days))
    # identifiers and named-entity text (queue/docket/solicitation ids, titles, org names,
    # county) are quoted verbatim, digits and all -- a "2027 All-Source RFP" or "Acme Solar 1"
    # is not "a number in the text" in the §4.3 gate 5 sense (a claimed size, date or amount).
    for text_field in (
        event.queue_id,
        event.docket_id,
        event.solicitation_id,
        event.solicitation_title,
        event.proposal_name,
        event.developer_org,
        event.issuer_org,
        event.awardee_org,
        event.funding_program,
        event.county,
        event.eia_plant_id,
    ):
        if text_field:
            allowed.update(re.findall(r"\d+", text_field))
    if event.member_count:
        allowed.add(str(event.member_count))
    return allowed


def _named_text(event: SocialEvent, attribution_line: str) -> tuple[str, ...]:
    """Text a body quotes verbatim rather than claims: the credit line, the source's and the
    licence's names, the phrase naming the source and the automated-item line. Digits inside them
    ("EIA-860M", "Grants.gov Search2 API", "TED API v3", a CC BY citation year) are part of a name,
    not a size, date or amount (docs/32 §4.3 gate 5; content audit F1: every EIA-860M draft failed
    on `860`, and the event was lost). Longest first, so a name inside the credit is removed with it."""
    fragments = {
        attribution_line,
        attribution_line_for(event),
        event.source_name,
        event.licence_name or "",
        source_phrase(event),
        source_phrase(event, short=True),
        automated_line_for(event),
    }
    return tuple(sorted((f for f in fragments if f), key=len, reverse=True))


def _org_names_in(text: str, event: SocialEvent) -> bool:
    """True if every capitalised organisation-shaped token sequence appears in the allowed org
    fields, or there is nothing org-shaped to check. This is a heuristic guard (docs/32 §4.3
    gate 6), not a full NER pass; it exists to catch a reviewer's edit inserting an unrecorded
    party name, not to parse free text."""
    allowed = " ".join(o for o in (event.developer_org, event.issuer_org, event.awardee_org) if o)
    for org in (event.developer_org, event.issuer_org, event.awardee_org):
        if org and org not in text and org not in allowed:  # pragma: no cover -- defensive
            return False
    return True


def validate_draft(
    body: str,
    event: SocialEvent,
    channel: str,
    *,
    attribution_line: str,
    disclosure_text: str,
    delayed_tier_notice: str | None,
    is_duplicate: bool = False,
) -> ValidationResult:
    """docs/32 §4.3 hard gates. All must pass before a draft reaches the review queue.

    `digest.weekly` is exempt from gates 3 (single attribution line) and 5 (every number traces
    to an event field): it is a roll-up of many sources and many underlying events (docs/32 §3.3
    digest template has no single `Source:` clause, and its counts are aggregates over
    `digest_items`, not a field on the event itself). Every other gate still applies.
    """
    failures: list[str] = []
    is_digest = event.event_type == "digest.weekly"

    length = channel_length(body, channel)
    if length > CHANNEL_LIMITS[channel]:
        failures.append(f"length {length} exceeds {channel} limit {CHANNEL_LIMITS[channel]}")

    # The bare record address, once: the tagged link (`link_for`) carries it as its prefix, and
    # Bluesky shows it untagged (`body_link`).
    if body.count(event.page_url) != 1:
        failures.append("page_url must appear exactly once")

    if not is_digest and attribution_line not in body:
        failures.append("attribution line missing or altered")

    requires_lag = event.event_type in ("proposal.new", "proposal.status_changed", "proposal.withdrawn")
    if requires_lag and delayed_tier_notice and delayed_tier_notice not in body:
        failures.append("delayed-tier notice missing on a proposal event")
    if not requires_lag and delayed_tier_notice and delayed_tier_notice in body:
        failures.append("delayed-tier notice present on a live (non-proposal) event")

    if not is_digest:
        # Identifiers inside URLs (UTM parameters, slugs) and digits inside quoted names (the
        # credit, the register's name) are not "a number in the text" (gate 5).
        text = _URL_RE.sub(" ", body)
        for fragment in _named_text(event, attribution_line):
            text = text.replace(fragment, " ")
        stray_numbers = _numbers_in(text) - _allowed_numbers(event)
        if stray_numbers:
            failures.append(f"numbers not traceable to event fields: {sorted(stray_numbers)}")

    if not _org_names_in(body, event):
        failures.append("organisation name not present in developer_org|issuer_org|awardee_org")

    lowered = body.lower()
    hit_words = [w for w in BANNED_WORDS if w in lowered]
    if hit_words:
        failures.append(f"banned words present: {hit_words}")

    if channel in ("x", "bluesky") and _MENTION_RE.search(body):
        failures.append("unsolicited @mention present")

    # docs/32 §3.2 item 4 explicitly puts the source URL inline "on LinkedIn and email" in
    # addition to page_url; gate 7's "no URLs other than page_url" is read as barring any
    # *third*, unsanctioned URL (e.g. one introduced by a reviewer's edit), not that second,
    # deliberate one.
    allowed_urls = {event.page_url, link_for(event, channel)} | (
        {event.source_url} if channel == "linkedin" else set()
    )
    urls = set(_URL_RE.findall(body))
    if urls - allowed_urls:
        failures.append("URL other than page_url (or, on LinkedIn, source_url) present")

    if event.reuse_class not in SOCIAL_REUSE_CLASSES:
        failures.append(f"source reuse_class {event.reuse_class!r} is not publishable on social channels")

    if is_duplicate:
        failures.append("duplicate")

    if not disclosure_text:
        failures.append("disclosure text missing")

    # Belt and braces for a body edited after `render` (a reviewer's edit in the queue): the
    # same gate `render` applies, as a validation failure rather than an exception.
    if contains_bare_none(body):
        failures.append("body contains a bare 'None' token")

    return ValidationResult(passed=not failures, failures=tuple(failures))


# --------------------------------------------------------------------------------- drafting


def event_to_json_safe(event: SocialEvent) -> dict[str, Any]:
    """`fields_snapshot` must round-trip through `json.dump` (queue.py's store), so dates and
    datetimes are serialised to ISO 8601 strings rather than left as objects."""
    out: dict[str, Any] = {}
    for field in dataclasses.fields(event):
        value = getattr(event, field.name)
        if isinstance(value, (dt.datetime, dt.date)):
            out[field.name] = value.isoformat()
        elif isinstance(value, tuple) and field.name == "digest_items":
            out[field.name] = list(value) if value else None
        else:
            out[field.name] = value
    return out


def build_draft(
    event: SocialEvent,
    channel: str,
    config: EditorialConfig = DEFAULT_CONFIG,
    *,
    automated: bool = False,
) -> PostDraft:
    """Render, attribute, disclose and validate one `(event, channel)` draft. Does not check the
    review queue's stored history -- `services.social.queue.ReviewQueue.add_draft` re-runs
    `validate_draft` with `is_duplicate` set once it knows the store's state. `automated` is for
    a post that will publish without review: its body carries docs/13 §6.5's trailing line."""
    body, template_id, template_version = render(event, channel, automated=automated)
    attribution = attribution_line_for(event)
    disclosure = disclosure_text_for(channel)
    lag_notice = delayed_tier_notice_for(event, channel)
    validation = validate_draft(
        body,
        event,
        channel,
        attribution_line=attribution,
        disclosure_text=disclosure,
        delayed_tier_notice=lag_notice,
    )
    if automated and automated_line_for(event) not in body:  # pragma: no cover -- _join_and_fit adds it
        validation = ValidationResult(
            passed=False, failures=(*validation.failures, "automated-item line missing")
        )
    return PostDraft(
        channel=channel,
        event_id=event.event_id,
        event_type=event.event_type,
        subject_type=event.subject_type,
        subject_id=event.subject_id,
        template_id=template_id,
        template_version=template_version,
        body=body,
        link_url=link_for(event, channel),
        attribution_line=attribution,
        disclosure_text=disclosure,
        delayed_tier_notice=lag_notice,
        fields_snapshot=event_to_json_safe(event),
        idempotency_key=idempotency_key(event, channel),
        dedupe_key=dedupe_key(event, channel),
        cost_estimate_usd=cost_estimate_usd(channel),
        validation=validation,
    )


def event_from_snapshot(data: dict[str, Any]) -> SocialEvent:
    """Inverse of `event_to_json_safe`: the `SocialEvent` a stored draft was rendered from, so an
    edited body can be validated against the same fields (`services/api/admin_posts.py`,
    `queue.ReviewQueue.edit`). Keys a later version added and an older snapshot lacks take their
    defaults; keys this version does not know are ignored."""
    known = {f.name for f in dataclasses.fields(SocialEvent)}
    fields = {k: v for k, v in data.items() if k in known}
    for key in ("event_date", "deadline_date"):
        if fields.get(key):
            fields[key] = dt.date.fromisoformat(fields[key])
    if fields.get("retrieved_at"):
        fields["retrieved_at"] = dt.datetime.fromisoformat(fields["retrieved_at"])
    if fields.get("digest_items") is not None:
        fields["digest_items"] = tuple(fields["digest_items"])
    return SocialEvent(**fields)


def draft_events(events: list[SocialEvent], config: EditorialConfig = DEFAULT_CONFIG) -> list[PostDraft]:
    """The `event -> draft` pipeline stage (docs/32 §4.1): for every event, for every channel it
    earns per `channels_for_event`, render and validate a draft."""
    drafts = []
    for event in events:
        for channel in channels_for_event(event, config):
            try:
                drafts.append(build_draft(event, channel, config))
            except BareNoneError as exc:
                # One event with a null field its template did not guard must not stop the
                # batch; it is logged with the template id (never the text, which may carry a
                # name) and produces no draft at all.
                logger.warning(
                    "draft_rejected_bare_none template_id=%s event_id=%s", exc.template_id, event.event_id
                )
    return drafts
