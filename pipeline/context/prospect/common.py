"""Shared plumbing for the prospect layer builders: regions, provenance, polite fetching, run records.

Every stored value carries provenance (CLAUDE.md): `source_id`, `source_url`, `retrieved_at`, `licence`
and the dataset's own `vintage`. `provenance_for` reads the licence quote, the credit line and the
licence URL from `data/sources.yaml`, so the manifest stays the one place a source's terms are written.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import pathlib
from dataclasses import asdict, dataclass, field
from typing import Any

from pipeline.connectors.http import HttpFailed, PoliteSession
from pipeline.connectors.registry import Registry
from pipeline.connectors.store import Store, ts_token
from services.context_sample.paths import prospect_dir

#: (min_lon, min_lat, max_lon, max_lat). Texas is the development region (docs/25 "Prospecting
#: layers": disk on the development machine is shared, so nothing national is built there); CONUS is
#: what GitHub Actions builds and uploads to R2.
REGIONS: dict[str, tuple[float, float, float, float]] = {
    "texas": (-106.65, 25.83, -93.50, 36.51),
    "conus": (-125.0, 24.3, -66.9, 49.5),
}
#: US state names (as the federal sources spell them) per region, for sources queried by state.
REGION_STATES: dict[str, tuple[str, ...] | None] = {"texas": ("Texas",), "conus": None}
REGION_STATE_CODES: dict[str, tuple[str, ...] | None] = {"texas": ("TX",), "conus": None}


def now_iso() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def snapshot_time(path: pathlib.Path) -> str:
    """When a recorded snapshot was taken, as best the file says: its modification time (UTC). A build from
    a snapshot stamps this, not the build time, as `retrieved_at`."""
    mtime = dt.datetime.fromtimestamp(path.stat().st_mtime, dt.UTC)
    return mtime.isoformat(timespec="seconds").replace("+00:00", "Z")


def snapshot_retrieved_at(path: pathlib.Path) -> str:
    """`retrieved_at` for a file a person downloaded and uploaded to the inbox: the `downloaded_at` they
    recorded in `<name>.json` beside it (`{"downloaded_at": "<ISO time>"}`), else the file's own time."""
    import json

    sidecar = path.with_suffix(".json")
    if sidecar != path and sidecar.is_file():
        try:
            when = json.loads(sidecar.read_text()).get("downloaded_at")
        except (ValueError, AttributeError):
            when = None
        if isinstance(when, str) and when.strip():
            return when.strip()
    return snapshot_time(path)


def output_dir(path: pathlib.Path | None = None) -> pathlib.Path:
    out = path or prospect_dir()
    out.mkdir(parents=True, exist_ok=True)
    return out


@dataclass(frozen=True)
class Provenance:
    source_id: str
    source_url: str
    retrieved_at: str
    #: The licence's short name ("CC BY 4.0", "Public domain (USGS)"), as other context records carry a
    #: licence class; the quoted terms stay in data/sources.yaml under `licence_id`.
    licence: str
    vintage: str | None
    #: `Registry.licence_id`: the key of the terms quoted in data/sources.yaml when this was built, so a
    #: change to the quote is visible against stored values.
    licence_id: str | None = None
    licence_url: str | None = None
    attribution: str | None = None
    #: A notice the terms require in every copy (NREL/NLR's data notice), verbatim.
    notice: str | None = None

    def record(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}

    def columns(self) -> dict[str, Any]:
        """The provenance columns every GeoParquet row carries."""
        return {
            "source_id": self.source_id,
            "source_url": self.source_url,
            "retrieved_at": self.retrieved_at,
            "licence": self.licence,
            "vintage": self.vintage,
        }

    def cog_items(self) -> dict[str, str]:
        """The provenance as GDAL metadata items inside a COG."""
        return {k: str(v) for k, v in self.record().items()}


def provenance_for(
    source_id: str,
    *,
    source_url: str,
    retrieved_at: str,
    vintage: str | None,
    licence: str,
    notice: str | None = None,
    registry: Registry | None = None,
) -> Provenance:
    """Provenance for one layer: `licence` is the short licence name; the credit, licence URL and the key
    of the quoted terms come from the source's row in data/sources.yaml (which must have one)."""
    entry = (registry or Registry()).get(source_id)
    if not entry.license:
        raise ValueError(
            f"{source_id}: no licence quoted in data/sources.yaml; quote the terms before building"
        )
    return Provenance(
        source_id=source_id,
        source_url=source_url,
        retrieved_at=retrieved_at,
        licence=licence,
        vintage=vintage,
        licence_id=entry.licence_id,
        licence_url=entry.licence_url or None,
        attribution=entry.credit_text,
        notice=notice,
    )


@dataclass
class FetchLog:
    """What one build fetched, for the run record (docs/20 §12: the run record is the commit marker)."""

    source_id: str
    requests: list[dict[str, Any]] = field(default_factory=list)

    def add(self, **entry: Any) -> None:
        self.requests.append(entry)

    def write(self, store: Store | None, **extra: Any) -> pathlib.Path | None:
        if store is None:
            return None
        record = {"source_id": self.source_id, "retrieved_at": now_iso(), "requests": self.requests, **extra}
        return store.write_run(self.source_id, ts_token(dt.datetime.now(dt.UTC)), record)


