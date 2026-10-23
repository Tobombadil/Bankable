"""`python -m services.site_finder [--states TX] [--data-root DIR] [--no-prospect]`: one run of the site
finder's candidate computation against `DATABASE_URL` (`services/site_finder/compute.py`)."""

from __future__ import annotations

import argparse
import json
import pathlib

from services.db.session import get_engine, get_sessionmaker
from services.site_finder.compute import refresh


def main(argv: list[str] | None = None) -> None:
    from pipeline.connectors.store import DATA_DIR

    parser = argparse.ArgumentParser(prog="python -m services.site_finder", description=__doc__)
    parser.add_argument("--states", default="", help="postal codes, comma-separated; default every line")
    parser.add_argument("--data-root", type=pathlib.Path, default=DATA_DIR)
    parser.add_argument("--no-prospect", action="store_true", help="skip the prospecting layers")
    args = parser.parse_args(argv)
    states = [s.strip() for s in args.states.split(",") if s.strip()] or None
    factory = get_sessionmaker(get_engine())
    report = refresh(factory, args.data_root, states=states, use_prospect=not args.no_prospect)
    print(json.dumps(report, sort_keys=True, default=str))  # noqa: T201 - CLI summary line


if __name__ == "__main__":
    main()
