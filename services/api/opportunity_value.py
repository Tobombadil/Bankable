"""An opportunity's `value` on the API: the block every list row and detail carries, the
`sort=value` ordering and the `min_value`/`max_value` bounds (lane rfp-value, 2026-10-22).

`value` is null when the notice states no amount. Otherwise:
  * `amount`, `currency`: the notice's own figure (the same numbers as `budget_amount` and
    `budget_currency`, which stay for existing clients);
  * `kind`, `kind_label`: what the figure is (`pipeline.connectors.opportunity.VALUE_KINDS`:
    estimated value, total awarded value, estimated total programme funding, award ceiling, ...);
    null and "Stated value" for a record whose source says nothing more;
  * `usd_equivalent`, `usd_is_approximate`, `fx_rate`, `fx_date`, `fx_source`: the amount in US
    dollars at the ECB reference rate of `fx_date` (`pipeline/fx.py`; exactly `amount * fx_rate`
    to the cent); `usd_is_approximate` is true for every converted amount, and false for an amount
    stated in USD and for one with no equivalent (an unquoted currency, a placeholder);
    `fx_source` credits the ECB (its terms require the credit and a statement that the cross rate
    is ours) and is null when nothing was converted; `usd_equivalent` is null when the ECB quotes
    no rate for the currency or the amount is a placeholder;
  * `also_stated`: the other figures the same notice states (a Grants.gov ceiling beside its total
    funding, a gross figure beside a net one), each with its own kind, source field and, for a
    placeholder, `amount_note`; `expected_awards`: the expected number of awards, where stated;
  * `provenance`: the source, the record's URL there, when the figure was read (a Grants.gov figure
    comes from the opportunity detail, read separately from the search, so its own time), the
    licence and its credit, and the source field.

Sort and bounds read the stored `identifiers.value.usd_equivalent` (no migration: a JSON path,
`usd_sort_expression`). Rows without one sort last in both directions (`paginate` orders a
nullable key `NULLS LAST`) and match no bound. The stored block always describes the amount the
record serves: it is computed from the pinned amount where an operator pinned one, on a load, an
edit and the daily restatement (`services/ingest/opportunity_value.py`), and rounded as
`budget_amount` is. The served `identifiers` leaves the stored block out (`served_identifiers`):
`value` is its served form.
"""

from __future__ import annotations

import functools
from collections.abc import Iterable, Mapping
from typing import Any

from sqlalchemy import ColumnElement

from pipeline import fx
from pipeline.connectors.opportunity import VALUE_KINDS, known_budget, placeholder_reason
from pipeline.connectors.registry import Registry, SourceEntry
from services.api.common import iso
from services.db.models import Opportunity, OpportunitySource

#: The list's USD-equivalent bounds (inclusive). Not saved-search or alert filters: the alert
#: matcher does not evaluate them, so a stored query naming them is refused, as any unknown key is.
VALUE_FILTERS = frozenset({"min_value", "max_value"})
#: The `sort` token ordering by USD equivalent.
VALUE_SORT = "value"
#: Label for an amount whose source does not say what it is.
UNSPECIFIED_LABEL = "Stated value"


def usd_sort_expression() -> ColumnElement[float]:
    """`identifiers -> value -> usd_equivalent` as a float, NULL where absent (both dialects)."""
    expression: ColumnElement[float] = Opportunity.identifiers[("value", "usd_equivalent")].as_float()
    return expression


def stored_usd(opportunity: Any) -> float | None:
    """The stored USD equivalent `usd_sort_expression` reads, for the page cursor."""
    block = (getattr(opportunity, "identifiers", None) or {}).get("value")
    value = block.get("usd_equivalent") if isinstance(block, dict) else None
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


@functools.lru_cache(maxsize=1)
def _fx_entry() -> SourceEntry | None:
    registry = Registry()
    return registry.get(fx.SOURCE_ID) if fx.SOURCE_ID in registry else None


def fx_credit(block: Mapping[str, Any]) -> dict[str, Any] | None:
    """`fx_source`: the ECB's provenance and credit for a converted amount, else None."""
    if block.get("fx_source") != fx.SOURCE_ID:
        return None
    entry = _fx_entry()
    return {
        "source_id": fx.SOURCE_ID,
        "source_name": entry.name if entry else "ECB euro foreign exchange reference rates",
        "source_url": fx.HIST_ZIP_URL,
        "retrieved_at": block.get("fx_retrieved_at"),
        "licence_id": entry.licence_id if entry else None,
        "licence_url": (entry.licence_url or None) if entry else None,
        "reuse_class": entry.reuse if entry else "attribution",
        "attribution_text": entry.credit_text if entry else "Source: European Central Bank",
        "rate_date": block.get("fx_date"),
    }


def served_identifiers(identifiers: Any) -> dict[str, Any]:
    """A record's `identifiers` as the API serves them: without the stored `value` block, whose
    served form is the record's `value` (and whose internal keys, such as `fx_retrieved_at`, are
    not part of the contract)."""
    if not isinstance(identifiers, Mapping):
        return {}
    return {k: v for k, v in identifiers.items() if k != "value"}


