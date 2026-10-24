"""The glossary (`/glossary`): plain words for the terms on every page, one short entry per term
(owner, 2026-10-23: "anyone should get excited by it or be able to reference it and explore or even
join the industry and movement"; docs/00-PLAN.md row "The United States is the primary focus", item 3).

Each entry says what the term means, why it matters to a developer or an investor, and links to the
list that shows it. Precise enough for a professional, readable by a newcomer; US examples first.

Anchors are stable and are part of the site's contract: other pages link to `/glossary#<id>`.

- Every lifecycle state and notice status key in `web/labels.py` `LIFECYCLE_LABELS` has an entry whose
  id is the key itself (`/glossary#filed`, `/glossary#under_construction`), and its term is that
  table's label, so the chip a reader clicked and the entry it opens use the same word.
- The record pages link to exactly these seven: `interconnection-queue`, `queue-position`, `capacity`,
  `in-service-date`, `large-load`, `rfp`, `funding-call` (lane record-tabs).
- Six more: `grid-operator`, `substation`, `transmission-line`, `mw-and-mwh`, `ppa`, `cite`.

`web/test_glossary.py` pins the ids, the one-entry-per-key rule and that every link points at a list
route with a filter that route reads. The page makes no API call: it is words and links, so it is
fast, cacheable and cannot fail on a busy API. Live counts belong to the lists it links to and to
`/methodology/status-words`, which counts every status on each load.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from web.labels import lifecycle_label
from web.page import JSONLD_CONTEXT, canonical_url, jsonld_block, templates

router = APIRouter()

#: The anchors lane record-tabs links to from record pages (2026-10-23). Renaming one breaks those links.
RECORD_PAGE_ANCHORS: tuple[str, ...] = (
    "interconnection-queue",
    "queue-position",
    "capacity",
    "in-service-date",
    "large-load",
    "rfp",
    "funding-call",
)


@dataclass(frozen=True)
class Link:
    """A list the entry points at: its words and its address (a path and query on this site)."""

    text: str
    href: str


@dataclass(frozen=True)
class Term:
    """One glossary entry. `means` and `matters` are one or two short sentences each."""

    id: str
    group: str
    term: str
    means: str
    matters: str
    links: tuple[Link, ...]
    #: A status word: its formal definition and live count are on `/methodology/status-words`.
    status: bool = False


@dataclass(frozen=True)
class Group:
    id: str
    title: str


#: In reading order: the grid first (where a project starts), then size and timing, buyers and money,
#: the status words a list shows, and how to cite.
GROUPS: tuple[Group, ...] = (
    Group("grid", "Grid and queues"),
    Group("size", "Size and timing"),
    Group("money", "Buyers and money"),
    Group("projects", "Project status"),
    Group("notices", "RFP and funding status"),
    Group("use", "Using this data"),
)


def _status(
    key: str, group: str, means: str, matters: str, *links: tuple[str, str], term: str | None = None
) -> Term:
    """A status entry: its term is `LIFECYCLE_LABELS`' own word for the key, so a chip and its entry agree."""
    return Term(
        id=key,
        group=group,
        term=term or lifecycle_label(key) or key,
        means=means,
        matters=matters,
        links=tuple(Link(text, href) for text, href in links),
        status=True,
    )


def _term(id_: str, group: str, term: str, means: str, matters: str, *links: tuple[str, str]) -> Term:
    return Term(id_, group, term, means, matters, tuple(Link(text, href) for text, href in links))


#: The proposal list filtered to the states a link names. Naming states replaces the list's default
#: (the active states), but a selection made only of active states (announced to under construction)
#: still hides records no register lists any more (`web/unlisted.py`, owner 2026-10-11), as the list does
#: for every reader; a selection with built, withdrawn, cancelled, postponed or unknown in it hides
#: nothing. So "Filed projects" can count fewer than `/methodology/status-words`, which counts every record.
_PROPOSALS = "/proposals?lifecycle_state="
_NOTICES = "/opportunities?status="
#: The kinds `/search`'s RFPs and Funding tabs read (web/app.py `SEARCH_RFP_KINDS`, `SEARCH_FUNDING_KINDS`;
#: `web/test_glossary.py` pins them equal, as web/app.py imports this module). A link that names kinds and
#: no status gets the list's own default: open and announced, then "Recently closed" (lane rfp-view). A
#: named status would skip that section, and on 2026-10-23 no utility RFP was open.
RFP_KINDS = "rfp,tender,auction,procurement_notice"
FUNDING_KINDS = "foa,loan_program,program"
#: Days past its stated date before a project counts as slipped (`slipped=true`): services/api/slippage.py
#: `SLIP_GRACE_DAYS`, which `web/test_glossary.py` pins this to.
SLIP_GRACE_DAYS = 90

