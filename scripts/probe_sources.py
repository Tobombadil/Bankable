#!/usr/bin/env python3
"""Probe every source in data/sources.yaml that declares a `probe` block and write a dated
results file to data/probes/<date>.json.

Usage:
    python -m venv .venv && .venv/bin/pip install pyyaml requests gridstatus
    .venv/bin/python scripts/probe_sources.py [--gridstatus]

The script is deliberately gentle: one request per source, a browser-like User-Agent, 25 s timeout,
and no retries. It records HTTP status, bytes, elapsed time and a short classification. `--gridstatus`
additionally pulls the seven ISO queues through the gridstatus library and records row counts.
"""
import argparse, datetime, json, pathlib, sys, time
import requests, yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/128.0 Safari/537.36 BankableProbe/0.1 (+https://www.bankablehq.com)")


def classify(status, body):
    if status == 200:
        return "blocked_challenge" if b"Just a moment" in body[:2000] else "ok"
    if status in (401,):
        return "needs_key"
    if status in (403,):
        return "blocked_403"
    if status == 404:
        return "not_found"
    if status == 0:
        return "no_response"
    if 500 <= status < 600:
        return "server_error"
    return f"http_{status}"


def probe(src):
    p = src.get("probe") or {}
    url = p.get("url") or src["url"]
    method = p.get("method", "GET").upper()
    headers = {"User-Agent": UA, "Accept": "*/*"}
    t = time.time()
    try:
        if method == "POST":
            headers["Content-Type"] = "application/json"
            r = requests.post(url, data=p.get("body", "{}"), headers=headers, timeout=25)
        else:
            r = requests.get(url, headers=headers, timeout=25, allow_redirects=True)
        status, body = r.status_code, r.content
    except Exception as e:  # noqa: BLE001
        status, body = 0, repr(e).encode()
    return {"id": src["id"], "url": url, "method": method, "status": status,
            "bytes": len(body), "secs": round(time.time() - t, 2),
            "result": classify(status, body)}


def probe_gridstatus():
    import gridstatus  # noqa: WPS433
    out = {}
    for name in ["CAISO", "PJM", "MISO", "Ercot", "SPP", "NYISO", "ISONE"]:
        t = time.time()
        try:
            df = getattr(gridstatus, name)().get_interconnection_queue()
            out[name] = {"ok": True, "rows": int(len(df)), "secs": round(time.time() - t, 1),
                         "status_counts": {str(k): int(v) for k, v in
                                           df["Status"].astype(str).value_counts().head(8).items()}
                         if "Status" in df else None}
        except Exception as e:  # noqa: BLE001
            out[name] = {"ok": False, "err": repr(e)[:300], "secs": round(time.time() - t, 1)}
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gridstatus", action="store_true")
    ap.add_argument("--only", help="comma-separated source ids")
    args = ap.parse_args()
    reg = yaml.safe_load((ROOT / "data" / "sources.yaml").read_text())
    only = set(args.only.split(",")) if args.only else None
    results = []
    for src in reg["sources"]:
        if "probe" not in src or (only and src["id"] not in only):
            continue
        res = probe(src)
        results.append(res)
        print(f"{res['result']:<18} {res['status']:>3} {res['bytes']:>9}B {res['secs']:>6}s  {res['id']}")
        time.sleep(1.0)
    out = {"date": datetime.date.today().isoformat(), "registry_version": str(reg.get("version")),
           "http": results}
    if args.gridstatus:
        out["gridstatus"] = probe_gridstatus()
        for k, v in out["gridstatus"].items():
            print(f"gridstatus {k:<6} {'OK' if v['ok'] else 'FAIL'} {v.get('rows', '')} {v.get('err', '')[:120]}")
    path = ROOT / "data" / "probes" / f"{out['date']}.json"
    path.write_text(json.dumps(out, indent=1))
    print("wrote", path.relative_to(ROOT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