def _figure(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict) or not isinstance(raw.get("amount"), int | float):
        return None
    amount = known_budget(float(raw["amount"]))
    if amount is None:
        return None
    kind = raw.get("kind") if raw.get("kind") in VALUE_KINDS else None
    return {
        "kind": kind,
        "kind_label": VALUE_KINDS.get(kind or "", UNSPECIFIED_LABEL),
        "amount": float(amount),
        "currency": raw.get("currency"),
        "source_field": raw.get("source_field"),
        # A placeholder beside the figure that leads (Grants.gov 363912's 2,147,483,647 total).
        "amount_note": placeholder_reason(float(amount)),
    }


def _same_amount(a: Any, b: float) -> bool:
    """Whether the stored block's amount is the served one, to the cent (`budget_amount` is
    `Numeric(18, 2)`)."""
    return isinstance(a, int | float) and not isinstance(a, bool) and round(float(a), 2) == round(b, 2)


def _supplier(opportunity: Any, sources: Iterable[OpportunitySource]) -> OpportunitySource | None:
    """The served link that supplied `budget_amount`: the gated view names it (`field_sources`),
    a stored record by `field_provenance`; else the most recently retrieved served link."""
    links = list(sources)
    wanted: list[str] = []
    field_sources = getattr(opportunity, "field_sources", None)
    if callable(field_sources):
        wanted = list((field_sources().get("budget_amount") or {}).get("source_ids") or [])
    if not wanted:
        prov = (opportunity.field_provenance or {}).get("budget_amount")
        if isinstance(prov, dict) and prov.get("source_id"):
            wanted = [str(prov["source_id"])]
    for link in links:
        if link.source_id in wanted:
            return link
    return max(links, key=lambda link: link.retrieved_at, default=None)


def serialize_value(opportunity: Any, sources: Iterable[OpportunitySource]) -> dict[str, Any] | None:
    """The `value` block (module docstring) of a served opportunity (`gated_opportunity`), from
    its served `budget_amount`/`budget_currency`/`identifiers` and its served links."""
    if opportunity.budget_amount is None:
        return None
    known = known_budget(float(opportunity.budget_amount))
    if known is None:  # 0 or less: a source's "not stated" (`known_budget`), not a value
        return None
    amount = float(known)
    currency = opportunity.budget_currency
    identifiers = opportunity.identifiers or {}
    stored = identifiers.get("value")
    block: Mapping[str, Any] = stored if isinstance(stored, dict) else {}
    # The conversion stands only for the figure it was computed from (an operator's override of the
    # amount after the last load would otherwise print a stale equivalent).
    converted_here = _same_amount(block.get("amount"), amount) and (block.get("currency") or None) == (
        currency or None
    )
    usd = block.get("usd_equivalent") if converted_here else None
    kind = block.get("kind") if block.get("kind") in VALUE_KINDS else None
    link = _supplier(opportunity, sources)
    licence = link.source.licence if link is not None else None
    provenance = {
        "source_id": link.source_id if link is not None else None,
        "source_name": link.source.name if link is not None else None,
        "source_url": link.source_url if link is not None else None,
        "retrieved_at": block.get("retrieved_at") or (iso(link.retrieved_at) if link is not None else None),
        "licence_id": link.licence_id if link is not None else None,
        "reuse_class": licence.reuse_class if licence is not None else None,
        "attribution_text": (licence.attribution_text if licence is not None else None),
        "source_field": block.get("source_field"),
        "endpoint": block.get("endpoint"),
    }
    also = [f for f in (_figure(x) for x in block.get("also_stated") or []) if f is not None]
    expected = block.get("expected_awards")
    is_usd = (currency or "").upper() == "USD"
    usd_value = float(usd) if isinstance(usd, int | float) and not isinstance(usd, bool) else None
    return {
        "amount": amount,
        "currency": currency,
        "kind": kind,
        "kind_label": VALUE_KINDS.get(kind or "", UNSPECIFIED_LABEL),
        "usd_equivalent": usd_value,
        # Approximate only when something was converted: not for USD, nor when there is no figure.
        "usd_is_approximate": usd_value is not None and not is_usd,
        "fx_rate": block.get("fx_rate") if converted_here else None,
        "fx_date": block.get("fx_date") if converted_here else None,
        "fx_source": fx_credit(block) if converted_here else None,
        "fx_note": block.get("fx_note") if converted_here else None,
        # Why a stated amount is not ranked (a field maximum, a run of nines, a unit price).
        "amount_note": block.get("amount_note") if converted_here else None,
        "also_stated": also,
        "expected_awards": int(expected)
        if isinstance(expected, int) and not isinstance(expected, bool)
        else None,
        "provenance": provenance,
    }


def fx_licence_rows(items: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """One `licence_summary` row per served value converted at an ECB rate, so the page's credit
    line names the ECB beside the notices' own sources (`build_licence_summary` groups them)."""
    rows: list[dict[str, Any]] = []
    for item in items:
        credit = (item.get("value") or {}).get("fx_source")
        if not credit:
            continue
        rows.append(
            {
                "source_id": credit["source_id"],
                "name": credit["source_name"],
                "operator": "European Central Bank",
                "licence_id": credit["licence_id"] or f"{fx.SOURCE_ID}#unrecorded",
                "licence_name": f"{credit['source_name']} terms of use",
                "licence_url": credit["licence_url"],
                "reuse_class": credit["reuse_class"],
                "attribution_text": credit["attribution_text"],
                "requires_link_back": True,
                "retrieved_at": credit["retrieved_at"],
            }
        )
    return rows
