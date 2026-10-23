"""`python -m services.site_finder [--states TX] [--data-root DIR] [--no-prospect] [--no-parcels]`: one run
of the site finder's candidate computation against `DATABASE_URL` (`services/site_finder/compute.py`).
`--parcels-only [--counties 48351,48241]` re-measures the served run's land parcels in place after a
parcels pull (`python -m pipeline.context.txgio_parcels pull`)."""

from __future__ import annotations

import argparse
import json
import pathlib

from services.db.session import get_engine, get_sessionmaker
from services.site_finder.compute import refresh, refresh_parcels


def main(argv: list[str] | None = None) -> None:
    from pipeline.connectors.store import DATA_DIR

    parser = argparse.ArgumentParser(prog="python -m services.site_finder", description=__doc__)
    parser.add_argument("--states", default="", help="postal codes, comma-separated; default every line")
    parser.add_argument("--data-root", type=pathlib.Path, default=DATA_DIR)
    parser.add_argument("--no-prospect", action="store_true", help="skip the prospecting layers")
    parser.add_argument("--no-parcels", action="store_true", help="skip the land parcels")
    parser.add_argument(
        "--parcels-only",
        action="store_true",
        help="re-measure the served run's parcels in place (after a parcels pull); nothing else changes",
    )
    parser.add_argument(
        "--counties", default="", help="with --parcels-only: county FIPS codes, comma-separated"
    )
    args = parser.parse_args(argv)
    states = [s.strip() for s in args.states.split(",") if s.strip()] or None
    factory = get_sessionmaker(get_engine())
    if args.parcels_only:
        counties = [c.strip() for c in args.counties.split(",") if c.strip()] or None
        print(json.dumps(refresh_parcels(factory, args.data_root, counties=counties), sort_keys=True))  # noqa: T201
        return
    report = refresh(
        factory,
        args.data_root,
        states=states,
        use_prospect=not args.no_prospect,
        use_parcels=not args.no_parcels,
    )
    print(json.dumps(report, sort_keys=True, default=str))  # noqa: T201 - CLI summary line


if __name__ == "__main__":
    main()
