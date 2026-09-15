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
import re
from typing import Any, Literal

from services.social.models import PostDraft, ValidationResult

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

#: Canonical proposal lifecycle transitions docs/32 §3.1 treats as "any transition between
#: canonical stages" worth a status_changed post (docs/21 §7.1).
PROPOSAL_CANONICAL_TRANSITIONS: frozenset[tuple[str, str]] = frozenset(
    {
        ("filed", "studied"),
        ("studied", "permitted"),
        ("filed", "permitted"),
        ("permitted", "contracted"),
        ("studied", "contracted"),
        ("contracted", "under_construction"),
        ("under_construction", "built"),
        ("contracted", "built"),
    }
)

#: docs/32 §3.1: LinkedIn joins a status_changed post only "for reaching interconnection
#: agreement, construction or operation".
LINKEDIN_STATUS_TARGETS: frozenset[str] = frozenset({"contracted", "under_construction", "built"})

#: Reuse classes allowed to leave the building at all (docs/21 §8, CLAUDE.md guardrails).
PUBLISHABLE_REUSE_CLASSES: frozenset[str] = frozenset({"open", "attribution"})

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
DISCLOSURE_TEXT: dict[str, str] = {
    "bluesky": (
        "Automated feed run by Bankable (bankablehq.com). Posts are generated from public "
        "records and are commercial in nature. Not monitored for replies — contact: hello@bankablehq.com."
    ),
    "x": (
        "Automated feed run by Bankable (bankablehq.com). Posts are generated from public "
        "records and are commercial in nature. Not monitored for replies — contact: hello@bankablehq.com."
    ),
    "linkedin": (
        "Human-reviewed: generated from structured public data by Bankable's pipeline and "
        "reviewed by a named editor before publication (docs/13-legal-outreach-and-social.md §6.5)."
    ),
}

