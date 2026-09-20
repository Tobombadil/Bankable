"""EPA Renewable Fuel Standard registrations -> per-facility D-code pathway features
(owner decision 2026-09-18: "RIN pathway (D3/D5/D6)" is in the objective feature set; ADR 0008 §4).

    fetch()          -> FetchResult    network; snapshot + run record under data/snapshots|runs
    parse(bytes)     -> DataFrame      the registration workbook, no network
    build_rows(...)  -> DataFrame      one row per registered facility, written to parquet

**Which file (measured 2026-09-19).** `us.phmsa`-style landing pages are not the issue here; the
RFS public-data page (`data/sources.yaml`, `us.epa.rfs_public_data`) turned out to publish no
facility file at all: its tables are a Qlik Sense application and its "Historical Monthly Data"
links are aggregate RIN volumes with no facility, no pathway and no location (checked live
2026-09-19, which is what the manifest's 2026-09-18 "behind JS" note anticipated). The registered
facilities with their D codes are published one level up in the same EPA tree, on "Registered
Companies and Facilities in EPA's Fuel Programs", as a daily-refreshed workbook:

    https://cdxoarapps.epa.gov/oar-otaq-reg-III/rest/public/reports/Part80FuelsProgramsList

That file is what this connector reads. Its `Part 80 Company Information` sheet carries one row
per (company, facility, registration) with `D Code`, `Fuel Created`, `Facility Type` and
`Facility Activities`; 1,145 of its 13,536 rows carry a D code. The sheet's second block of
address columns repeats the labels of the first (`Address 1`, `City`, `State` …) for the facility,
so columns are read positionally, not by label.

**`first_registered_year` is null.** The registration list is a current-state snapshot: it states
no registration or approval date for any facility, and EPA's pathway-determination letters (the
only dated artifact) cover a few dozen petitions, not the fleet. The key is emitted as `None` with
a `feature_flags` note rather than being inferred from anything.

**RNG separators are not producers.** The workbook's second sheet (`RNG RIN Separator
Information`) lists RIN separation points -- withdrawal points and CNG/LNG dispensing stations --
which are not the facilities that generate the RINs, so it is not read into the feature set.

The .xls is a legacy OLE2/BIFF8 workbook and this repository declares no reader for that format,
so one is included here, in the same spirit as `pipeline/context/shapefile.py` (a pure-Python
shapefile/DBF reader rather than a geospatial stack). It implements only what this one file needs:
the compound-file container, the sheet directory, the shared-string table and cell records.

CLI: `python -m pipeline.context.rfs [--snapshot PATH]`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import re
import struct
import time
from dataclasses import dataclass
from typing import Any

import pandas as pd

from pipeline.connectors.base import ParseError, json_default
from pipeline.connectors.http import HttpFailed, PoliteSession
from pipeline.connectors.store import Store, ts_token
from pipeline.context import fuels

SOURCE_ID = "us.epa.rfs_public_data"
REGISTRATION_URL = "https://cdxoarapps.epa.gov/oar-otaq-reg-III/rest/public/reports/Part80FuelsProgramsList"
REGISTRATION_PAGE = (
    "https://www.epa.gov/fuels-registration-reporting-and-compliance-help/"
    "registered-companies-and-facilities-epas-fuel"
)
RATE_LIMITS: dict[str, float] = {"cdxoarapps.epa.gov": 0.5, "www.epa.gov": 0.5}

COMPANY_SHEET = "Part 80 Company Information"
D_CODE_RE = re.compile(r"\bD\s*([1-9])\b", re.IGNORECASE)

#: Only these D codes are in scope for this wave's asset types (owner decision 2026-09-18:
#: "D3 cellulosic incl. RNG, D5 advanced, D6 renewable fuel"). Other codes a facility carries
#: (D4 biomass-based diesel, D7) are kept on the row as stated -- they are facts about the same
#: facility -- but they never create a match on their own.
WAVE_D_CODES = ("D3", "D5", "D6")

FIRST_REGISTERED_UNAVAILABLE = (
    "first_registered_year unavailable: EPA's Part 80 registration list is a current-state "
    "snapshot and states no registration or approval date"
)

COLUMNS: list[str] = [
    "source_id",
    "source_url",
    "retrieved_at",
    "licence",
    "licence_id",
    "facility_key",
    "company_id",
    "company_name",
    "facility_name",
    "facility_city",
    "facility_state",
    "facility_type",
    "facility_activities",
    "program_types",
    "fuel_created",
    "d_codes",
    "pathway_count",
    "first_registered_year",
    "registration_rows",
    "feature_flags",
]


# ============================================================ legacy .xls (OLE2 + BIFF8) reader
class XlsError(ParseError):
    """The bytes are not a readable BIFF8 workbook."""


OLE_SIGNATURE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_FREESECT = 0xFFFFFFFF
_ENDOFCHAIN = 0xFFFFFFFE


def _ole_streams(data: bytes) -> dict[str, bytes]:
    """Every stream of an OLE2 compound file, by name.

    Only what a .xls needs: the FAT (via the header DIFAT and any DIFAT sectors), the directory,
    the mini-FAT for streams below the cutoff, and 512- or 4096-byte sectors.
    """
    if data[:8] != OLE_SIGNATURE:
        raise XlsError("not an OLE2 compound file (bad signature)")
    sector_size = 1 << struct.unpack_from("<H", data, 0x1E)[0]
    mini_size = 1 << struct.unpack_from("<H", data, 0x20)[0]
    fat_count = struct.unpack_from("<I", data, 0x2C)[0]
    dir_start = struct.unpack_from("<I", data, 0x30)[0]
    mini_cutoff = struct.unpack_from("<I", data, 0x38)[0]
    mini_fat_start = struct.unpack_from("<I", data, 0x3C)[0]
    difat_start = struct.unpack_from("<I", data, 0x44)[0]
    difat_count = struct.unpack_from("<I", data, 0x48)[0]

    def sector(index: int) -> bytes:
        start = sector_size + index * sector_size
        chunk = data[start : start + sector_size]
        if len(chunk) != sector_size:
            raise XlsError(f"sector {index} is past the end of the file")
        return chunk

    difat = list(struct.unpack_from("<109I", data, 0x4C))
    nxt, seen = difat_start, 0
    while nxt not in (_ENDOFCHAIN, _FREESECT) and seen <= difat_count:
        block = struct.unpack(f"<{sector_size // 4}I", sector(nxt))
        difat.extend(block[:-1])
        nxt = block[-1]
        seen += 1

    fat: list[int] = []
    for index in difat[:fat_count]:
        if index in (_FREESECT, _ENDOFCHAIN):
            continue
        fat.extend(struct.unpack(f"<{sector_size // 4}I", sector(index)))

    def chain(start: int, table: list[int]) -> list[int]:
        out: list[int] = []
        current = start
        while current not in (_ENDOFCHAIN, _FREESECT) and current < len(table):
            out.append(current)
            current = table[current]
            if len(out) > len(table):
                raise XlsError("cyclic sector chain")
        return out

    def read_chain(start: int, size: int) -> bytes:
        return b"".join(sector(i) for i in chain(start, fat))[:size]

    directory = b"".join(sector(i) for i in chain(dir_start, fat))
    entries: list[tuple[str, int, int, int]] = []
    for offset in range(0, len(directory), 128):
        entry = directory[offset : offset + 128]
        if len(entry) < 128:
            break
        name_len = struct.unpack_from("<H", entry, 0x40)[0]
        if name_len < 2:
            continue
        name = entry[: name_len - 2].decode("utf-16-le", "replace")
        entry_type = entry[0x42]
        start_sector = struct.unpack_from("<I", entry, 0x74)[0]
        size = struct.unpack_from("<Q", entry, 0x78)[0]
        entries.append((name, entry_type, start_sector, int(size)))
    if not entries:
        raise XlsError("compound file has no directory entries")

    root = next((e for e in entries if e[1] == 5), None)
    mini_stream = b""
    if root is not None and root[3]:
        mini_stream = read_chain(root[2], root[3])
    mini_fat: list[int] = []
    if mini_fat_start not in (_ENDOFCHAIN, _FREESECT):
        mini_fat = [
            v for i in chain(mini_fat_start, fat) for v in struct.unpack(f"<{sector_size // 4}I", sector(i))
        ]

    streams: dict[str, bytes] = {}
    for name, entry_type, start, size in entries:
        if entry_type != 2 or not size:
            continue
        if size < mini_cutoff:
            payload = b"".join(
                mini_stream[i * mini_size : (i + 1) * mini_size] for i in chain(start, mini_fat)
            )[:size]
        else:
            payload = read_chain(start, size)
        streams[name] = payload
    return streams


def _biff_records(stream: bytes) -> list[tuple[int, bytes]]:
    records: list[tuple[int, bytes]] = []
    offset = 0
    while offset + 4 <= len(stream):
        code, length = struct.unpack_from("<HH", stream, offset)
        payload = stream[offset + 4 : offset + 4 + length]
        records.append((code, payload))
        offset += 4 + length
    return records


def _sst_strings(records: list[tuple[int, bytes]], start: int) -> list[str]:
    """The shared-string table: the SST record plus its CONTINUE records. A string may straddle a
    CONTINUE boundary, where its remaining characters carry a fresh compression flag -- which is
    why the blocks are concatenated with their boundaries remembered rather than simply joined."""
    blocks = [records[start][1]]
    index = start + 1
    while index < len(records) and records[index][0] == 0x003C:  # CONTINUE
        blocks.append(records[index][1])
        index += 1
    data = b"".join(blocks)
    boundaries: set[int] = set()
    position = 0
    for block in blocks[:-1]:
        position += len(block)
        boundaries.add(position)

    unique = struct.unpack_from("<I", data, 4)[0]
    cursor = 8
    out: list[str] = []
    for _ in range(unique):
        if cursor + 3 > len(data):
            break
        cch = struct.unpack_from("<H", data, cursor)[0]
        flags = data[cursor + 2]
        cursor += 3
        rich_runs = 0
        ext_size = 0
        if flags & 0x08:
            rich_runs = struct.unpack_from("<H", data, cursor)[0]
            cursor += 2
        if flags & 0x04:
            ext_size = struct.unpack_from("<I", data, cursor)[0]
            cursor += 4
        chars: list[str] = []
        remaining = cch
        high = bool(flags & 0x01)
        while remaining:
            next_boundary = min((b for b in boundaries if b > cursor), default=len(data))
            limit = min(next_boundary, len(data)) - cursor
            take = min(remaining, limit // 2 if high else limit)
            if take <= 0:
                break
            width = 2 if high else 1
            raw = data[cursor : cursor + take * width]
            chars.append(raw.decode("utf-16-le" if high else "latin-1", "replace"))
            cursor += take * width
            remaining -= take
            if remaining and cursor in boundaries:
                high = bool(data[cursor] & 0x01)
                cursor += 1
        cursor += 4 * rich_runs + ext_size
        out.append("".join(chars))
    return out


def _rk_value(rk: int) -> float:
    multiplied = bool(rk & 0x01)
    if rk & 0x02:
        value = float(rk >> 2 if rk >> 2 < 2**29 else (rk >> 2) - 2**30)
    else:
        value = struct.unpack("<d", struct.pack("<Q", (rk & 0xFFFFFFFC) << 32))[0]
    return value / 100 if multiplied else value


def read_xls_sheet(content: bytes, sheet_name: str) -> list[list[Any]]:
    """One worksheet of a BIFF8 workbook as a list of rows (ragged rows are padded)."""
    streams = _ole_streams(content)
    stream = streams.get("Workbook") or streams.get("Book")
    if stream is None:
        raise XlsError(f"no Workbook stream (streams: {sorted(streams)[:6]})")
    records = _biff_records(stream)

    sheets: list[tuple[str, int]] = []
    strings: list[str] = []
    for index, (code, payload) in enumerate(records):
        if code == 0x0085 and len(payload) >= 8:  # BOUNDSHEET
            position = struct.unpack_from("<I", payload, 0)[0]
            length = payload[6]
            flags = payload[7]
            raw = payload[8 : 8 + (length * 2 if flags & 0x01 else length)]
            name = raw.decode("utf-16-le" if flags & 0x01 else "latin-1", "replace")
            sheets.append((name, position))
        elif code == 0x00FC and not strings:  # SST
            strings = _sst_strings(records, index)
    match = next((p for n, p in sheets if n == sheet_name), None)
    if match is None:
        raise XlsError(f"no sheet named {sheet_name!r} (sheets: {[n for n, _ in sheets]})")

    cells: dict[tuple[int, int], Any] = {}
    offset = match
    max_row = max_col = -1
    while offset + 4 <= len(stream):
        code, length = struct.unpack_from("<HH", stream, offset)
        payload = stream[offset + 4 : offset + 4 + length]
        offset += 4 + length
        if code == 0x000A:  # EOF of this sheet's substream
            break
        value: Any = None
        row = col = -1
        if code == 0x00FD and len(payload) >= 10:  # LABELSST
            row, col, sst = struct.unpack_from("<HHxxI", payload, 0)
            value = strings[sst] if sst < len(strings) else None
        elif code == 0x0204 and len(payload) >= 8:  # LABEL
            row, col = struct.unpack_from("<HH", payload, 0)
            cch = struct.unpack_from("<H", payload, 6)[0]
            flags = payload[8]
            raw = payload[9 : 9 + (cch * 2 if flags & 0x01 else cch)]
            value = raw.decode("utf-16-le" if flags & 0x01 else "latin-1", "replace")
        elif code == 0x0203 and len(payload) >= 14:  # NUMBER
            row, col = struct.unpack_from("<HH", payload, 0)
            value = struct.unpack_from("<d", payload, 6)[0]
        elif code == 0x027E and len(payload) >= 10:  # RK
            row, col = struct.unpack_from("<HH", payload, 0)
            value = _rk_value(struct.unpack_from("<I", payload, 6)[0])
        elif code == 0x00BD and len(payload) >= 6:  # MULRK
            row, first = struct.unpack_from("<HH", payload, 0)
            count = (len(payload) - 6) // 6
            for i in range(count):
                rk = struct.unpack_from("<I", payload, 4 + i * 6 + 2)[0]
                cells[(row, first + i)] = _rk_value(rk)
                max_row, max_col = max(max_row, row), max(max_col, first + i)
            continue
        if row >= 0 and col >= 0:
            cells[(row, col)] = value
            max_row, max_col = max(max_row, row), max(max_col, col)
    if max_row < 0:
        return []
    return [[cells.get((r, c)) for c in range(max_col + 1)] for r in range(max_row + 1)]


# --------------------------------------------------------------------------------------- fetch
@dataclass
class FetchResult:
    source_url: str
    retrieved_at: str
    content: bytes
    sha256: str
    last_modified: str | None = None
    snapshot_path: pathlib.Path | None = None
    run_path: pathlib.Path | None = None

    @property
    def bytes_(self) -> int:
        return len(self.content)


def polite_session() -> PoliteSession:
    return PoliteSession(rate_limits=RATE_LIMITS, default_rps=0.5, timeout=180.0)


def fetch(
    *, session: PoliteSession | None = None, store: Store | None = None, write: bool = True
) -> FetchResult:
    session = session or polite_session()
    store = store or Store()
    resp = session.get(REGISTRATION_URL, honour_robots=True)
    if resp.status_code != 200 or resp.content[:8] != OLE_SIGNATURE:
        raise HttpFailed(
            f"{REGISTRATION_URL} answered HTTP {resp.status_code} "
            f"({resp.headers.get('Content-Type')}), not an .xls workbook"
        )
    content: bytes = resp.content
    retrieved_at = fuels.iso(fuels.utc_now())
    result = FetchResult(
        source_url=REGISTRATION_URL,
        retrieved_at=retrieved_at,
        content=content,
        sha256=hashlib.sha256(content).hexdigest(),
        last_modified=resp.headers.get("Last-Modified"),
    )
    if write:
        token = ts_token(fuels.utc_now())
        result.snapshot_path = store.write_snapshot(SOURCE_ID, token, "xls", content)
        result.run_path = store.write_run(
            SOURCE_ID,
            token,
            {
                "id": f"{SOURCE_ID}:{token}",
                "source_id": SOURCE_ID,
                "kind": "feature",
                "status": "ok",
                "retrieved_at": retrieved_at,
                "bytes": len(content),
                "snapshot": {
                    "fetched_url": REGISTRATION_URL,
                    "listed_on": REGISTRATION_PAGE,
                    "byte_size": len(content),
                    "sha256": result.sha256,
                    "last_modified": result.last_modified,
                    "path": str(result.snapshot_path.relative_to(store.root)),
                },
                "requests_made": session.requests_made,
            },
        )
    return result


# --------------------------------------------------------------------------------------- parse
def parse(content: bytes) -> pd.DataFrame:
    """The company/facility sheet as a frame. The sheet repeats its address column labels for the
    facility block, so columns are taken positionally and renamed to unambiguous names."""
    rows = read_xls_sheet(content, COMPANY_SHEET)
    if len(rows) < 2:
        raise ParseError(f"sheet {COMPANY_SHEET!r} has no data rows")
    header = [str(h).strip() if h is not None else "" for h in rows[0]]
    expected = ["Company ID", "Company Name"]
    if header[: len(expected)] != expected:
        raise ParseError(f"unexpected header {header[:4]}; expected {expected}")
    for label in ("D Code", "Facility Name", "Facility Type"):
        if label not in header:
            raise ParseError(f"sheet {COMPANY_SHEET!r} has no {label!r} column")
    names: list[str] = []
    seen: dict[str, int] = {}
    for index, label in enumerate(header):
        key = label or f"column_{index}"
        if key in seen:
            key = f"{key}__{index}"
        seen[key] = index
        names.append(key)
    frame = pd.DataFrame(rows[1:], columns=names)
    # The facility address block repeats the company block's labels immediately after the
    # facility-name column (`Address 1`, `Address 2`, `City`, `State`, `Postal Code`), so its city
    # and state are taken by position and renamed.
    facility_at = names.index("Facility Name")
    rename: dict[str, str] = {}
    for offset, label in ((3, "facility_city"), (4, "facility_state")):
        if facility_at + offset < len(names):
            rename[names[facility_at + offset]] = label
    return frame.rename(columns=rename)


def d_codes(value: Any) -> list[str]:
    """`"D6, D3"` -> `["D3", "D6"]`; anything without a D code -> `[]`."""
    text = fuels.clean_str(value)
    if text is None:
        return []
    return sorted({f"D{m.group(1)}" for m in D_CODE_RE.finditer(text)})


def build_rows(
    frame: pd.DataFrame,
    *,
    retrieved_at: str,
    source_url: str = REGISTRATION_URL,
    licence_id: str = "",
) -> pd.DataFrame:
    """One row per registered facility carrying at least one D code. A company that registers the
    same facility under several programs contributes one row with the union of its D codes."""
    if not len(frame):
        return pd.DataFrame(columns=COLUMNS)
    work = frame.copy()
    work["_codes"] = work["D Code"].map(d_codes)
    work = work[work["_codes"].map(bool)]

    grouped: dict[str, dict[str, Any]] = {}
    for _, row in work.iterrows():
        company_id = fuels.clean_str(row.get("Company ID")) or ""
        facility = fuels.clean_str(row.get("Facility Name")) or ""
        state = (fuels.clean_str(row.get("facility_state")) or "").upper()
        key = fuels.content_key(company_id, facility, state)
        bucket = grouped.setdefault(
            key,
            {
                "facility_key": key,
                "company_id": company_id or None,
                "company_name": fuels.clean_str(row.get("Company Name")),
                "facility_name": facility or None,
                "facility_city": fuels.clean_str(row.get("facility_city")),
                "facility_state": fuels.state_code(state),
                "facility_type": fuels.clean_str(row.get("Facility Type")),
                "facility_activities": fuels.clean_str(row.get("Facility Activities")),
                "program_types": fuels.clean_str(row.get("Program Types")),
                "fuel_created": fuels.clean_str(row.get("Fuel Created")),
                "codes": set(),
                "registration_rows": 0,
            },
        )
        bucket["codes"].update(row["_codes"])
        bucket["registration_rows"] = int(bucket["registration_rows"]) + 1

    rows: list[dict[str, Any]] = []
    for bucket in grouped.values():
        codes = sorted(bucket.pop("codes"))
        rows.append(
            {
                "source_id": SOURCE_ID,
                "source_url": source_url,
                "retrieved_at": retrieved_at,
                "licence": fuels.LICENCE_CLASS,
                "licence_id": licence_id,
                **bucket,
                "d_codes": codes,
                "pathway_count": len(codes),
                "first_registered_year": None,
                "feature_flags": [FIRST_REGISTERED_UNAVAILABLE],
            }
        )
    out = pd.DataFrame(rows, columns=COLUMNS)
    return out.sort_values(["company_name", "facility_name"], na_position="last").reset_index(drop=True)


def default_output() -> pathlib.Path:
    return fuels.CONTEXT_DIR / f"{SOURCE_ID}.parquet"


# ----------------------------------------------------------------------------------------- run
def run(
    *,
    snapshot: pathlib.Path | None = None,
    out: pathlib.Path | None = None,
    store: Store | None = None,
    manifest: pathlib.Path | None = None,
) -> dict[str, Any]:
    t0 = time.monotonic()
    entry = fuels.entry_for(SOURCE_ID, manifest)
    store = store or Store()
    if snapshot is not None:
        content = snapshot.read_bytes()
        retrieved_at, source_url = fuels.snapshot_metadata(snapshot, SOURCE_ID, REGISTRATION_URL, store)
        meta: dict[str, Any] = {"snapshot": str(snapshot)}
    else:
        fetched = fetch(store=store)
        content, retrieved_at, source_url = fetched.content, fetched.retrieved_at, fetched.source_url
        meta = {
            "fetched_url": fetched.source_url,
            "bytes": fetched.bytes_,
            "sha256": fetched.sha256,
            "last_modified": fetched.last_modified,
        }
    frame = parse(content)
    features = build_rows(
        frame, retrieved_at=retrieved_at, source_url=source_url, licence_id=entry.licence_id
    )
    out = out or default_output()
    fuels.write_context_parquet(features, out)
    by_code: dict[str, int] = {}
    for codes in features["d_codes"]:
        for code in codes:
            by_code[code] = by_code.get(code, 0) + 1
    return {
        "source_id": SOURCE_ID,
        "registration_rows": len(frame),
        "facilities_with_d_codes": len(features),
        "by_d_code": dict(sorted(by_code.items())),
        "wave_d_codes": {c: by_code.get(c, 0) for c in WAVE_D_CODES},
        "retrieved_at": retrieved_at,
        "out": str(out),
        "parquet_bytes": out.stat().st_size,
        "elapsed_s": round(time.monotonic() - t0, 2),
        **meta,
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--snapshot", type=pathlib.Path, help="Parse this recorded .xls")
    parser.add_argument("--out", type=pathlib.Path)
    args = parser.parse_args(argv)
    summary = run(snapshot=args.snapshot, out=args.out)
    print(json.dumps(summary, default=json_default))  # noqa: T201 — CLI summary line


if __name__ == "__main__":
    main()