TERMS: tuple[Term, ...] = (
    # ---- Grid and queues
    _term(
        "interconnection-queue",
        "grid",
        "Interconnection queue",
        "The public list a grid operator or utility keeps of requests to connect a new power plant, "
        "battery or large load to its grid. Since FERC Order 2023 most US operators study them in "
        "clusters, not one at a time.",
        "It is the earliest public sign that a project is real, at a stated place and size. Most requests "
        "are withdrawn before anything is built, so a queue measures intent, not supply.",
        ("Projects filed or in study", _PROPOSALS + "filed,studied"),
        ("Grid connection points", "/interconnection-points"),
    ),
    _term(
        "queue-position",
        "grid",
        "Queue position",
        "One request in an interconnection queue: its queue ID, the megawatts asked for, where it would "
        "connect and the date it asks to start.",
        "The queue ID is how operators, utilities and counterparties name a request. Paste one into "
        "search to find its record here.",
        ("Search by queue ID", "/search"),
        ("Projects in study", _PROPOSALS + "studied"),
    ),
    _term(
        "grid-operator",
        "grid",
        "Grid operator (ISO or RTO)",
        "The organisation that runs the high-voltage grid and the wholesale power market across a region. "
        "The US has seven: CAISO, ERCOT, SPP, MISO, PJM, NYISO and ISO-NE. Elsewhere in the US a utility "
        "runs its own system.",
        "Each operator has its own queue rules, study timeline, prices and data terms, so the one a site "
        "sits in shapes the cost and the time to connect.",
        ("Grid connection points", "/interconnection-points"),
        ("Prices at hubs and zones", "/prices"),
    ),
    _term(
        "substation",
        "grid",
        "Substation",
        "A fenced site where lines meet and transformers step the voltage up or down. A plant or a large "
        "load connects at a substation, or on a line near one.",
        "Distance to a substation with room to spare is one of the biggest drivers of connection cost. "
        "Whether it has room is rarely public: ask the utility. Substations here come from OpenStreetMap, "
        "loaded state by state, Texas first.",
        ("Substations loaded so far", "/assets?asset_type=substation"),
        ("Grid connection points", "/interconnection-points"),
    ),
    _term(
        "transmission-line",
        "grid",
        "Transmission line",
        "A high-voltage line that carries power over distance. The maps here show lines of 69 kV and "
        "above; the higher the voltage, the more power a line can carry.",
        "A site beside a line of the right voltage can avoid miles of new line, often the costliest part "
        "of a connection.",
        ("Transmission lines", "/assets?asset_type=transmission_line"),
        ("Site finder, Texas first", "/site-finder"),
    ),
    # ---- Size and timing
    _term(
        "capacity",
        "size",
        "Capacity (MW)",
        "The most power a plant can deliver, or a load can draw, at one moment, in megawatts. 1,000 MW is "
        "1 GW. In a queue it is the amount requested, not what gets built.",
        "Capacity sizes the rest of a deal: the connection, the land, the offtake and the cost.",
        ("Largest projects first", "/proposals?sort=-capacity_mw"),
    ),
    _term(
        "mw-and-mwh",
        "size",
        "MW and MWh (storage)",
        "For a battery, MW is how fast it charges or discharges and MWh is how much energy it holds. A "
        "100 MW, 400 MWh battery runs at full power for four hours.",
        "Value depends on both. A MW figure alone hides how long the battery lasts, which decides what it "
        "can earn and which grid needs it meets.",
        ("Storage projects", "/proposals?kind=storage"),
    ),
    _term(
        "in-service-date",
        "size",
        "In-service date",
        "The date a project is expected to start operating, as its source states it. Close to, not the "
        "same as, the commercial operation date (COD): where a queue publishes both, the in-service or "
        "backfeed date usually comes first.",
        "Dates slip often, and a slip is a signal: study delays, network upgrades or money. Where a "
        f"stated date has passed by more than {SLIP_GRACE_DAYS} days, the record says so.",
        ("Earliest stated date first", "/proposals?sort=proposed_online_date"),
        (f"More than {SLIP_GRACE_DAYS} days past their stated date", "/proposals?slipped=true"),
    ),
    _term(
        "large-load",
        "size",
        "Large load",
        "One customer drawing a lot of power at one site: a data centre, a factory, a hydrogen plant or a "
        "mine. Each grid operator sets its own size threshold.",
        "A large load needs the same scarce grid capacity as a new plant, so where loads cluster shows a "
        "developer where power is wanted.",
        ("Data centres and other large loads", "/proposals?kind=load"),
    ),
    # ---- Buyers and money
    _term(
        "rfp",
        "money",
        "RFP (request for proposals)",
        "A buyer's formal call for offers, with requirements and a deadline: a utility asking for power, "
        "capacity, storage or a project. Tenders and auctions work the same way.",
        "An RFP is demand a developer can bid into, and its deadline is the clock for the whole response.",
        ("RFPs, tenders and auctions", "/opportunities?kind=" + RFP_KINDS),
    ),
    _term(
        "funding-call",
        "money",
        "Funding call",
        "A public offer of money with eligibility rules and a deadline: a grant, a loan or a programme, "
        "such as a US Department of Energy funding opportunity announcement (FOA).",
        "Public money can pay for early work or lower the cost of capital, but each call fits a narrow "
        "kind of project.",
        ("Grants, loans and programmes", "/opportunities?kind=" + FUNDING_KINDS),
    ),
    _term(
        "ppa",
        "money",
        "PPA (power purchase agreement)",
        "A long-term contract to buy a project's output (energy, capacity or certificates) at an agreed "
        "price, often for 10 to 20 years.",
        "A signed PPA is usually what makes a project financeable: lenders size the debt on it. In this "
        "register, Contracted means a signed grid connection agreement, not a PPA.",
        ("Utility RFPs", "/opportunities?kind=rfp"),
        ("Contracted", "#contracted"),
    ),
    # ---- Project status (proposal lifecycle states)
    _status(
        "announced",
        "projects",
        "A sponsor or agency has said the project exists; no filing, application or study is on record. "
        "For an RFP or funding call: the buyer has said it is coming, and responses are not open yet.",
        "The weakest evidence in the register. Many announced projects never file.",
        ("Announced projects", _PROPOSALS + "announced"),
        ("Announced notices", _NOTICES + "announced"),
    ),
    _status(
        "filed",
        "projects",
        "A request has been lodged with a grid operator or a regulator; study or review has not started.",
        "The first formal step: the sponsor has entered a process with deadlines and, usually, a deposit.",
        ("Filed projects", _PROPOSALS + "filed"),
    ),
    _status(
        "studied",
        "projects",
        "The request is inside the operator's or regulator's process: a cluster, impact or facilities "
        "study, or an agency review, under way.",
        "Most queue requests sit here, and many end withdrawn. The study sets the grid upgrades a project "
        "must pay for.",
        ("Studied projects", _PROPOSALS + "studied"),
    ),
    _status(
        "permitted",
        "projects",
        "The approvals the source tracks have been granted; there is no signed connection agreement or "
        "construction yet.",
        "Permits remove a major risk, but which permit depends on the source (federal, state or planning "
        "consent). Local permits are not tracked here.",
        ("Permitted projects", _PROPOSALS + "permitted"),
    ),
    _status(
        "contracted",
        "projects",
        "A grid connection (interconnection) agreement has been signed, or a binding award made; "
        "construction has not started.",
        "The strongest sign short of construction that a project will be built. It is not a signed PPA.",
        ("Contracted projects", _PROPOSALS + "contracted"),
    ),
    _status(
        "under_construction",
        "projects",
        "Physical construction is under way, or finished, and commercial operation has not started.",
        "Supply close to arriving, though not every record states a date. These megawatts are not "
        "operating yet: count operating capacity from Built.",
        ("Projects under construction", _PROPOSALS + "under_construction"),
    ),
    _status(
        "built",
        "projects",
        "The project is in commercial operation, as its source reports it.",
        "The operating fleet: its owners, its buyers, and the plants that will retire or repower next.",
        ("Built projects", _PROPOSALS + "built"),
        ("Operating plants", "/assets?asset_type=power_plant&status=operating"),
    ),
    _status(
        "withdrawn",
        "projects",
        "The sponsor withdrew the request.",
        "Most queue requests end this way. A withdrawal frees grid capacity and can mark a site worth a "
        "second look.",
        ("Withdrawn projects", _PROPOSALS + "withdrawn"),
    ),
    _status(
        "cancelled",
        "projects",
        "The process was ended by the operator, agency or buyer, not by the sponsor. For an RFP or "
        "funding call: the buyer cancelled it.",
        "Unlike a withdrawal, the decision was someone else's, often over a missed milestone or deposit.",
        ("Cancelled projects", _PROPOSALS + "cancelled"),
        ("Cancelled notices", _NOTICES + "cancelled"),
    ),
    _status(
        "postponed",
        "projects",
        "The source lists the project as canceled or indefinitely postponed and does not say which. Today "
        "that is EIA's monthly list of canceled or postponed generators.",
        "Not counted as active or as withdrawn. A postponed project can return to the planned list, and "
        "its site can be worth a second look.",
        ("Postponed projects", _PROPOSALS + "postponed"),
    ),
    _status(
        "unknown",
        "projects",
        "The source gave a status we could not place, or none at all.",
        "It does not mean early or inactive: the project could be at any stage. Check the source.",
        ("Projects with no known status", _PROPOSALS + "unknown"),
        ("Notices with no known status", _NOTICES + "unknown"),
    ),
    # ---- RFP and funding status (opportunity statuses)
    _status(
        "open",
        "notices",
        "The notice accepts responses and its deadline has not passed.",
        "The window to act. Deadlines are the buyer's: confirm them at the source.",
        ("Open notices", _NOTICES + "open"),
    ),
    _status(
        "frozen",
        "notices",
        "The buyer has paused the process without closing or cancelling it.",
        "It may resume, often on new dates: worth an alert.",
        ("Frozen notices", _NOTICES + "frozen"),
    ),
    _status(
        "reinstated",
        "notices",
        "A paused or closed process has been reopened by the buyer.",
        "A second chance to respond, usually with new dates.",
        ("Reinstated notices", _NOTICES + "reinstated"),
    ),
    _status(
        "closed",
        "notices",
        "The deadline has passed, or the buyer marked the notice complete, and no award has been seen.",
        "Not a statement that nothing was awarded: an award may exist unpublished. Closed notices show "
        "who buys, what, and how often.",
        ("Closed notices", _NOTICES + "closed"),
    ),
    _status(
        "awarded",
        "notices",
        "The buyer published a contract award or result.",
        "Awards show what was bought and, where the notice names one, who won.",
        ("Awarded notices", _NOTICES + "awarded"),
    ),
    # ---- Using this data
    _term(
        "cite",
        "use",
        "How to cite a record",
        "Name the record, Inframation, the page's address and the date you read it, then the source "
        "register the page names and its release: [Record name]. Inframation. [Page address], read "
        "[date]. Source: [register], [release].",
        "A record is only as good as its source. Citing both lets a reader check the fact, and some "
        "licences require the credit printed on the page.",
        ("Attribution and credits", "/attribution"),
        ("Reuse conditions", "/legal/reuse"),
    ),
)

