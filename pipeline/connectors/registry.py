"""Connector registry over `data/sources.yaml` (docs/20 §3.1, §4.3; docs/04 DA-12).

- The manifest is the single source of truth. A source with no connector module is a manifest
  entry only and lists as `unimplemented`.
- Publication gating is code, not convention: a source whose `reuse` is `restricted` or `unknown`
  is refused unless `allow_restricted=True`, and even then its outputs are quarantined (see
  `pipeline.connectors.store`). Private aggregators (`category: aggregator` + `reuse: restricted`,
  and the names in `CLAUDE.md`) fail to register under any flag.
- Host politeness: `max_rps` comes from an explicit `rate_limit_rps` field, else from a limit
  quoted in the entry's notes (`≤0.5 rps`, `1 per 5 s`, `6/min`), else the per-host defaults
  from docs/02 §7, else 1 rps.

Connector modules live at `pipeline/connectors/<source_id with '.' -> '_'>/connector.py`
(docs/04 E-1; dots are not importable) and expose a class named `Connector`.
"""

from __future__ import annotations

import hashlib
import importlib
import pathlib
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import yaml

if TYPE_CHECKING:
    from pipeline.connectors.base import Connector

ROOT = pathlib.Path(__file__).resolve().parents[2]
SOURCES_YAML = ROOT / "data" / "sources.yaml"
CONNECTORS_DIR = pathlib.Path(__file__).resolve().parent

PUBLISHABLE_REUSE = frozenset({"open", "attribution"})
GATED_REUSE = frozenset({"restricted", "unknown"})
NEVER_INGEST = frozenset({"us.gridtracker.interconnection_fyi"})
NEVER_INGEST_NAMES = ("interconnection.fyi", "cleanview", "energy adepto", "bidnet", "halcyon", "enverus")

# docs/02 §7 host limits (requests per second). Everything else defaults to 1 rps.
HOST_DEFAULT_RPS: dict[str, float] = {
    "elibrary.ferc.gov": 0.5,
    "ecollection.ferc.gov": 0.5,
    "www.ferc.gov": 0.5,
    "api.gdeltproject.org": 0.2,
    "api.pjm.com": 0.1,
    "www.ercot.com": 0.5,
}
_RPS_PATTERNS = (
    re.compile(r"(?:≤|<=|<)?\s*(\d+(?:\.\d+)?)\s*rps", re.I),
    re.compile(r"(\d+(?:\.\d+)?)\s*(?:requests?|req|calls?)?\s*(?:per|/)\s*(?:second|sec|s)\b", re.I),
)
_PER_N_SEC = re.compile(r"(?:one|1)\s*(?:request\s*)?per\s*(\d+)\s*s(?:ec|econds)?\b", re.I)
_PER_MIN = re.compile(r"(\d+)\s*(?:requests?|connections?|calls?|req)?\s*(?:per|/)\s*min", re.I)


class RegistrationError(Exception):
    """The source may not be registered at all (private aggregator, unknown id)."""


@dataclass
class SourceEntry:
    id: str
    name: str
    url: str
    category: str
    access: str
    reuse: str
    cadence: str
    jurisdiction: str = ""
    operator: str = ""
    format: str = ""
    tier: int = 3
    effort: str = ""
    license: str = ""
    notes: str = ""
    connector: str = ""
    verified: dict[str, Any] = field(default_factory=dict)
    probe: dict[str, Any] = field(default_factory=dict)
    egress: str = ""
    max_rps: float = 1.0
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def host(self) -> str:
        return urlsplit(self.url).netloc

    @property
    def licence_id(self) -> str:
        """Key of the licence text in force at fetch time (docs/21 §3.19 invariant L2).

        The registry has no separate licence table yet, so the id is derived from the `license`
        clause recorded for the source: a change to the quoted terms yields a new id while old
        observations keep the one they were fetched under.
        """
        digest = hashlib.sha256((self.license or "").encode("utf-8")).hexdigest()[:10]
        return f"{self.id}#{digest}"

    @property
    def gated(self) -> bool:
        return self.reuse in GATED_REUSE

    @property
    def never_ingest(self) -> bool:
        low = f"{self.name} {self.url} {self.notes}".lower()
        return (
            self.id in NEVER_INGEST
            or (self.category == "aggregator" and self.reuse == "restricted")
            or (any(n in low for n in NEVER_INGEST_NAMES) and self.category == "aggregator")
        )

    @property
    def module_name(self) -> str:
        return self.id.replace(".", "_").replace("-", "_")

    @property
    def implemented(self) -> bool:
        return (CONNECTORS_DIR / self.module_name / "connector.py").exists()

    @classmethod
    def from_yaml(cls, entry: dict[str, Any]) -> SourceEntry:
        e = dict(entry)
        src = cls(
            id=str(e["id"]),
            name=str(e.get("name", "")),
            url=str(e.get("url", "")),
            category=str(e.get("category", "")),
            access=str(e.get("access", "")),
            reuse=str(e.get("reuse", "unknown")),
            cadence=str(e.get("cadence", "")),
            jurisdiction=str(e.get("jurisdiction", "")),
            operator=str(e.get("operator", "")),
            format=str(e.get("format", "")),
            tier=int(e.get("tier", 3) or 3),
            effort=str(e.get("effort", "")),
            license=str(e.get("license", "")),
            notes=str(e.get("notes", "")),
            connector=str(e.get("connector", "")),
            verified=dict(e.get("verified") or {}),
            probe=dict(e.get("probe") or {}),
            egress=str(e.get("egress") or _default_egress(str(e.get("access", "")))),
            raw=e,
        )
        src.max_rps = _rate_limit(src, e)
        return src


