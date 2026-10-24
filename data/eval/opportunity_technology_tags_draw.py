"""The held-out draw for the opportunity tag evaluation (docs/28 lane F3; review of 2026-10-24,
finding 6), seeded and reproducible from a pulled register.

    python data/eval/opportunity_technology_tags_draw.py --snapshot DIR --out FILE.csv

Every notice already in `opportunity_technology_tags.csv` (the first sample, which the rules were
then fitted to) is left out, so the draw holds only notices no rule was written against. The
frames are rule-independent: they read the served title, CPV codes and World Bank sector codes
and the tags the register stored, never the tags this lane's rules give, so the draw is the same
whatever the code says:

- technology questions: a notice is in the frame of `solar_pv`, `nuclear`, `coal`, `biomass` or
  `transmission` when its stored tags carry the token or `SCREEN` (title words, CPV prefixes,
  sector codes) finds it; up to `--per-tag` are drawn from each frame;
- `work_types`: notices whose status is `open` or `announced` (the RFP page's statuses, whatever
  their relevance); `--work` are drawn;
- `all_source` is not drawn: the register's 16 utility RFP notices, the only notices that can
  carry it, are all in the first sample.

`random.Random(seed)` draws from each frame sorted by `public_id`, in the order above, so one seed
gives one sample. The rows come out with `label` empty and `strata` `heldout`; they are judged
from the title, CPV codes, contract nature and World Bank reference alone (`--judging-sheet`
writes exactly those columns) before any rule is run on them, and the labels are then added to
the CSV. `DIR` is the pull `opportunity_technology_tags_measure.py` describes (2026-10-23).
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import re
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
LABELLED = HERE / "opportunity_technology_tags.csv"
SEED = 20261025
TECH_QUESTIONS = ("solar_pv", "nuclear", "coal", "biomass", "transmission")
COLUMNS = (
    "question",
    "sheet_row",
    "strata",
    "public_id",
    "source_id",
    "kind",
    "status",
    "title",
    "cpv",
    "contract_nature",
    "sector_codes",
    "bid_reference_no",
    "stored_technologies",
    "label",
    "note",
    "source_url",
    "retrieved_at",
    "licence_id",
    "reuse_class",
)
JUDGING_COLUMNS = (
    "question",
    "sheet_row",
    "title",
    "cpv",
    "contract_nature",
    "sector_codes",
    "bid_reference_no",
)

#: Title words, CPV prefixes and World Bank sector codes that put a notice in a technology's frame
#: (the screen of the first sample's `pool` stratum, unchanged).
SCREEN: dict[str, tuple[re.Pattern[str], tuple[str, ...], tuple[str, ...]]] = {
    "solar_pv": (
        re.compile(
            r"solar|photovolta|fotowolta|fotovolta|\bpv\b|\bfve\b|solaire|solare|napelem|saules|ηλιακ|φωτοβολτα|слънчев",
            re.I,
        ),
        ("0933", "31712331", "45261215"),
        ("LU",),
    ),
    "nuclear": (
        re.compile(r"nucl|jądrow|jadern|kernkraft|atom|ядрен|uran|smr\b|reactor|reaktor", re.I),
        ("4525111", "0934", "4215"),
        (),
    ),
    "coal": (
        re.compile(
            r"coal|kohle|w[ęe]gl|charbon|carb[oó]n|uhl[íie]|lignit|c[ăa]rbune|въглищ|solid fuel|twarde"
            r"|tuhá paliva|твърди горива",
            re.I,
        ),
        ("0911",),
        (),
    ),
    "biomass": (
        re.compile(
            r"biomas|biogas|biogaz|biometh|pellet|wood|holz|drewn|zrębk|štěpk|bois|madera|legna|malk|kokskaid|granul"
            r"|anaerob|digest",
            re.I,
        ),
        ("091114", "03413", "45251142", "45251240"),
        ("LB",),
    ),
    "transmission": (
        re.compile(
            r"transmission|substation|\bgrid|kv\b|voltage|transformat|switchgear|umspann|netz|sieci|électrique|"
            r"elektroenerget|trafo|napięci|napětí|tension|tensión|tensione|spannung|power line|overhead line|"
            r"rozdziel|rozvod|schaltanlage|disjonct|subestac|sottostaz|подстанц|трансформ",
            re.I,
        ),
        ("3117", "31682540", "3121", "312132", "312134", "31321", "452314", "452322", "31200000"),
        ("LT",),
    ),
}


def load_register(snapshot: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted(snapshot.glob("opportunities-p*.json")):
        doc = json.loads(path.read_text(encoding="utf-8"))
        rows += doc["data"] if isinstance(doc, dict) else doc
    return rows


def _codes(row: dict[str, Any], key: str) -> list[str]:
    return [str(c) for c in ((row.get("identifiers") or {}).get(key) or [])]


def in_frame(question: str, row: dict[str, Any]) -> bool:
    if question in (row.get("technologies") or []):
        return True
    words, cpv, sectors = SCREEN[question]
    return (
        bool(words.search(str(row.get("title") or "")))
        or any(c.startswith(cpv) for c in _codes(row, "cpv"))
        or any(s in sectors for s in _codes(row, "sector_codes"))
    )


def as_row(question: str, n: int, row: dict[str, Any]) -> dict[str, str]:
    ids = row.get("identifiers") or {}
    prov = (row.get("provenance") or [{}])[0]
    return {
        "question": question,
        "sheet_row": f"h{n}",
        "strata": "heldout",
        "public_id": row["public_id"],
        "source_id": str(prov.get("source_id") or ""),
        "kind": str(row.get("kind") or ""),
        "status": str(row.get("status") or ""),
        # whitespace runs (line breaks inside a World Bank title) as one space
        "title": " ".join(str(row.get("title") or "").split()),
        "cpv": "|".join(_codes(row, "cpv")[:9]),
        "contract_nature": "|".join(str(v) for v in ids.get("contract_nature") or []),
        "sector_codes": "|".join(_codes(row, "sector_codes")),
        "bid_reference_no": str(ids.get("bid_reference_no") or ""),
        "stored_technologies": "|".join(row.get("technologies") or []),
        "label": "",
        "note": "",
        "source_url": str(prov.get("source_url") or ""),
        "retrieved_at": str(prov.get("retrieved_at") or ""),
        "licence_id": str(prov.get("licence_id") or ""),
        "reuse_class": str(prov.get("reuse_class") or ""),
    }


def draw(
    register: list[dict[str, Any]], labelled_ids: set[str], *, seed: int, per_tag: int, work: int
) -> list[dict[str, str]]:
    rng = random.Random(seed)  # noqa: S311 - a seeded, reproducible draw, not a secret
    pool = sorted((r for r in register if r["public_id"] not in labelled_ids), key=lambda r: r["public_id"])
    out: list[dict[str, str]] = []
    for question in TECH_QUESTIONS:
        frame = [r for r in pool if in_frame(question, r)]
        picked = rng.sample(frame, min(per_tag, len(frame)))
        out += [
            as_row(question, i + 1, r) for i, r in enumerate(sorted(picked, key=lambda r: r["public_id"]))
        ]
    frame = [r for r in pool if r.get("status") in ("open", "announced")]
    picked = rng.sample(frame, min(work, len(frame)))
    out += [
        as_row("work_types", i + 1, r) for i, r in enumerate(sorted(picked, key=lambda r: r["public_id"]))
    ]
    return out


def frame_sizes(register: list[dict[str, Any]], labelled_ids: set[str]) -> dict[str, int]:
    pool = [r for r in register if r["public_id"] not in labelled_ids]
    sizes = {q: sum(1 for r in pool if in_frame(q, r)) for q in TECH_QUESTIONS}
    sizes["work_types"] = sum(1 for r in pool if r.get("status") in ("open", "announced"))
    return sizes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True, help="the drawn rows, label empty")
    parser.add_argument("--judging-sheet", type=Path, help="the same rows, judging columns only")
    parser.add_argument("--seed", type=int, default=SEED)
    parser.add_argument("--per-tag", type=int, default=60)
    parser.add_argument("--work", type=int, default=100)
    args = parser.parse_args(argv)
    register = load_register(args.snapshot)
    with LABELLED.open(encoding="utf-8", newline="") as f:
        labelled_ids = {r["public_id"] for r in csv.DictReader(f) if r["strata"] != "heldout"}
    rows = draw(register, labelled_ids, seed=args.seed, per_tag=args.per_tag, work=args.work)
    with args.out.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    if args.judging_sheet:
        with args.judging_sheet.open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=JUDGING_COLUMNS, extrasaction="ignore", lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)
    sizes = frame_sizes(register, labelled_ids)
    print(json.dumps({"seed": args.seed, "frames": sizes, "drawn": len(rows)}, indent=1))  # noqa: T201
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
