"""GLEIF Level 2 relationship records ("who owns whom") -> the direct-parent parquet
(`global.gleif.lei`, `data/sources.yaml` section K; docs/21 §3.23, docs/22 §17).

GLEIF publishes the whole LEI corpus as CC0 "golden copy" files (`https://www.gleif.org/en/
lei-data/gleif-golden-copy/download-the-golden-copy`; the licence quoted in docs/13 §2.13). This
module reads two of them, chosen by `latest_publish()` from the publish index at
`https://goldencopy.gleif.org/api/v2/golden-copies/publishes?format=json`:

- the **relationship** golden copy (`rr`, RR_2.1), which is the filtered download this lane wants:
  488,439 records in a 23 MB zip on 2026-09-20, the entire Level 2 corpus. Every relationship is
  there, so no per-entity API paging is needed and the result is reproducible from one file;
- the **entity** golden copy (`lei2`, LEI_3.1), which is not small (3,435,980 records, a 481 MB
  zip). It is read as a single streaming pass and only the LEIs that appear in a consolidation
  relationship are kept in memory (183,855 of 3.4 M on that publish), because a relationship record
  carries LEIs and no names, and a name is what the loader has to match on. The alternative --
  `api.gleif.org/api/v1/lei-records?filter[lei]=...`, 200 LEIs per request -- is ~920 requests for
  the same answer against a host that rate-limits, so the one big file wins on politeness as well
  as on determinism. `read_entities` never materialises the file: it decompresses in 1 MB chunks
  through `csv.reader`, so peak memory is the kept subset, not the 5 GB of CSV.

Only the two consolidation types matter for a parent link: `IS_DIRECTLY_CONSOLIDATED_BY` (the
accounting parent that consolidates the child's accounts) and `IS_ULTIMATELY_CONSOLIDATED_BY` (the
top of that chain). `IS_FUND-MANAGED_BY`, `IS_SUBFUND_OF`, `IS_FEEDER_TO` and
`IS_INTERNATIONAL_BRANCH_OF` are fund-administration and branch facts, not ownership, and are
dropped (they are 228,649 of the 488,439 records). In GLEIF's model the **start node is the
child** and the end node is the parent, which is why `IS_..._CONSOLIDATED_BY` reads in that
direction.

Output, one row per (child, relationship type) -- `OUTPUT_COLUMNS`:

`child_lei`, `child_legal_name`, `child_country`, `child_region`, `child_city`,
`child_jurisdiction`, `child_entity_status`, `parent_lei`, `parent_legal_name`, `parent_country`,
`relationship_type`, `relationship_status`, `registration_status`, `period_start`, `period_end`,
`accounting_period_end`, `last_update_date`, `source_id`, `source_url`, `retrieved_at`, `licence`.

The as-of dates are GLEIF's own and all three are kept, because they answer different questions:
`period_start`/`period_end` are the RELATIONSHIP_PERIOD (when the ownership itself began and, if
ended, stopped), `accounting_period_end` is the end of the consolidated accounting period the
filer reported the relationship for, and `last_update_date` is when the record was last maintained.
`services/ingest/organizations.py` writes `period_start or accounting_period_end or
last_update_date` onto `organization.parent_as_of`; the loader, not this module, picks.

A relationship whose `Relationship.RelationshipStatus` is INACTIVE, or whose
`Registration.RegistrationStatus` is not PUBLISHED, is kept in the parquet and filtered by the
loader -- an expired parent link is a fact about the past and belongs in the file, just not on a
company page.

Measured, 2026-09-20 publish (`--rr-zip`/`--lei2-zip` against the files fetched that day):
488,439 relationship records, 259,790 of them consolidation; 140,434 distinct children, 54,191
distinct parents, 183,855 LEIs to name, of which 183,850 are in the entity file (5 relationship
records point at an LEI the entity file does not carry, and are dropped with a count).
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import io
import json
import logging
import pathlib
import sys
import time
import zipfile
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from pipeline.connectors.base import ParseError
from pipeline.connectors.http import PoliteSession

ROOT = pathlib.Path(__file__).resolve().parents[2]
CONTEXT_DIR = ROOT / "data" / "normalized" / "context"
SOURCE_ID = "global.gleif.lei"
LICENCE = "cc0"
DEFAULT_OUT = CONTEXT_DIR / f"{SOURCE_ID}.parents.parquet"
PUBLISHES_URL = "https://goldencopy.gleif.org/api/v2/golden-copies/publishes?format=json"
DOWNLOAD_PAGE = "https://www.gleif.org/en/lei-data/gleif-golden-copy/download-the-golden-copy"

#: The two Level 2 relationship types that are ownership. Everything else in the RR file is fund
#: administration or a branch registration (module docstring).
CONSOLIDATION_TYPES: tuple[str, ...] = ("IS_DIRECTLY_CONSOLIDATED_BY", "IS_ULTIMATELY_CONSOLIDATED_BY")

#: Column index -> field, in the LEI_3.1 golden-copy CSV. Indexes, not names, because the file has
#: 338 columns and reading it as a dict per row costs several minutes; `read_entities` asserts the
#: header still spells each one as expected before it trusts a single index.
ENTITY_COLUMNS: dict[str, tuple[int, str]] = {
    "lei": (0, "LEI"),
    "legal_name": (1, "Entity.LegalName"),
    "city": (41, "Entity.LegalAddress.City"),
    "region": (42, "Entity.LegalAddress.Region"),
    "country": (43, "Entity.LegalAddress.Country"),
    "jurisdiction": (190, "Entity.LegalJurisdiction"),
    "entity_status": (199, "Entity.EntityStatus"),
}

OUTPUT_COLUMNS: list[str] = [
    "child_lei",
    "child_legal_name",
    "child_country",
    "child_region",
    "child_city",
    "child_jurisdiction",
    "child_entity_status",
    "parent_lei",
    "parent_legal_name",
    "parent_country",
    "relationship_type",
    "relationship_status",
    "registration_status",
    "period_start",
    "period_end",
    "accounting_period_end",
    "last_update_date",
    "source_id",
    "source_url",
    "retrieved_at",
    "licence",
]

log = logging.getLogger(__name__)


def utcnow_iso() -> str:
    return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


@dataclass
class GoldenCopyFile:
    """One published golden-copy file: what GLEIF says about it, plus what we measured."""

    kind: str  # "rr" | "lei2"
    url: str
    published_records: int
    published_bytes: int
    publish_date: str
    path: pathlib.Path | None = None
    downloaded_bytes: int | None = None
    sha256: str | None = None
    retrieved_at: str | None = None

    def as_report(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "url": self.url,
            "publish_date": self.publish_date,
            "published_records": self.published_records,
            "published_bytes": self.published_bytes,
            "downloaded_bytes": self.downloaded_bytes,
            "sha256": self.sha256,
            "retrieved_at": self.retrieved_at,
        }


@dataclass
class BuildResult:
    rows: int = 0
    relationships_seen: int = 0
    consolidation_relationships: int = 0
    distinct_children: int = 0
    distinct_parents: int = 0
    leis_wanted: int = 0
    leis_named: int = 0
    dropped_unnamed_lei: int = 0
    by_type: dict[str, int] = field(default_factory=dict)
    files: list[dict[str, Any]] = field(default_factory=list)

    def as_report(self) -> dict[str, Any]:
        return {
            "rows": self.rows,
            "relationships_seen": self.relationships_seen,
            "consolidation_relationships": self.consolidation_relationships,
            "distinct_children": self.distinct_children,
            "distinct_parents": self.distinct_parents,
            "leis_wanted": self.leis_wanted,
            "leis_named": self.leis_named,
            "dropped_unnamed_lei": self.dropped_unnamed_lei,
            "by_type": dict(sorted(self.by_type.items())),
            "files": list(self.files),
        }


# ---------------------------------------------------------------------------------- fetch
def latest_publish(payload: dict[str, Any]) -> dict[str, GoldenCopyFile]:
    """`{"rr": GoldenCopyFile, "lei2": GoldenCopyFile}` from the publish index's newest entry.

    The index is a list ordered newest first; each entry carries a `full_file` per format. We take
    `csv`, which is a third the size of the JSON and a quarter of the XML for the same records.
    """
    data = payload.get("data")
    if not isinstance(data, list) or not data:
        raise ParseError("GLEIF publish index carries no `data` entries")
    entry = data[0]
    files: dict[str, GoldenCopyFile] = {}
    for kind in ("rr", "lei2"):
        block = entry.get(kind) or {}
        full = (block.get("full_file") or {}).get("csv") or {}
        url = full.get("url")
        if not url:
            raise ParseError(f"GLEIF publish index has no {kind} full-file CSV url")
        files[kind] = GoldenCopyFile(
            kind=kind,
            url=str(url),
            published_records=int(full.get("record_count") or 0),
            published_bytes=int(full.get("size") or 0),
            publish_date=str(block.get("publish_date") or entry.get("publish_date") or ""),
        )
    return files


def fetch_publish_index(session: PoliteSession, url: str = PUBLISHES_URL) -> dict[str, Any]:
    response = session.get(f"{url}&page=1&per_page=1" if "?" in url else url)
    response.raise_for_status()
    payload: dict[str, Any] = response.json()
    return payload


def download(session: PoliteSession, spec: GoldenCopyFile, into: pathlib.Path) -> GoldenCopyFile:
    """Stream one golden-copy zip to disk, recording the bytes and sha256 we actually received."""
    into.mkdir(parents=True, exist_ok=True)
    target = into / spec.url.rsplit("/", 1)[-1]
    digest = hashlib.sha256()
    size = 0
    retrieved_at = utcnow_iso()
    with session.get(spec.url, stream=True) as response:
        response.raise_for_status()
        with target.open("wb") as fh:
            for chunk in response.iter_content(chunk_size=1 << 20):
                if not chunk:
                    continue
                digest.update(chunk)
                size += len(chunk)
                fh.write(chunk)
    spec.path, spec.downloaded_bytes, spec.sha256, spec.retrieved_at = (
        target,
        size,
        digest.hexdigest(),
        retrieved_at,
    )
    return spec


# ---------------------------------------------------------------------------------- parse
def _only_member(path: pathlib.Path) -> tuple[zipfile.ZipFile, str]:
    zf = zipfile.ZipFile(path)
    names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
    if len(names) != 1:
        raise ParseError(f"{path.name}: expected exactly one CSV member, found {names[:5]}")
    return zf, names[0]


def _date(value: str) -> dt.date | None:
    """GLEIF stamps are `2024-10-28T00:00:00.000Z`; we keep the date, which is the resolution the
    relationship is actually stated at."""
    text = (value or "").strip()
    if not text:
        return None
    try:
        return dt.date.fromisoformat(text[:10])
    except ValueError:
        return None


def _period(row: dict[str, str], period_type: str) -> tuple[dt.date | None, dt.date | None]:
    """`(start, end)` of the first period of `period_type`. The RR CDF allows five period slots in
    any order, so they are scanned rather than indexed."""
    for i in range(1, 6):
        if row.get(f"Relationship.Period.{i}.periodType") == period_type:
            return (
                _date(row.get(f"Relationship.Period.{i}.startDate", "")),
                _date(row.get(f"Relationship.Period.{i}.endDate", "")),
            )
    return None, None


def read_relationships(path: pathlib.Path, result: BuildResult | None = None) -> list[dict[str, Any]]:
    """The consolidation relationships of an RR golden-copy zip, as plain dicts."""
    result = result if result is not None else BuildResult()
    zf, member = _only_member(path)
    kept: list[dict[str, Any]] = []
    with zf.open(member) as raw:
        for row in csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8-sig")):
            result.relationships_seen += 1
            rel_type = row.get("Relationship.RelationshipType", "")
            if rel_type not in CONSOLIDATION_TYPES:
                continue
            if row.get("Relationship.StartNode.NodeIDType") != "LEI":
                continue
            if row.get("Relationship.EndNode.NodeIDType") != "LEI":
                continue
            start, end = _period(row, "RELATIONSHIP_PERIOD")
            _, accounting_end = _period(row, "ACCOUNTING_PERIOD")
            kept.append(
                {
                    "child_lei": row["Relationship.StartNode.NodeID"],
                    "parent_lei": row["Relationship.EndNode.NodeID"],
                    "relationship_type": rel_type,
                    "relationship_status": row.get("Relationship.RelationshipStatus") or "",
                    "registration_status": row.get("Registration.RegistrationStatus") or "",
                    "period_start": start,
                    "period_end": end,
                    "accounting_period_end": accounting_end,
                    "last_update_date": _date(row.get("Registration.LastUpdateDate", "")),
                }
            )
            result.by_type[rel_type] = result.by_type.get(rel_type, 0) + 1
    zf.close()
    result.consolidation_relationships = len(kept)
    return kept


def read_entities(path: pathlib.Path, wanted: set[str]) -> dict[str, dict[str, str]]:
    """`{lei: {legal_name, country, region, city, jurisdiction, entity_status}}` for `wanted` only.

    One streaming pass over the entity golden copy. The header is checked against
    `ENTITY_COLUMNS` before any index is trusted, so a GLEIF CDF change fails loudly here rather
    than silently writing the wrong column into a company page.
    """
    zf, member = _only_member(path)
    out: dict[str, dict[str, str]] = {}
    with zf.open(member) as raw:
        reader = csv.reader(io.TextIOWrapper(raw, encoding="utf-8-sig"))
        header = next(reader, None)
        if header is None:
            raise ParseError(f"{path.name}: empty entity file")
        for field_name, (index, spelling) in ENTITY_COLUMNS.items():
            if index >= len(header) or header[index] != spelling:
                raise ParseError(
                    f"{path.name}: column {index} is {header[index : index + 1]}, expected "
                    f"{spelling!r} for {field_name!r} -- the LEI CDF layout changed"
                )
        lei_at = ENTITY_COLUMNS["lei"][0]
        for row in reader:
            if len(row) <= lei_at or row[lei_at] not in wanted:
                continue
            out[row[lei_at]] = {
                name: row[index] for name, (index, _) in ENTITY_COLUMNS.items() if name != "lei"
            }
    zf.close()
    return out


def build_parent_rows(
    relationships: list[dict[str, Any]],
    entities: dict[str, dict[str, str]],
    *,
    source_url: str,
    retrieved_at: str,
    result: BuildResult | None = None,
) -> pd.DataFrame:
    """Join names onto the relationships and return exactly `OUTPUT_COLUMNS`.

    A relationship whose child or parent LEI has no entity record is dropped and counted: without a
    legal name there is nothing for `services/ingest/organizations.py` to match on, and inventing
    one from the LEI would be a fabricated fact.
    """
    result = result if result is not None else BuildResult()
    rows: list[dict[str, Any]] = []
    for rel in relationships:
        child = entities.get(rel["child_lei"])
        parent = entities.get(rel["parent_lei"])
        if child is None or parent is None:
            result.dropped_unnamed_lei += 1
            continue
        rows.append(
            {
                "child_lei": rel["child_lei"],
                "child_legal_name": child["legal_name"],
                "child_country": child["country"],
                "child_region": child["region"],
                "child_city": child["city"],
                "child_jurisdiction": child["jurisdiction"],
                "child_entity_status": child["entity_status"],
                "parent_lei": rel["parent_lei"],
                "parent_legal_name": parent["legal_name"],
                "parent_country": parent["country"],
                "relationship_type": rel["relationship_type"],
                "relationship_status": rel["relationship_status"],
                "registration_status": rel["registration_status"],
                "period_start": rel["period_start"],
                "period_end": rel["period_end"],
                "accounting_period_end": rel["accounting_period_end"],
                "last_update_date": rel["last_update_date"],
                "source_id": SOURCE_ID,
                "source_url": source_url,
                "retrieved_at": retrieved_at,
                "licence": LICENCE,
            }
        )
    df = pd.DataFrame(rows, columns=OUTPUT_COLUMNS)
    result.rows = len(df)
    result.distinct_children = int(df["child_lei"].nunique()) if len(df) else 0
    result.distinct_parents = int(df["parent_lei"].nunique()) if len(df) else 0
    return df


def build(
    rr_zip: pathlib.Path, lei2_zip: pathlib.Path, *, source_url: str, retrieved_at: str
) -> tuple[pd.DataFrame, BuildResult]:
    result = BuildResult()
    relationships = read_relationships(rr_zip, result)
    wanted = {r["child_lei"] for r in relationships} | {r["parent_lei"] for r in relationships}
    result.leis_wanted = len(wanted)
    entities = read_entities(lei2_zip, wanted)
    result.leis_named = len(entities)
    df = build_parent_rows(
        relationships, entities, source_url=source_url, retrieved_at=retrieved_at, result=result
    )
    return df, result


# ------------------------------------------------------------------------------------ CLI
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=pathlib.Path, default=DEFAULT_OUT)
    parser.add_argument(
        "--rr-zip", type=pathlib.Path, help="an already-downloaded relationship golden-copy zip"
    )
    parser.add_argument("--lei2-zip", type=pathlib.Path, help="an already-downloaded entity golden-copy zip")
    parser.add_argument(
        "--download-dir",
        type=pathlib.Path,
        default=ROOT / "data" / "snapshots" / SOURCE_ID,
        help="where a fetched golden copy is stored (default data/snapshots/global.gleif.lei)",
    )
    parser.add_argument("--publishes-url", default=PUBLISHES_URL)
    parser.add_argument(
        "--retrieved-at",
        help="ISO 8601 UTC stamp to record on every row; only for a re-build from zips that were "
        "downloaded earlier, so the stored provenance stays the real fetch time, not the re-run's.",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    t0 = time.monotonic()
    files: list[dict[str, Any]] = []
    retrieved_at = args.retrieved_at or utcnow_iso()
    if args.rr_zip and args.lei2_zip:
        rr_zip, lei2_zip = args.rr_zip, args.lei2_zip
        for kind, path in (("rr", rr_zip), ("lei2", lei2_zip)):
            files.append({"kind": kind, "path": str(path), "bytes": path.stat().st_size})
    elif args.rr_zip or args.lei2_zip:
        parser.error("--rr-zip and --lei2-zip go together (both files are needed for names)")
    else:
        session = PoliteSession()
        specs = latest_publish(fetch_publish_index(session, args.publishes_url))
        for kind in ("rr", "lei2"):
            spec = download(session, specs[kind], args.download_dir)
            log.info("gleif: %s %s bytes from %s", kind, spec.downloaded_bytes, spec.url)
            files.append(spec.as_report())
        rr_zip = specs["rr"].path  # type: ignore[assignment]
        lei2_zip = specs["lei2"].path  # type: ignore[assignment]
        retrieved_at = args.retrieved_at or specs["rr"].retrieved_at or retrieved_at

    df, result = build(rr_zip, lei2_zip, source_url=DOWNLOAD_PAGE, retrieved_at=retrieved_at)
    result.files = files
    args.out.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(args.out, index=False)
    print(  # noqa: T201 — CLI summary line
        json.dumps({**result.as_report(), "out": str(args.out), "elapsed_s": round(time.monotonic() - t0, 2)})
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