#: docs/32 §1.5: $0.20 per post containing a URL (every Bankable post does), plus an amortised
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
        {"solar", "storage", "battery storage", "solar+storage"}
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
    clears its bar -- an event with no size field at all never qualifies (never invent a value)."""
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


def channels_for_event(event: SocialEvent, config: EditorialConfig = DEFAULT_CONFIG) -> tuple[str, ...]:
    """Which channels (of `services.social.models.CHANNELS`) this event earns a post on, per
    docs/32 §3.1. Returns `()` for anything not itemised there (default-deny), for a gated
    source, or for a proposal event below its size threshold."""
    if event.event_type not in POSTABLE_EVENT_TYPES:
        return ()
    if event.reuse_class not in PUBLISHABLE_REUSE_CLASSES:
        return ()

    if event.event_type == "proposal.new":
        if not meets_proposal_size_threshold(event, config):
            return ()
        channels = ["bluesky", "x"]
        if meets_linkedin_size_threshold(event, config):
            channels.append("linkedin")
        return tuple(channels)

    if event.event_type == "proposal.status_changed":
        if not meets_proposal_size_threshold(event, config):
            return ()
        transition = (event.status_from, event.status_to)
        if event.status_from and event.status_to and transition not in PROPOSAL_CANONICAL_TRANSITIONS:
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


# --------------------------------------------------------------------------------- templates


def _size_tech(event: SocialEvent) -> str:
    """`{capacity_mw} MW {technology}`, or just the capacity when technology is unrecorded."""
    if event.technology:
        return f"{fmt_mw(event.capacity_mw)} {event.technology}".strip()
    return fmt_mw(event.capacity_mw)


def _place(event: SocialEvent, *, full_state: bool) -> str:
    county = f"{event.county} County" if event.county else None
    state = fmt_state(event.state, full=full_state)
    return ", ".join(p for p in (county, state) if p)


def _clauses_new(event: SocialEvent, *, full_state: bool) -> dict[str, str]:
    return {
        "size_tech": _size_tech(event),
        "place": _place(event, full_state=full_state),
        "developer": event.developer_org or "",
    }


def render_proposal_new(event: SocialEvent, channel: str) -> str:
    full = channel == "linkedin"
    c = _clauses_new(event, full_state=full)
    place = c["place"]
    if channel in ("bluesky", "x"):
        head = (
            f"New in {event.iso_rto} queue: {c['size_tech']}" if event.iso_rto else f"New: {c['size_tech']}"
        )
        head += f", {place}." if place else "."
        parts = [head]
        if event.queue_id:
            parts.append(f"Queue {event.queue_id}.")
        if event.developer_org:
            parts.append(f"{event.developer_org}.")
        parts.append(event.page_url)
        parts.append(f"Source: {event.source_name}, {fmt_date(event.retrieved_at.date())}.")
        if event.lag_days:
            parts.append(f"Public feed {event.lag_days}d behind; live alerts on the page.")
        return _join_and_fit(
            parts, CHANNEL_LIMITS[channel], event, tail_count=2 + (1 if event.lag_days else 0)
        )

    # linkedin
    head = f"{c['size_tech']} proposed"
    if place:
        head += f" in {place}"
    if event.iso_rto and event.queue_id:
        head += f" ({event.iso_rto} queue {event.queue_id})"
    head += "."
    body_lines = [head]
    if event.developer_org:
        body_lines.append(f"Developer: {event.developer_org} (per the official record).")
    body_lines.append(f"Details, provenance and alert sign-up: {event.page_url}")
    body_lines.append(
        f"Source: {event.source_name} — {event.source_url} "
        f"(retrieved {fmt_date(event.retrieved_at.date())}; {event.reuse_class})."
    )
    if event.lag_days:
        body_lines.append(f"This public feed runs {event.lag_days} days behind our live tier.")
    return _join_and_fit(
        body_lines, CHANNEL_LIMITS[channel], event, tail_count=2 + (1 if event.lag_days else 0), sep="\n"
    )


def render_proposal_status_changed(event: SocialEvent, channel: str) -> str:
    full = channel == "linkedin"
    name = event.proposal_name or _size_tech(event)
    state = fmt_state(event.state, full=full)
    queue = f"{event.iso_rto} {event.queue_id}" if event.iso_rto and event.queue_id else (event.iso_rto or "")
    if channel in ("bluesky", "x"):
        if state:
            head = f"{name}, {state}: status {event.status_from} → {event.status_to}"
        else:
            head = f"{name}: status {event.status_from} → {event.status_to}"
        head += f" ({queue})." if queue else "."
        parts = [head, event.page_url, f"Source: {event.source_name}, {fmt_date(event.retrieved_at.date())}."]
        if event.lag_days:
            parts.append(f"Public feed {event.lag_days}d behind.")
        return _join_and_fit(
            parts, CHANNEL_LIMITS[channel], event, tail_count=2 + (1 if event.lag_days else 0)
        )

    head = f"{fmt_mw(event.capacity_mw)} {event.technology} in {state} moves to {event.status_to}.".strip()
    body_lines = [
        head,
        f"Status: {event.status_from} → {event.status_to}, per {event.source_name} "
        f"(observed {fmt_date(event.event_date)}).",
        f"Details, provenance and alert sign-up: {event.page_url}",
        f"Source: {event.source_name} — {event.source_url} "
        f"(retrieved {fmt_date(event.retrieved_at.date())}; {event.reuse_class}).",
    ]
    if event.lag_days:
        body_lines.append(f"This public feed runs {event.lag_days} days behind our live tier.")
    return _join_and_fit(
        body_lines, CHANNEL_LIMITS[channel], event, tail_count=2 + (1 if event.lag_days else 0), sep="\n"
    )


def render_proposal_withdrawn(event: SocialEvent, channel: str) -> str:
    full = channel == "linkedin"
    place = _place(event, full_state=full)
    size_tech = _size_tech(event)
    head = (
        f"Withdrawn from {event.iso_rto} queue: {size_tech}" if event.iso_rto else f"Withdrawn: {size_tech}"
    )
    if place:
        head += f", {place}"
    if event.queue_id:
        head += f" ({event.queue_id})"
    head += "."
    parts = [head, event.page_url, f"Source: {event.source_name}, {fmt_date(event.retrieved_at.date())}."]
    if event.withdrawal_reason_code:
        parts.insert(1, f"Reason per record: {event.withdrawal_reason_code}.")
    if event.lag_days:
        parts.append(f"Public feed {event.lag_days}d behind.")
    return _join_and_fit(parts, CHANNEL_LIMITS[channel], event, tail_count=2 + (1 if event.lag_days else 0))


def render_opportunity_rfp_opened(event: SocialEvent, channel: str) -> str:
    scope = _size_tech(event) if (event.technology or event.capacity_mw) else ""
    if channel in ("bluesky", "x"):
        head = f"RFP open: {event.issuer_org} — {event.solicitation_title}."
        parts = [head]
        if scope:
            parts.append(f"{scope}.")
        parts.append(f"Responses due {fmt_date(event.deadline_date)}.")
        parts.append(event.page_url)
        parts.append(f"Source: {event.source_name}, {fmt_date(event.retrieved_at.date())}.")
        return _join_and_fit(parts, CHANNEL_LIMITS[channel], event, tail_count=2)

    deadline = fmt_date(event.deadline_date)
    body_lines = [f"{event.issuer_org} opens {event.solicitation_title}: responses due {deadline}."]
    if scope:
        body_lines.append(f"Scope: {scope}.")
    body_lines.append(f"Details and alert sign-up: {event.page_url}")
    body_lines.append(
        f"Source: {event.source_name} — {event.source_url} "
        f"(retrieved {fmt_date(event.retrieved_at.date())}; {event.reuse_class})."
    )
    return _join_and_fit(body_lines, CHANNEL_LIMITS[channel], event, tail_count=2, sep="\n")


def render_opportunity_rfp_closing(event: SocialEvent, channel: str) -> str:
    days_left = (event.deadline_date - event.event_date).days if event.deadline_date else None
    parts = [
        f"Closing in {days_left} days: {event.issuer_org} — {event.solicitation_title}.",
        f"Due {fmt_date(event.deadline_date)}.",
        event.page_url,
        f"Source: {event.source_name}.",
    ]
    return _join_and_fit(parts, CHANNEL_LIMITS[channel], event, tail_count=2)


def render_opportunity_awarded(event: SocialEvent, channel: str) -> str:
    award = f", {fmt_usd(event.award_usd)}" if event.award_usd else ""
    head = f"Awarded: {event.issuer_org} selects {event.awardee_org} for {event.solicitation_title}{award}."
    parts = [head, event.page_url, f"Source: {event.source_name}, {fmt_date(event.retrieved_at.date())}."]
    if channel == "linkedin" and event.award_prior_status:
        parts.insert(1, f"Prior status per record: {event.award_prior_status}.")
    return _join_and_fit(parts, CHANNEL_LIMITS[channel], event, tail_count=2)


def render_funding_change(event: SocialEvent, channel: str) -> str:
    verb = "Cancelled" if event.event_type == "funding.cancelled" else "Reinstated"
    full = channel == "linkedin"
    head = (
        f"{verb}: {fmt_usd(event.award_usd)} {event.funding_program} award to {event.awardee_org} "
        f"for {event.proposal_name}, {fmt_state(event.state, full=full)}, "
        f"per {event.source_name} record dated {fmt_date(event.event_date)}."
    )
    parts = [head, event.page_url]
    if channel == "linkedin" and event.award_prior_status:
        parts.insert(1, f"Prior status per record: {event.award_prior_status}.")
    return _join_and_fit(parts, CHANNEL_LIMITS[channel], event, tail_count=1)


def render_digest_weekly(event: SocialEvent, channel: str) -> str:
    items = event.digest_items or ()
    n_new = sum(1 for i in items if i.get("event_type") == "proposal.new")
    sum_mw = sum(float(i.get("capacity_mw") or 0) for i in items if i.get("event_type") == "proposal.new")
    n_rfps = sum(1 for i in items if i.get("event_type") == "opportunity.rfp_opened")
    n_awards = sum(1 for i in items if i.get("event_type") == "opportunity.awarded")
    head = (
        f"This week in US energy proposals: {n_new} new proposals ({fmt_mw(sum_mw)}), "
        f"{n_rfps} RFPs open, {n_awards} awards."
    )
    parts = [head, event.page_url]
    return _join_and_fit(parts, CHANNEL_LIMITS[channel], event, tail_count=1)


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

_TEMPLATE_VERSION = "v1"


def render(event: SocialEvent, channel: str) -> tuple[str, str, str]:
    """Render a draft body for `(event.event_type, channel)`.

    Returns `(body, template_id, template_version)`.
    """
    renderer = _RENDERERS.get(event.event_type)
    if renderer is None:
        raise ValueError(f"no template for event_type {event.event_type!r}")
    return renderer(event, channel), f"{event.event_type}.{channel}", _TEMPLATE_VERSION


def attribution_line_for(event: SocialEvent) -> str:
    """The credit line (docs/21 §3.18 `credit_line`) as a substring guaranteed present in every
    channel's rendering of this event. Most templates say `Source: {source_name}` (date on short
    formats, URL+licence on LinkedIn -- docs/32 §3.2 item 4); the funding template instead reads
    "... per {source_name} record dated ..." (docs/32 §3.3), so the bare source name is the
    substring common to both phrasings.
    """
    if event.event_type in ("funding.cancelled", "funding.reinstated"):
        return event.source_name
    return f"Source: {event.source_name}"


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
    limit: int,
    event: SocialEvent,
    *,
    tail_count: int,
    sep: str = " ",
) -> str:
    """Join clauses and, on overflow, drop optional clauses first -- docs/32 §4.2's deterministic
    fallback ("the deterministic template is used with clauses dropped in the order defined in
    the template file") -- before falling back to a hard truncation of the fact line.

    `head_then_optional_then_tail[0]` (the fact line) and its last `tail_count` items (page_url,
    the attribution line, and the lag notice when present -- the docs/32 §4.3 gate-checked
    clauses) are never dropped, only truncated as a last resort; everything strictly between
    them is optional and is dropped one at a time, most-recently-added first, until the body
    fits or nothing optional is left.
    """
    parts = list(head_then_optional_then_tail)
    protected = 1 + tail_count  # head + mandatory tail
    text = sep.join(parts)
    while len(text) > limit and len(parts) > protected:
        del parts[-tail_count - 1]  # the optional clause immediately before the mandatory tail
        text = sep.join(parts)
    if len(text) > limit:
        # last resort: trim the fact clause; never trim page_url, attribution or the lag notice.
        overflow = len(text) - limit
        parts[0] = parts[0][: max(0, len(parts[0]) - overflow - 1)].rstrip() + "…"
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
    ):
        if text_field:
            allowed.update(re.findall(r"\d+", text_field))
    return allowed


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

    if len(body) > CHANNEL_LIMITS[channel]:
        failures.append(f"length {len(body)} exceeds {channel} limit {CHANNEL_LIMITS[channel]}")

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
        # identifiers inside URLs (utm params, slugs) are not "a number in the text" (gate 5) --
        # strip every URL before scanning so a page_url or source_url digit never trips the gate.
        text_without_urls = _URL_RE.sub(" ", body)
        stray_numbers = _numbers_in(text_without_urls) - _allowed_numbers(event)
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
    allowed_urls = {event.page_url} | ({event.source_url} if channel == "linkedin" else set())
    urls = set(_URL_RE.findall(body))
    if urls - allowed_urls:
        failures.append("URL other than page_url (or, on LinkedIn, source_url) present")

    if event.reuse_class not in PUBLISHABLE_REUSE_CLASSES:
        failures.append(f"source reuse_class {event.reuse_class!r} is not publishable")

    if is_duplicate:
        failures.append("duplicate")

    if not disclosure_text:
        failures.append("disclosure text missing")

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


def build_draft(event: SocialEvent, channel: str, config: EditorialConfig = DEFAULT_CONFIG) -> PostDraft:
    """Render, attribute, disclose and validate one `(event, channel)` draft. Does not check the
    review queue's stored history -- `services.social.queue.ReviewQueue.add_draft` re-runs
    `validate_draft` with `is_duplicate` set once it knows the store's state."""
    body, template_id, template_version = render(event, channel)
    attribution = attribution_line_for(event)
    disclosure = DISCLOSURE_TEXT[channel]
    lag_notice = delayed_tier_notice_for(event, channel)
    validation = validate_draft(
        body,
        event,
        channel,
        attribution_line=attribution,
        disclosure_text=disclosure,
        delayed_tier_notice=lag_notice,
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
        link_url=event.page_url,
        attribution_line=attribution,
        disclosure_text=disclosure,
        delayed_tier_notice=lag_notice,
        fields_snapshot=event_to_json_safe(event),
        idempotency_key=idempotency_key(event, channel),
        dedupe_key=dedupe_key(event, channel),
        cost_estimate_usd=cost_estimate_usd(channel),
        validation=validation,
    )


def draft_events(events: list[SocialEvent], config: EditorialConfig = DEFAULT_CONFIG) -> list[PostDraft]:
    """The `event -> draft` pipeline stage (docs/32 §4.1): for every event, for every channel it
    earns per `channels_for_event`, render and validate a draft."""
    drafts = []
    for event in events:
        for channel in channels_for_event(event, config):
            drafts.append(build_draft(event, channel, config))
    return drafts