def _default_egress(access: str) -> str:
    return "browser" if access == "js_app" else "plain"


def _rate_limit(src: SourceEntry, entry: dict[str, Any]) -> float:
    if entry.get("rate_limit_rps"):
        return float(entry["rate_limit_rps"])
    text = f"{src.notes} {src.license}"
    m = _PER_N_SEC.search(text)
    if m:
        return 1.0 / float(m.group(1))
    m = _PER_MIN.search(text)
    if m:
        return float(m.group(1)) / 60.0
    for pat in _RPS_PATTERNS:
        m = pat.search(text)
        if m:
            return float(m.group(1))
    return HOST_DEFAULT_RPS.get(src.host, 1.0)


class Registry:
    def __init__(self, path: pathlib.Path = SOURCES_YAML) -> None:
        self.path = path
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        self.version = str(doc.get("version"))
        self.sources: dict[str, SourceEntry] = {}
        for entry in doc["sources"]:
            src = SourceEntry.from_yaml(entry)
            self.sources[src.id] = src

    def __contains__(self, source_id: str) -> bool:
        return source_id in self.sources

    def get(self, source_id: str) -> SourceEntry:
        try:
            return self.sources[source_id]
        except KeyError:
            raise RegistrationError(f"{source_id!r} is not in {self.path.name}") from None

    def ids(self) -> list[str]:
        return list(self.sources)

    def status(self) -> list[dict[str, Any]]:
        """One row per source for the admin view: implemented / unimplemented / gated / excluded."""
        rows = []
        for s in self.sources.values():
            state = (
                "excluded"
                if s.never_ingest
                else "gated"
                if s.gated
                else "implemented"
                if s.implemented
                else "unimplemented"
            )
            rows.append(
                {
                    "id": s.id,
                    "state": state,
                    "reuse": s.reuse,
                    "implemented": s.implemented,
                    "egress": s.egress,
                    "max_rps": s.max_rps,
                    "cadence": s.cadence,
                }
            )
        return rows

    def connector_class(self, source_id: str) -> type[Connector]:
        src = self.get(source_id)
        if src.never_ingest:
            raise RegistrationError(f"{source_id} is a private aggregator; it has no connector by design")
        if not src.implemented:
            raise RegistrationError(f"{source_id} is unimplemented (manifest entry only)")
        module = importlib.import_module(f"pipeline.connectors.{src.module_name}.connector")
        cls: type[Connector] = module.Connector
        if cls.source_id != source_id:
            raise RegistrationError(f"{module.__name__} declares {cls.source_id}, expected {source_id}")
        return cls

    def instantiate(self, source_id: str, *, allow_restricted: bool = False, **kwargs: Any) -> Connector:
        """Build a connector. Gated sources refuse unless `allow_restricted=True` (docs/21 §8)."""
        from pipeline.connectors.base import GateViolation  # local import: base imports this module

        src = self.get(source_id)
        if src.gated and not allow_restricted:
            raise GateViolation(
                f"{source_id} has reuse={src.reuse!r}; runs are refused without allow_restricted=True "
                "and never write publishable output (docs/21 §8, CLAUDE.md)"
            )
        cls = self.connector_class(source_id)
        return cls(src, **kwargs)
