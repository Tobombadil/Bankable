"""An opportunity's value: what its notice states, and an approximate USD equivalent (lane
rfp-value, 2026-10-22).

Where it is kept (no migration). `budget_amount`/`budget_currency` hold the notice's primary stated
figure in its own currency, as before. `identifiers["value"]` holds what the connector says the
figure is (`kind` from `pipeline.connectors.opportunity.VALUE_KINDS`, `source_field`, any other
figures the notice states, `expected_awards`, and for a figure read from a separate response its
`retrieved_at` and `endpoint`) and, written here, the conversion: the `amount`/`currency` it was
computed from, `usd_equivalent`, `fx_rate` (USD per unit), `fx_date` (the ECB fixing used),
`fx_source` (`eu.ecb.eurofxref`, or null for an amount stated in USD) and `fx_retrieved_at`.
`GET /v1/opportunities?sort=-value` orders on `identifiers.value.usd_equivalent`.

When it is written.
  * Every load (`services/ingest/loader.py::_opportunity_fields_from_row` -> `with_value`), with
    the rate table in force (`pipeline.fx.default_table`), at the fixing of the notice's `open_at`
    (its publication date for TED, the World Bank and Grants.gov; the tender period's start, else
    the release date, for Find a Tender). A record whose amount an operator pinned (`overrides`)
    is converted at the pinned amount (`with_pinned_value`), so the list never sorts on a figure
    the record does not show; the link keeps the source's own view.
  * An operator's edit (`PATCH /admin/v1/opportunities/{id}`) and an intake approval, the same way.
  * `restate_values` (CLI below, and the daily `value_tick`, `infra/scheduler/app.py`, which first
    fetches the ECB history): every stored opportunity again, its stated value re-read from the
    raw row of the link that supplied `budget_amount`, through that connector's own
    `stated_value(row)`. Incremental sources (TED, Find a Tender, the World Bank) never reload old
    rows, so this is how the rows loaded before this lane gain a kind and a USD equivalent, and how
    new fixings reach rows converted at a fallback rate. A restatement is not news: it does not move
    `last_changed` or write events (the runner's rule for a parser fix, docs/22 §8). A field an
    operator pinned (`overrides`) is left as pinned and converted as pinned. On Postgres each row
    is locked for the pass and a row a load holds is skipped (that load writes its own value).

What is news (review of lane rfp-value, 2026-10-22). `identifiers` is a served field, so the
loader's `last_changed` comparison (`services/ingest/loader.py::_served_values`) reads it through
`served_identifiers`, which leaves out of `identifiers.value` what changes while the notice does
not (`NOT_NEWS_KEYS`): when a figure or a rate was read (`retrieved_at`, `fx_retrieved_at`) and the
conversion itself (a new fixing, a refreshed table). Measured on the 2026-10-20 store before the
change: a Grants.gov reload whose figures were the same and only `retrieved_at` had moved changed
`last_changed` on 105 of 105 value-bearing notices; every TED and Find a Tender row reloaded after
an `fx refresh` did the same through `fx_retrieved_at`. A changed amount, currency, kind or other
stated figure still moves it.

Never invented: no amount is derived from another (a ceiling is not multiplied by the number of
awards, award values are not summed), a currency the ECB does not quote has no USD equivalent, and
a record with no stated amount has no `value`. A stated amount that is a placeholder (a 32-bit field
maximum, a run of nines, or under 100: `pipeline.connectors.opportunity.placeholder_reason`) is
kept and shown as stated, with `amount_note` saying why, and has no USD equivalent: on the
2026-10-20 store, Grants.gov's 2,147,483,647 and 999,999,999 led the value sort and electricity unit
prices of EUR 0.01 trailed it.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import importlib
import json
import math
import sys
from collections.abc import Callable, Mapping
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from pipeline import fx
from pipeline.connectors.http import HttpBlocked, HttpFailed
from pipeline.connectors.opportunity import StatedValue, known_budget, placeholder_reason
from services.db.models import Opportunity, OpportunitySource

VALUE_KEY = "value"
#: Keys `with_value` owns inside `identifiers["value"]`; everything else is the connector's.
CONVERSION_KEYS = (
    "amount",
    "currency",
    "usd_equivalent",
    "fx_rate",
    "fx_date",
    "fx_source",
    "fx_retrieved_at",
    "fx_note",
    "amount_note",
)
#: Keys of `identifiers["value"]` whose change is not news (module docstring "What is news").
NOT_NEWS_KEYS = frozenset(
    {"retrieved_at", "usd_equivalent", "fx_rate", "fx_date", "fx_source", "fx_retrieved_at", "fx_note"}
)
#: `budget_amount` is `Numeric(18, 2)`: the block's amount is rounded the same way, so the figure the
#: list sorts on is the one the record serves.
AMOUNT_PLACES = 2
#: The record fields a value block is computed from.
VALUE_FIELDS = ("budget_amount", "budget_currency", "open_at")


def stored_amount(amount: Any) -> float | None:
    """`amount` as `budget_amount` stores it (two decimal places), or None for no amount, a
    non-number, or a source's 0 / negative placeholder (`known_budget`)."""
    if amount is None or isinstance(amount, bool):
        return None
    try:
        value = float(amount)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value):
        return None
    known = known_budget(round(value, AMOUNT_PLACES))
    return float(known) if known is not None else None


