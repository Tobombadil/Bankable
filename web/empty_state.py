"""Empty and invalid list states (docs/31 §6, §5.9; designer audit 2026-09-30 D-11).

docs/31 §6: an empty state "names the filter facet that removed the last result and offers Clear
all". Before this module the lists said "No proposals match these filters." whatever was set, so
`/proposals?jurisdiction=US-ZZ` gave no hint that the jurisdiction was the cause or what a code
looks like, and `capacity_mw[gte]=500&capacity_mw[lte]=10` asked the API a question no row can
answer and printed the same sentence (§5.9 wants inline validation text, not a silent no-op).

`empty_result_facets` is only called when a list came back empty. For each filter in play it asks
the API how many rows the same query returns *without* that one filter (`limit=1&include=count`,
one cheap call per facet, at most `MAX_FACETS`), so the page can say which facet emptied the result
and offer a link that removes just it.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import Any
from urllib.parse import urlencode

from starlette.datastructures import QueryParams

from web.alerts import FILTER_LABELS
from web.api_client import ApiClient, ApiError
from web.labels import opportunity_kind_label, proposal_kind_label, technology_label

#: Bounds the extra count calls an empty page makes.
MAX_FACETS = 6

#: Keys that are not filters a reader set: paging, sort, the map's own view and placement default,
#: and tracking parameters, which the passthrough lists drop anyway.
NOT_A_FACET = frozenset({"cursor", "sort", "include_withdrawn", "center", "zoom", "layers", "region"})

_VALUE_WORDS: dict[str, Callable[[Any], str | None]] = {
    "technology": technology_label,
    "technologies": technology_label,
}


def _value_words(entity: str, key: str, value: str) -> str:
    if key == "kind":
        words = (proposal_kind_label if entity == "proposal" else opportunity_kind_label)(value)
        return words or value
    convert = _VALUE_WORDS.get(key)
    return (convert(value) if convert else None) or value


def facet_label(key: str) -> str:
    return FILTER_LABELS.get(key, key.replace("_", " ").capitalize())


def remove_href(path: str, qp: QueryParams, key: str) -> str:
    """`path` with every parameter of the view except `key` (and the page cursor)."""
    kept = [(k, v) for k, v in qp.multi_items() if k not in (key, "cursor") and v]
    return path + ("?" + urlencode(kept) if kept else "")


def empty_result_facets(
    api: ApiClient,
    *,
    entity: str,
    endpoint: str,
    path: str,
    qp: QueryParams,
    names: Iterable[str],
    base_params: Mapping[str, str | None],
) -> list[dict[str, Any]]:
    """One entry per filter in play, in URL order: its label and value in words, how many rows the
    query returns without it (`None` when the API could not say), and the link that removes it.

    `base_params` is the API query the page sent (filters, lifecycle or status, no cursor); each
    count drops one filter from it."""
    allowed = set(names)
    active = [(k, v) for k, v in qp.multi_items() if k in allowed and k not in NOT_A_FACET and v]
    out: list[dict[str, Any]] = []
    for key, value in active[:MAX_FACETS]:
        params = {k: v for k, v in base_params.items() if k not in (key, "cursor", "sort") and v is not None}
        try:
            envelope = api.get(endpoint, params={**params, "limit": 1, "include": "count"})
            without: int | None = int(envelope["meta"].get("total") or 0)
        except (ApiError, KeyError, TypeError, ValueError):
            without = None
        out.append(
            {
                "key": key,
                "label": facet_label(key),
                "value": _value_words(entity, key, value),
                "without": without,
                "href": remove_href(path, qp, key),
            }
        )
    return out


def _number(value: str | None) -> float | None:
    if value is None or value.strip() == "":
        return None
    try:
        return float(value)
    except ValueError:
        return None


def range_error(qp: Mapping[str, str], low_key: str, high_key: str, *, noun: str) -> str | None:
    """Inline validation text for a min/max pair (docs/31 §5.9 "invalid combination shown as
    inline validation text, not a silent no-op"), or `None` when the pair is usable."""
    low_raw, high_raw = qp.get(low_key), qp.get(high_key)
    low, high = _number(low_raw), _number(high_raw)
    if low_raw and low is None:
        return f"Minimum {noun} must be a number."
    if high_raw and high is None:
        return f"Maximum {noun} must be a number."
    if (low is not None and low < 0) or (high is not None and high < 0):
        return f"{noun.capitalize()} cannot be negative."
    if low is not None and high is not None and low > high:
        return (
            f"Minimum {noun} ({low_raw}) is above the maximum ({high_raw}), so nothing can match. "
            "Swap them or clear one."
        )
    return None
