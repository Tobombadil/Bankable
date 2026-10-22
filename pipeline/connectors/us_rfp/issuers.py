"""The curated issuer registry, `data/vendored/rfp_issuers/issuers.yaml` (docs/28, US-303).

The connector reads only the rows this module clears, and fails closed on everything else
(docs/28 §8.2: "Read only the registry's `automated_reading: allowed` rows. Fail closed on any
other value, including a missing key."). A row is fetchable when, and only when:

- `automated_reading` is exactly `allowed`;
- `verdict` is `cleared` (tranche 1: the rows whose facts may be published; the link-out-only
  rows of tranche 1b wait for counsel item C-2 and a link-out surface, docs/28 §8.1);
- its `manifest.publication` is not `none`;
- it carries a `connector` block naming a known extractor, an https `listing_url`, an `issuer`
  name and a `jurisdiction`;
- the listing host is not one of the row's `link_only_hosts` (CT DEEP's document store, which
  robots.txt closes to every client).

Anything else is refused with the reason, and the registry never builds a connector class for it
(`connector_class_for`), so a `not_allowed` row cannot be fetched by any code path.
"""

from __future__ import annotations

import functools
import hashlib
import json
import pathlib
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[3]
REGISTRY_PATH = ROOT / "data" / "vendored" / "rfp_issuers" / "issuers.yaml"

#: The extractors `extract.py` implements, by the registry's `connector.extractor` value.
EXTRACTORS = ("aep_energy_rfps", "news_release_index", "notice_table_pdf")
#: Opportunity kinds a row may publish as (docs/21 §3.3; `pipeline.connectors.opportunity.KINDS`).
ROW_KINDS = ("rfp", "procurement_notice", "auction", "program")
ISSUER_TYPES = ("utility", "coop", "cca", "agency")


@dataclass(frozen=True)
class IssuerRow:
    id: str
    name: str
    raw: dict[str, Any] = field(repr=False)

    @property
    def connector(self) -> dict[str, Any]:
        block = self.raw.get("connector")
        return dict(block) if isinstance(block, dict) else {}

    @property
    def manifest(self) -> dict[str, Any]:
        block = self.raw.get("manifest")
        return dict(block) if isinstance(block, dict) else {}

    @property
    def extractor(self) -> str:
        return str(self.connector.get("extractor") or "")

    @property
    def listing_url(self) -> str:
        return str(self.connector.get("listing_url") or "")

    @property
    def listing_host(self) -> str:
        return urlsplit(self.listing_url.replace("{year}", "2000")).netloc.lower()

    @property
    def link_only_hosts(self) -> frozenset[str]:
        hosts = self.connector.get("link_only_hosts") or []
        return frozenset(str(h).lower() for h in hosts)

    @property
    def issuer(self) -> str:
        return str(self.connector.get("issuer") or "")

    @property
    def issuer_type(self) -> str:
        return str(self.connector.get("issuer_type") or "")

    @property
    def jurisdiction(self) -> str:
        return str(self.connector.get("jurisdiction") or "")

    @property
    def kind(self) -> str:
        return str(self.connector.get("kind") or "rfp")

    @property
    def terms_url(self) -> str:
        return str(self.raw.get("terms_url") or "")

    @property
    def terms_quotes(self) -> list[str]:
        quotes = self.raw.get("terms_quote") or []
        return [str(q) for q in quotes if str(q).strip()]

    @property
    def states(self) -> list[str]:
        return [str(s) for s in self.raw.get("states") or []]

    def config_digest(self) -> str:
        """What in this row changes what the parser produces: the connector block (selectors, kind,
        issuer, jurisdiction). Part of the connector's effective parser version."""
        blob = json.dumps(self.connector, sort_keys=True, default=str)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def refusal(self) -> str | None:
        """Why this row may not be fetched, or None when it may (module docstring)."""
        if self.raw.get("automated_reading") != "allowed":
            return f"automated_reading={self.raw.get('automated_reading')!r}; only 'allowed' is read"
        if self.raw.get("verdict") != "cleared":
            return f"verdict={self.raw.get('verdict')!r}; tranche 1 reads only 'cleared' rows (docs/28 §8.1)"
        if self.manifest.get("publication") in (None, "none"):
            return "manifest publication is none"
        if not self.connector:
            return "no connector block"
        if self.extractor not in EXTRACTORS:
            return f"connector.extractor={self.extractor!r} is not one of {EXTRACTORS}"
        if not self.listing_url.startswith("https://"):
            return f"connector.listing_url={self.listing_url!r} is not https"
        if self.listing_host in self.link_only_hosts:
            return f"the listing host {self.listing_host} is link-only for this row"
        if not self.issuer or not self.jurisdiction:
            return "connector.issuer and connector.jurisdiction are required"
        if self.issuer_type not in ISSUER_TYPES:
            return f"connector.issuer_type={self.issuer_type!r} is not one of {ISSUER_TYPES}"
        if self.kind not in ROW_KINDS:
            return f"connector.kind={self.kind!r} is not one of {ROW_KINDS}"
        return None


def load_rows(path: pathlib.Path = REGISTRY_PATH) -> dict[str, IssuerRow]:
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    rows: dict[str, IssuerRow] = {}
    for entry in doc.get("issuers") or []:
        row = IssuerRow(id=str(entry["id"]), name=str(entry.get("name") or ""), raw=dict(entry))
        if row.id in rows:
            raise ValueError(f"{path.name}: duplicate issuer id {row.id}")
        rows[row.id] = row
    return rows


@functools.cache
def registry_rows() -> dict[str, IssuerRow]:
    """The vendored registry, read once per process."""
    return load_rows()


def fetchable_ids(rows: dict[str, IssuerRow] | None = None) -> list[str]:
    """Every row id the connector may read today, in registry order."""
    rows = rows if rows is not None else registry_rows()
    return [rid for rid, row in rows.items() if row.refusal() is None]