def served_identifiers(identifiers: Any) -> Any:
    """`identifiers` as the loader compares it for `last_changed`: `identifiers["value"]` without
    `NOT_NEWS_KEYS` (module docstring "What is news")."""
    if not isinstance(identifiers, Mapping):
        return identifiers
    block = identifiers.get(VALUE_KEY)
    if not isinstance(block, Mapping):
        return identifiers
    return {**identifiers, VALUE_KEY: {k: v for k, v in block.items() if k not in NOT_NEWS_KEYS}}


def value_block(
    stated: Mapping[str, Any] | None,
    amount: float | None,
    currency: str | None,
    on: dt.date | None,
    table: fx.RateTable,
) -> dict[str, Any] | None:
    """The `identifiers["value"]` block for a record stating `amount` in `currency` (None when it
    states no amount): the connector's own keys from `stated`, then the conversion of the amount as
    `budget_amount` stores it (`stored_amount`). An amount that is a placeholder
    (`placeholder_reason`) keeps its stated figure, says why in `amount_note`, and gets no USD
    equivalent, so it neither leads nor trails the value sort."""
    value = stored_amount(amount)
    if value is None:
        return None
    block = {k: v for k, v in dict(stated or {}).items() if k not in CONVERSION_KEYS}
    code = str(currency).strip().upper() if currency else None
    block["amount"] = value
    block["currency"] = code
    placeholder = placeholder_reason(value)
    conversion = None if placeholder else table.convert(value, code, on)
    if placeholder:
        block.update({"usd_equivalent": None, "fx_rate": None, "fx_date": None, "fx_source": None})
        block["amount_note"] = placeholder
        block["fx_note"] = "not converted: the stated amount looks like a placeholder"
    elif conversion is None:
        block.update({"usd_equivalent": None, "fx_rate": None, "fx_date": None, "fx_source": None})
        block["fx_note"] = f"no ECB reference rate for {code}" if code else "the notice states no currency"
    else:
        block.update(conversion.as_json())
        if conversion.fx_source is not None:
            block["fx_retrieved_at"] = table.retrieved_at_on(conversion.fx_date)
    return block


def _as_date(value: Any) -> dt.date | None:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if isinstance(value, str) and value:
        try:
            return dt.date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def with_value(fields: Mapping[str, Any], table: fx.RateTable | None = None) -> dict[str, Any]:
    """`fields` (the loader's opportunity fields) with `identifiers["value"]` completed for its
    `budget_amount`/`budget_currency`/`open_at`, or removed when there is no amount."""
    out = dict(fields)
    identifiers = dict(out.get("identifiers") or {})
    stated = identifiers.get(VALUE_KEY) if isinstance(identifiers.get(VALUE_KEY), dict) else None
    block = value_block(
        stated,
        out.get("budget_amount"),
        out.get("budget_currency"),
        _as_date(out.get("open_at")),
        table if table is not None else fx.default_table(),
    )
    if block is None:
        identifiers.pop(VALUE_KEY, None)
    else:
        identifiers[VALUE_KEY] = block
    out["identifiers"] = identifiers
    return out


def with_pinned_value(
    fields: Mapping[str, Any], entity: Any, table: fx.RateTable | None = None
) -> Mapping[str, Any]:
    """`fields` (a load's, for an already-stored record) with `identifiers["value"]` converted for
    the amount, currency and date the record keeps: where an operator pinned any of them
    (`overrides`, which the load does not overwrite), the pinned one. Unchanged when nothing is
    pinned, or when `identifiers` itself is pinned (then the load writes no identifiers at all)."""
    pinned = set(getattr(entity, "overrides", None) or {})
    if not pinned & set(VALUE_FIELDS) or "identifiers" in pinned or "identifiers" not in fields:
        return fields
    kept = {
        name: getattr(entity, name, None) if name in pinned else fields.get(name) for name in VALUE_FIELDS
    }
    return {**fields, "identifiers": with_value({**fields, **kept}, table)["identifiers"]}