TERMS_BY_ID: dict[str, Term] = {t.id: t for t in TERMS}


def glossary_href(term_id: str) -> str:
    """`/glossary#<id>` for an entry that exists; a `KeyError` for one that does not, so a page can never
    link to an anchor this page does not carry."""
    return f"/glossary#{TERMS_BY_ID[term_id].id}"


def grouped() -> list[tuple[Group, list[Term]]]:
    """The groups in order, each with its entries in order; a group with no entry is left out."""
    return [(g, [t for t in TERMS if t.group == g.id]) for g in GROUPS if any(t.group == g.id for t in TERMS)]


def glossary_jsonld(request: Request) -> str:
    """schema.org `DefinedTermSet`: each entry a `DefinedTerm` with its own anchored URL."""
    page = canonical_url(request, "/glossary")
    return jsonld_block(
        {
            "@context": JSONLD_CONTEXT,
            "@type": "DefinedTermSet",
            "@id": page,
            "name": "Inframation glossary",
            "url": page,
            "hasDefinedTerm": [
                {
                    "@type": "DefinedTerm",
                    "@id": f"{page}#{t.id}",
                    "name": t.term,
                    "description": t.means,
                    "url": f"{page}#{t.id}",
                    "inDefinedTermSet": page,
                }
                for t in TERMS
            ],
        }
    )


@router.get("/glossary", response_class=HTMLResponse)
def glossary(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "glossary.html",
        {
            "groups": grouped(),
            "jsonld": [glossary_jsonld(request)],
        },
    )
