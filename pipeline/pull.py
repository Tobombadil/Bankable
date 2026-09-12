#!/usr/bin/env python3
"""Pull the five reachable ISO queues (gridstatus) and the EIA-860M "Planned" sheet into
data/eval/raw/<source>.<date>.parquet, plus a small JSON manifest.

Usage:
    .venv/bin/python pipeline/pull.py [--date YYYY-MM-DD] [--only caiso,ercot,...]

PJM (needs API key) and MISO (Cloudflare 403) are deliberately skipped; see docs/02 §2.
"""
from __future__ import annotations

import argparse
import datetime as dt
import io
import json
import pathlib
import re
import sys
import time

import pandas as pd
import requests

ROOT = pathlib.Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "eval" / "raw"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/128.0 Safari/537.36 BankableProbe/0.1 (+https://www.bankablehq.com)")
ISO_CLASSES = {"caiso": "CAISO", "ercot": "Ercot", "spp": "SPP", "nyiso": "NYISO", "isone": "ISONE"}
EIA_INDEX = "https://www.eia.gov/electricity/data/eia860m/"
MAX_BYTES = 20 * 1024 * 1024


def pull_iso(key: str) -> pd.DataFrame:
    import gridstatus  # noqa: WPS433 (slow import)

    df = getattr(gridstatus, ISO_CLASSES[key])().get_interconnection_queue()
    df.columns = [str(c) for c in df.columns]
    return df


def find_eia860m_xlsx(session: requests.Session) -> str:
    """The index page links months as .../xls/<month>_generatorYYYY.xlsx, but it also lists
    *future* months as placeholders that 301 to an HTML page (observed 2026-09-12: december_/
    october_generator2026 redirect; july_generator2026 is the real latest). Guessed names likewise
    return HTML with a 200 (docs/02 §7). So: take links in page order and return the first whose
    first bytes are a zip ("PK")."""
    html = session.get(EIA_INDEX, timeout=30).text
    links = re.findall(r'href="([^"]+?generator\d{4}\.xlsx)"', html, flags=re.I)
    seen: list[str] = []
    for href in links:
        url = href if href.startswith("http") else requests.compat.urljoin(EIA_INDEX, href)
        if url in seen:
            continue
        seen.append(url)
        r = session.get(url, headers={"Range": "bytes=0-3"}, timeout=30, allow_redirects=True)
        if r.content[:2] == b"PK":
            return url
        if len(seen) >= 6:
            break
    raise RuntimeError(f"no real xlsx among first EIA-860M links: {seen}")


def pull_eia860m(session: requests.Session) -> tuple[pd.DataFrame, str]:
    url = find_eia860m_xlsx(session)
    r = session.get(url, timeout=120)
    r.raise_for_status()
    if not r.content[:2] == b"PK":
        raise RuntimeError(f"{url} did not return an xlsx (first bytes {r.content[:20]!r})")
    # header is on the third row (docs/02 §7 / task note) -> header=2
    df = pd.read_excel(io.BytesIO(r.content), sheet_name="Planned", header=2, engine="openpyxl")
    df.columns = [str(c).strip() for c in df.columns]
    return df, url


def write(df: pd.DataFrame, name: str, date: str) -> dict:
    path = RAW / f"{name}.{date}.parquet"
    # parquet needs homogeneous columns; cast everything object-ish to string
    out = df.copy()
    for c in out.columns:
        if out[c].dtype == object:
            out[c] = out[c].astype("string")
    out.to_parquet(path, index=False)
    sampled = False
    if path.stat().st_size > MAX_BYTES:
        out = out.sample(frac=0.5, random_state=0)
        out.to_parquet(path, index=False)
        sampled = True
    return {"path": str(path.relative_to(ROOT)), "rows": int(len(out)), "cols": list(map(str, df.columns)),
            "bytes": path.stat().st_size, "sampled": sampled}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=dt.date.today().isoformat())
    ap.add_argument("--only")
    args = ap.parse_args()
    RAW.mkdir(parents=True, exist_ok=True)
    only = set(args.only.split(",")) if args.only else None
    mpath = RAW / f"manifest.{args.date}.json"
    manifest: dict[str, dict] = json.loads(mpath.read_text()) if mpath.exists() else {}
    session = requests.Session()
    session.headers.update({"User-Agent": UA, "Accept": "*/*"})

    for key in ISO_CLASSES:
        if only and key not in only:
            continue
        t = time.time()
        try:
            df = pull_iso(key)
            manifest[key] = write(df, key, args.date) | {"ok": True, "secs": round(time.time() - t, 1),
                                                          "retrieved_at": dt.datetime.utcnow().isoformat() + "Z"}
        except Exception as e:  # noqa: BLE001
            manifest[key] = {"ok": False, "err": repr(e)[:300]}
        print(key, manifest[key].get("rows"), manifest[key].get("err", ""))
    if not only or "eia860m" in only:
        t = time.time()
        try:
            df, url = pull_eia860m(session)
            manifest["eia860m"] = write(df, "eia860m", args.date) | {
                "ok": True, "url": url, "secs": round(time.time() - t, 1),
                "retrieved_at": dt.datetime.utcnow().isoformat() + "Z"}
        except Exception as e:  # noqa: BLE001
            manifest["eia860m"] = {"ok": False, "err": repr(e)[:300]}
        print("eia860m", manifest["eia860m"].get("rows"), manifest["eia860m"].get("err", ""))
    mpath.write_text(json.dumps(manifest, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