def value_identifiers(
    identifiers: Mapping[str, Any] | None,
    amount: Any,
    currency: str | None,
    open_at: Any,
    table: fx.RateTable | None = None,
) -> dict[str, Any]:
    """`identifiers` with the value block for this amount, currency and date: for a record written
    outside a load (an operator's edit, an intake approval)."""
    fields = {
        "identifiers": dict(identifiers or {}),
        "budget_amount": amount,
        "budget_currency": currency,
        "open_at": open_at,
    }
    out: dict[str, Any] = with_value(fields, table)["identifiers"]
    return out


# ------------------------------------------------------------------------------- restatement
def _module_name(source_id: str) -> str:
    return "pipeline.connectors." + source_id.replace(".", "_").replace("-", "_") + ".connector"


_PARSERS: dict[str, Callable[[dict[str, Any]], StatedValue | None] | None] = {}


def stated_value_parser(source_id: str) -> Callable[[dict[str, Any]], StatedValue | None] | None:
    """The connector's own `stated_value(row)` for `source_id`, or None when it has none."""
    if source_id not in _PARSERS:
        try:
            module = importlib.import_module(_module_name(source_id))
        except ImportError:
            _PARSERS[source_id] = None
        else:
            fn = getattr(module, "stated_value", None)
            _PARSERS[source_id] = fn if callable(fn) else None
    return _PARSERS[source_id]


def _supplying_link(opportunity: Opportunity) -> OpportunitySource | None:
    """The active link whose source `field_provenance` credits for `budget_amount`, else the most
    recently retrieved active link."""
    active = [link for link in opportunity.sources if link.active]
    prov = (opportunity.field_provenance or {}).get("budget_amount")
    source_id = prov.get("source_id") if isinstance(prov, dict) else None
    for link in active:
        if link.source_id == source_id:
            return link
    return max(active, key=lambda link: link.retrieved_at, default=None)


