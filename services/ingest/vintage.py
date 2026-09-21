"""What release of a source we are holding, as distinct from when we fetched it.

The bug this module exists to fix
---------------------------------
`services/api/build_info.py::data_as_of` returns the newest `retrieved_at` across loaded source
records, and the site footer printed it as "sources last fetched ...". That sentence is true, and
readers took it to mean the data was current to that date. On the 2026-09-13 load it was not: the
EIA-860M workbook loaded was ``july_generator2026.xlsx``, EIA's **July 2026** report, fetched on
13 September. Two months of age were invisible because the only date on the page was ours.

A fetch date is a fact about us. A release is a fact about the source. Global Energy Monitor's
recommended citation names the release ("Global Solar Power Tracker, Global Energy Monitor,
February 2026 release") for exactly this reason. Both belong on the page, and neither may stand in
for the other — so nothing here ever falls back to `retrieved_at`. A source that states no release
resolves to :data:`NOT_STATED`, which renders as "no release stated by this source", and that is
the honest answer rather than a borrowed date.

Where the value comes from
--------------------------
Only from something the source itself published, never from a hand-maintained field that would
drift the moment a new release appeared:

``artefact_filename``
    The fetched URL names the release. EIA-860M publishes ``xls/july_generator2026.xlsx``; the
    month and year in that path are EIA's own label for the report.
``shapefile_member``
    The US Energy Atlas ships shapefiles whose member names carry a vintage token
    (``..._US_202001.shp``). `pipeline/context/eia_atlas.py::source_vintage` already extracts it
    and the asset loader already stores it on `asset.attributes.source_vintage`; this module
    normalises that token into the same shape as everything else.
``not_stated``
    The artefact carries no release label. True of every ISO queue register loaded today: CAISO,
    ERCOT and NYISO publish a file at a stable URL that is simply replaced, and NESO, TED,
    grants.gov and the World Bank are APIs queried live. For these, `retrieved_at` really is the
    best statement available about the data's age — but it is labelled as a fetch, not as a
    release.

A source whose vintage has never been determined is a fourth state, distinct from "the source
states none": `Source.vintage_basis` is NULL until a load resolves it (:data:`UNDETERMINED`).

Normalised form
---------------
`Vintage.value` is ``YYYY-MM`` or ``YYYY`` — sortable as text, so "which loaded release is the
oldest" is a `min()` and needs no date parsing. `Vintage.label` is the reader's form ("July 2026").
Anything a source states that does not reduce to a year or a year-month is kept verbatim in
`value` and labelled verbatim, because inventing precision is the failure this module is about.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

#: `Source.vintage_basis` vocabulary. Mirrored as a CHECK constraint in
#: `services/db/models.py::SOURCE_VINTAGE_BASES`; NULL there means "never determined".
ARTEFACT_FILENAME = "artefact_filename"
SHAPEFILE_MEMBER = "shapefile_member"
NOT_STATED = "not_stated"
VINTAGE_BASES = (ARTEFACT_FILENAME, SHAPEFILE_MEMBER, NOT_STATED)

#: Returned by `vintage_of_source` for a source no load has examined yet. Never stored: the
#: stored form of this state is `vintage_basis IS NULL`.
UNDETERMINED = "undetermined"

_MONTHS = (
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
)
_MONTH_NUMBER = {name: i + 1 for i, name in enumerate(_MONTHS)}
_MONTH_LABEL = {i + 1: name.capitalize() for i, name in enumerate(_MONTHS)}

#: EIA-860M: ``.../xls/july_generator2026.xlsx``. The month name and year are EIA's own label for
#: the report, and the same path pattern has held since the series moved to `xls/`.
_EIA_860M_FILENAME = re.compile(
    r"/(?P<month>" + "|".join(_MONTHS) + r")_generator(?P<year>\d{4})\.xlsx\b",
    re.IGNORECASE,
)

#: EIA Energy Atlas shapefile member tokens: ``202001`` (year+month) or ``2017``/``2017_v2``.
_ATLAS_TOKEN = re.compile(r"^(?P<year>\d{4})(?P<month>0[1-9]|1[0-2])?(?:_v\d+)?$")


@dataclass(frozen=True)
class Vintage:
    """One source's release, as the source states it.

    `value` is ``None`` exactly when `basis` is :data:`NOT_STATED` or :data:`UNDETERMINED`; there
    is no third way to be empty, and no path in this module produces a value from a fetch date.
    """

    value: str | None
    basis: str
    label: str | None = None

    @property
    def stated(self) -> bool:
        return self.value is not None


#: The answer for a source whose artefacts carry no release label.
NOT_STATED_VINTAGE = Vintage(value=None, basis=NOT_STATED, label=None)
#: The answer for a source no load has examined.
UNDETERMINED_VINTAGE = Vintage(value=None, basis=UNDETERMINED, label=None)


def label_for(value: str | None) -> str | None:
    """Reader's form of a normalised value: ``2026-07`` -> ``July 2026``, ``2017`` -> ``2017``.

    A value that is neither shape is returned unchanged — it came verbatim from a source and is
    not ours to reformat."""
    if value is None:
        return None
    m = re.fullmatch(r"(\d{4})-(\d{2})", value)
    if m:
        month = int(m.group(2))
        if month in _MONTH_LABEL:
            return f"{_MONTH_LABEL[month]} {m.group(1)}"
    return value


def from_artefact_url(url: str | None) -> Vintage:
    """The release named by a fetched artefact's own URL, or :data:`NOT_STATED_VINTAGE`.

    Only patterns a source actually publishes are recognised. A URL this does not know is *not
    stated*, never a guess: `retrieved_at` is deliberately unreachable from here."""
    if not url:
        return NOT_STATED_VINTAGE
    m = _EIA_860M_FILENAME.search(url)
    if m:
        value = f"{m.group('year')}-{_MONTH_NUMBER[m.group('month').lower()]:02d}"
        return Vintage(value=value, basis=ARTEFACT_FILENAME, label=label_for(value))
    return NOT_STATED_VINTAGE


def from_atlas_token(token: Any) -> Vintage:
    """A US Energy Atlas shapefile vintage token (`pipeline/context/eia_atlas.py::source_vintage`).

    ``202001`` -> ``2020-01``; ``2017_v2`` -> ``2017`` (the ``_v2`` is EIA's revision of the same
    vintage, not a later one, so it is dropped from the sortable value and the label says the year
    only). A token in neither shape is kept verbatim rather than discarded or reshaped."""
    if token is None:
        return NOT_STATED_VINTAGE
    text = str(token).strip()
    if not text:
        return NOT_STATED_VINTAGE
    m = _ATLAS_TOKEN.fullmatch(text)
    if m is None:
        return Vintage(value=text, basis=SHAPEFILE_MEMBER, label=text)
    value = f"{m.group('year')}-{m.group('month')}" if m.group("month") else m.group("year")
    return Vintage(value=value, basis=SHAPEFILE_MEMBER, label=label_for(value))


def from_source_urls(urls: object) -> Vintage:
    """The release stated by a run's record URLs, or :data:`NOT_STATED_VINTAGE`.

    A connector run writes one artefact URL onto every record it produced, so the first URL that
    names a release answers for the run. Values are scanned rather than only the first taken,
    because a frame may carry blank URLs (an opportunity connector that fills `source_url` per
    record) ahead of the ones that carry the artefact path."""
    if urls is None:
        return NOT_STATED_VINTAGE
    seen = 0
    for url in urls:  # type: ignore[attr-defined]
        if url is None:
            continue
        text = str(url).strip()
        if not text:
            continue
        seen += 1
        vintage = from_artefact_url(text)
        if vintage.stated:
            return vintage
        # Every record in a run shares one artefact URL; a few distinct ones are worth trying, a
        # whole 10,000-row frame of identical strings is not.
        if seen >= 20:
            break
    return NOT_STATED_VINTAGE


def from_attributes(attributes: object) -> Vintage:
    """The release recorded on an asset row's `attributes.source_vintage` (the Atlas path)."""
    if not isinstance(attributes, Mapping):
        return NOT_STATED_VINTAGE
    return from_atlas_token(attributes.get("source_vintage"))