def download(
    session: PoliteSession,
    url: str,
    dest: pathlib.Path,
    *,
    log: FetchLog,
    honour_robots: bool = True,
    chunk: int = 1 << 20,
) -> dict[str, Any]:
    """Stream `url` to `dest` (no whole-file buffering); returns bytes, sha256 and the validators.
    Raw downloads are working files: the caller deletes them once the compact layer is written."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    resp = session.get(url, honour_robots=honour_robots, stream=True)
    if resp.status_code != 200:
        resp.close()
        raise HttpFailed(f"{url} answered HTTP {resp.status_code}")
    digest = hashlib.sha256()
    size = 0
    with dest.open("wb") as fh:
        for block in resp.iter_content(chunk):
            fh.write(block)
            digest.update(block)
            size += len(block)
    info = {
        "url": url,
        "status": resp.status_code,
        "bytes": size,
        "sha256": digest.hexdigest(),
        "last_modified": resp.headers.get("Last-Modified"),
        "etag": resp.headers.get("ETag"),
        "content_type": resp.headers.get("Content-Type"),
        "retrieved_at": now_iso(),
    }
    log.add(**info)
    return info


def get_json(session: PoliteSession, url: str, *, log: FetchLog, params: dict[str, Any] | None = None) -> Any:
    resp = session.get(url, honour_robots=False, params=params)
    if resp.status_code != 200:
        raise HttpFailed(f"{url} answered HTTP {resp.status_code}")
    log.add(url=resp.url, status=resp.status_code, bytes=len(resp.content), retrieved_at=now_iso())
    return resp.json()


class TermsChanged(Exception):
    """A quoted clause is no longer on the source's terms page: stop and re-read before serving."""


def _normalise_text(text: str) -> str:
    import html
    import re

    text = re.sub(r"<[^>]+>", " ", html.unescape(text))
    return re.sub(r"\s+", " ", text).strip().lower()


def verify_terms(
    session: PoliteSession, url: str, phrases: tuple[str, ...], *, log: FetchLog, honour_robots: bool = True
) -> None:
    """Re-read the terms page and require every quoted phrase on it (whitespace and case folded), so a
    build never runs on terms that changed since they were quoted in data/sources.yaml."""
    resp = session.get(url, honour_robots=honour_robots)
    if resp.status_code != 200:
        raise TermsChanged(f"terms page {url} answered HTTP {resp.status_code}")
    text = resp.text
    if "json" in (resp.headers.get("Content-Type") or ""):
        # An ArcGIS item's `licenseInfo` is HTML inside JSON: check the decoded strings.
        import json

        def strings(node: Any) -> list[str]:
            if isinstance(node, str):
                return [node]
            if isinstance(node, dict):
                return [s for v in node.values() for s in strings(v)]
            if isinstance(node, list):
                return [s for v in node for s in strings(v)]
            return []

        text = " ".join(strings(json.loads(text)))
    check_phrases(text, phrases, url=url)
    log.add(url=url, status=resp.status_code, terms_checked=len(phrases), retrieved_at=now_iso())


def check_phrases(text: str, phrases: tuple[str, ...], *, url: str) -> None:
    page = _normalise_text(text)
    missing = [p for p in phrases if _normalise_text(p) not in page]
    if missing:
        raise TermsChanged(f"{url} no longer carries: {missing}")


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while block := fh.read(1 << 20):
            digest.update(block)
    return digest.hexdigest()


def extract_gdb(zip_path: pathlib.Path, dest: pathlib.Path) -> pathlib.Path:
    """Extract the first file geodatabase (`*.gdb/`) in `zip_path` under `dest`; returns its directory.

    Only the geodatabase's members are written (a PAD-US state zip also carries a KMZ; Aqueduct's carries
    CSVs), streamed in 1 MB blocks. A member whose name would land outside `dest` (an absolute path or
    `..`) is refused: the archive is downloaded data, not trusted input."""
    import shutil
    import zipfile

    root_dir = dest.resolve()
    with zipfile.ZipFile(zip_path) as z:
        members = [n for n in z.namelist() if ".gdb/" in n and not n.endswith("/") and "__MACOSX" not in n]
        if not members:
            raise FileNotFoundError(f"no .gdb in {zip_path.name}")
        gdb_root = members[0].split(".gdb/")[0] + ".gdb"
        for name in members:
            if not name.startswith(gdb_root + "/"):
                continue
            target = (dest / name).resolve()
            if not target.is_relative_to(root_dir):
                raise ValueError(f"{zip_path.name}: member {name!r} would extract outside {dest}")
            target.parent.mkdir(parents=True, exist_ok=True)
            with z.open(name) as src, target.open("wb") as dst:
                shutil.copyfileobj(src, dst, 1 << 20)
    return dest / gdb_root