@dataclasses.dataclass
class RestateResult:
    examined: int = 0
    changed: int = 0
    amounts_restated: int = 0
    #: Links whose `normalised` (the source's own view) gained or changed its value block.
    links_restated: int = 0
    with_value: int = 0
    with_usd: int = 0
    #: Stated amounts withheld from the USD scale as placeholders (`placeholder_reason`).
    placeholders: int = 0
    no_rate: dict[str, int] = dataclasses.field(default_factory=dict)
    by_kind: dict[str, int] = dataclasses.field(default_factory=dict)

    def as_json(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def _json_equal(a: Any, b: Any) -> bool:
    return json.dumps(a, sort_keys=True, default=str) == json.dumps(b, sort_keys=True, default=str)


def _link_view(
    link: OpportunitySource, stated: StatedValue | None, open_at: Any, table: fx.RateTable
) -> dict[str, Any] | None:
    """The link's `normalised` (the source's own view of the record) with the source's own value:
    the amount and currency its row states and their block. An operator's pinned figure lives on
    the record only, never here, so clearing the override restores the source's value. None when
    nothing changes."""
    normalised = dict(link.normalised or {})
    own = dict(normalised.get("identifiers") or {})
    if stated is not None:
        amount: Any = stored_amount(stated.amount)
        currency = stated.currency
        connector: Mapping[str, Any] | None = stated.identifiers()
    else:
        amount, currency = normalised.get("budget_amount"), normalised.get("budget_currency")
        connector = own.get(VALUE_KEY) if isinstance(own.get(VALUE_KEY), dict) else None
    on = _as_date(normalised.get("open_at")) or _as_date(open_at)
    block = value_block(connector, amount, currency, on, table)
    if stated is not None:
        normalised["budget_amount"] = amount
        normalised["budget_currency"] = currency
    if block is None:
        own.pop(VALUE_KEY, None)
    else:
        own[VALUE_KEY] = block
    normalised["identifiers"] = own
    view: dict[str, Any] = json.loads(json.dumps(normalised, default=str))
    return None if _json_equal(view, link.normalised or {}) else view


def restate_opportunity(opportunity: Opportunity, table: fx.RateTable, result: RestateResult) -> bool:
    """One record (module docstring "When it is written"). Returns whether the record changed."""
    pinned = set(opportunity.overrides or {})
    link = _supplying_link(opportunity)
    stated: StatedValue | None = None
    if link is not None and isinstance(link.raw, dict):
        parser = stated_value_parser(link.source_id)
        stated = parser(link.raw) if parser is not None else None
    identifiers = dict(opportunity.identifiers or {})
    amount = stored_amount(opportunity.budget_amount)
    currency = opportunity.budget_currency
    connector_block: Mapping[str, Any] | None = (
        identifiers.get(VALUE_KEY) if isinstance(identifiers.get(VALUE_KEY), dict) else None
    )
    restated_amount = False
    if stated is not None and link is not None and not {"budget_amount", "budget_currency"} & pinned:
        connector_block = stated.identifiers()
        new_amount = stored_amount(stated.amount)
        if new_amount != amount or stated.currency != currency:
            amount, currency = new_amount, stated.currency
            restated_amount = True
    block = value_block(connector_block, amount, currency, opportunity.open_at, table)
    new_identifiers = dict(identifiers)
    if block is None:
        new_identifiers.pop(VALUE_KEY, None)
    else:
        new_identifiers[VALUE_KEY] = block
    changed = False
    if "identifiers" not in pinned and not _json_equal(new_identifiers, identifiers):
        opportunity.identifiers = new_identifiers
        changed = True
    if restated_amount and link is not None:
        opportunity.budget_amount = amount
        opportunity.budget_currency = currency
        provenance = dict(opportunity.field_provenance or {})
        stamp = {
            "source_id": link.source_id,
            "licence_id": link.licence_id,
            "retrieved_at": link.retrieved_at.isoformat() if link.retrieved_at else None,
        }
        for name in ("budget_amount", "budget_currency"):
            provenance[name] = stamp
        opportunity.field_provenance = provenance
        result.amounts_restated += 1
        changed = True
    if link is not None:
        view = _link_view(link, stated, opportunity.open_at, table)
        if view is not None:
            link.normalised = view
            result.links_restated += 1
    if block is not None:
        result.with_value += 1
        result.by_kind[str(block.get("kind"))] = result.by_kind.get(str(block.get("kind")), 0) + 1
        if block.get("usd_equivalent") is not None:
            result.with_usd += 1
        elif block.get("amount_note"):
            result.placeholders += 1
        else:
            code = str(block.get("currency"))
            result.no_rate[code] = result.no_rate.get(code, 0) + 1
    return changed


def restate_statement() -> Any:
    """Every stored opportunity not merged into another, in id order. On Postgres each row is
    locked for the pass (`FOR UPDATE OF opportunity`), and a row a concurrent load already holds is
    skipped rather than waited for (`SKIP LOCKED`): that load writes the row's value itself, and the
    pass never overwrites `identifiers` a load is changing. SQLite has no row locks and one writer
    at a time; the clause is not rendered there."""
    return (
        select(Opportunity)
        .where(Opportunity.merged_into_id.is_(None))
        .options(selectinload(Opportunity.sources))
        .order_by(Opportunity.id)
        .with_for_update(of=Opportunity, skip_locked=True)
    )


def restate_values(session: Session, table: fx.RateTable | None = None) -> RestateResult:
    """`restate_opportunity` for every row `restate_statement` selects. Flushes; the caller
    ends the transaction."""
    table = table if table is not None else fx.default_table()
    result = RestateResult()
    for opportunity in session.scalars(restate_statement()):
        result.examined += 1
        if restate_opportunity(opportunity, table, result):
            result.changed += 1
    session.flush()
    return result


def refresh_rates(refresh: Callable[[], fx.RefreshResult] | None = None) -> dict[str, Any]:
    """`pipeline.fx.refresh` (one conditional GET of the ECB history), reported rather than
    raised: when it fails, the restatement still runs on the table in force (the last fetched
    history, else the vendored seed), and each `fx_date` says how old its rate is."""
    try:
        return (refresh or fx.refresh)().as_json()
    except (fx.FxError, HttpFailed, HttpBlocked, OSError) as exc:
        return {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}


def value_pass(
    session: Session, *, refresh: bool = True, _refresh: Callable[[], fx.RefreshResult] | None = None
) -> dict[str, Any]:
    """The daily `value_tick` (`infra/scheduler/jobs.py::value_tick_job`) and the CLI below: the
    ECB history refreshed (`refresh_rates`), then `restate_values` at the table just read. Flushes;
    the caller ends the transaction."""
    report = refresh_rates(_refresh) if refresh else None
    table = fx.load_table()
    result = restate_values(session, table)
    return {
        "fx_refresh": report,
        "fx_table": {
            "origin": table.origin,
            "retrieved_at": table.retrieved_at,
            "last_fixing": table.last_date.isoformat() if table.last_date else None,
        },
        **result.as_json(),
    }


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - thin CLI over tested functions
    parser = argparse.ArgumentParser(description="Restate every opportunity's value and USD equivalent.")
    parser.add_argument("--db", help="database URL (default: the service's DATABASE_URL)")
    parser.add_argument("--refresh-rates", action="store_true", help="fetch the ECB history first")
    parser.add_argument("--dry-run", action="store_true", help="report without committing")
    args = parser.parse_args(argv)
    from services.db.session import get_engine, get_sessionmaker

    engine = get_engine(args.db)
    with get_sessionmaker(engine)() as session:
        report = value_pass(session, refresh=args.refresh_rates)
        if args.dry_run:
            session.rollback()
        else:
            session.commit()
    sys.stdout.write(json.dumps({"dry_run": args.dry_run, **report}, indent=1) + "\n")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
