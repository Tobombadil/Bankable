"""CLI: `python -m pipeline.connectors.global_ember_electricity run [--trigger ...] [--run-id <uuid>]`;
`check <file.csv> [--downloaded-at <ISO time>] [--dataset yearly|monthly]` to read what a person's download
parses to before it goes in the inbox, with the sidecar to place beside it (writes nothing, asks no host);
and `fixture <in> <out> --areas World,USA,DEU --years 2023-2025` to trim a real Ember file (an API answer or
a person's CSV download) into a small test fixture.

Structured JSON on stdout, one `result` line per run, in the record connectors' shape
(`pipeline/connectors/__main__.py`), so the scheduler records it the same way
(`infra/scheduler/jobs.py::fetch_outcome`). Exit 0 for ok/unchanged/partial, 1 for failed or blocked,
2 for a refusal (the manifest row's terms or status forbid the run).
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import pathlib
import sys
import uuid

from pipeline.connectors.__main__ import _setup_logging
from pipeline.connectors.global_ember_electricity import SOURCE_ID
from pipeline.connectors.global_ember_electricity.ember import FILE_DATASETS, redact
from pipeline.connectors.global_ember_electricity.runner import (
    ENV_KEY,
    TRIGGERS,
    EmberGateRefused,
    check_file,
    run,
)
from pipeline.connectors.objectstore import StoreConfigError
from pipeline.connectors.registry import RegistrationError
from pipeline.connectors.store import DATA_DIR, open_store


def _run_id(value: str) -> str:
    try:
        return str(uuid.UUID(value))
    except ValueError as e:
        raise argparse.ArgumentTypeError(f"not a UUID: {value!r}") from e


def _years(text: str) -> tuple[int, int]:
    first, _, last = text.partition("-")
    return int(first), int(last or first)


def trim(content: bytes, *, areas: set[str], years: tuple[int, int]) -> bytes:
    """Keep the rows of `areas` (names or ISO 3 codes, any case) in `years`, from an API answer or a CSV."""
    wanted = {a.strip().lower() for a in areas if a.strip()}

    def keep(name: object, code: object, date: object) -> bool:
        year = str(date or "")[:4]
        return (
            (str(name or "").lower() in wanted or str(code or "").lower() in wanted)
            and year.isdigit()
            and years[0] <= int(year) <= years[1]
        )

    text = content.decode("utf-8-sig")
    if text.lstrip().startswith("{"):
        payload = json.loads(text)
        payload["data"] = [
            d for d in payload.get("data", []) if keep(d.get("entity"), d.get("entity_code"), d.get("date"))
        ]
        return (json.dumps(payload, indent=1, ensure_ascii=False) + "\n").encode("utf-8")
    reader = csv.DictReader(io.StringIO(text))
    fields = list(reader.fieldnames or [])
    lower = {f.lower(): f for f in fields}
    name_col = next((lower[k] for k in ("area", "country or region", "entity") if k in lower), fields[0])
    code_col = next((lower[k] for k in ("iso 3 code", "country code", "iso3 code") if k in lower), None)
    date_col = next((lower[k] for k in ("year", "date") if k in lower), fields[1])
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    for row in reader:
        if keep(row.get(name_col), row.get(code_col) if code_col else None, row.get(date_col)):
            writer.writerow(row)
    return out.getvalue().encode("utf-8")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m pipeline.connectors.global_ember_electricity")
    sub = ap.add_subparsers(dest="cmd", required=True)
    rp = sub.add_parser("run", help="one run: the API (with EMBER_API_KEY) and any new inbox file")
    rp.add_argument("--trigger", choices=TRIGGERS, default="manual")
    rp.add_argument("--run-id", type=_run_id, default=None)
    rp.add_argument("--data-dir", default=str(DATA_DIR))
    cp = sub.add_parser("check", help="read what a downloaded Ember CSV parses to, and the sidecar to place")
    cp.add_argument("source", type=pathlib.Path)
    cp.add_argument("--downloaded-at", default=None, help="when you downloaded it (ISO 8601)")
    cp.add_argument("--dataset", choices=FILE_DATASETS, default=None)
    fp = sub.add_parser("fixture", help="trim a real Ember file into a small test fixture")
    fp.add_argument("source", type=pathlib.Path)
    fp.add_argument("target", type=pathlib.Path)
    fp.add_argument("--areas", default="World,USA,CHN,DEU,IND")
    fp.add_argument("--years", type=_years, default=(2023, 2025))
    args = ap.parse_args(argv)
    log = _setup_logging()

    if args.cmd == "check":
        report = check_file(
            args.source.read_bytes(),
            name=args.source.name,
            downloaded_at=args.downloaded_at,
            dataset=args.dataset,
        )
        sys.stdout.write(json.dumps(report, indent=1, default=str) + "\n")
        return 0 if report["ok"] else 1
    if args.cmd == "fixture":
        body = trim(args.source.read_bytes(), areas=set(args.areas.split(",")), years=args.years)
        args.target.write_bytes(body)
        log.info("fixture written", extra={"target": str(args.target), "bytes": len(body)})
        return 0
    try:
        store = open_store(pathlib.Path(args.data_dir))
    except StoreConfigError as e:
        log.error("store misconfigured", extra={"error": str(e)})
        return 1
    try:
        result = run(store, trigger=args.trigger, run_id=args.run_id)
    except (EmberGateRefused, RegistrationError) as e:
        log.error("refused", extra={"source_id": SOURCE_ID, "error": str(e)})
        return 2
    except Exception as e:
        # The text of a requests error can hold the query string, so the key is removed first.
        text = redact(f"{type(e).__name__}: {e}", [os.environ.get(ENV_KEY, "")])[:500]
        log.error("crashed", extra={"source_id": SOURCE_ID, "error_class": type(e).__name__, "error": text})
        return 1
    r = result.record
    log.info(
        "result",
        extra={
            "source_id": SOURCE_ID,
            "run_id": r["id"],
            "status": r["status"],
            "trigger": r["trigger"],
            "rows_seen": r.get("rows_seen", 0),
            "rows_new": r.get("rows_new", 0),
            "rows_changed": r.get("rows_changed", 0),
            "rows_gone": r.get("rows_gone", 0),
            "requests": len(r.get("requests") or []),
            "bytes": r.get("bytes", 0),
            "dq_status": r.get("dq_status"),
            "datasets": (r.get("outputs") or {}).get("datasets") or [],
            "error": r.get("error"),
            "run_path": str(result.run_path),
            "run_key": store.key(result.run_path),
            "store": store.backend.name,
        },
    )
    return 1 if r["status"] in ("failed", "blocked") else 0


if __name__ == "__main__":
    sys.exit(main())
