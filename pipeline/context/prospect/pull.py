"""The server's copy of the published prospect build: `python -m pipeline.context.prospect.pull`.

A thin pull over `services.context_sample.sync` (which downloads a build, checks every sha256 and swaps
`layers.json` last), run by the server's monthly `context_build` (`infra/scheduler/jobs.py`
`CONTEXT_BUILDERS`) so the site finder's sampler reads the current build (docs/25 "Prospecting layers"
§5.3; open item 7 there, closed by this module). It adds the two things the scheduled pull needs:

- **Which build.** The private bucket holds one pointer per build region
  (`prospect/current/<region>.json`, `.github/workflows/prospect-layers.yml`: `texas` or `conus`). The
  server's states are `GRID_LAYERS_REGIONS`, the variable the OpenStreetMap and flood pulls read (ISO
  codes such as `US-TX`, or `all`; unset is refused, `pipeline.context.osm_power.default_regions`). A
  server that loads Texas alone reads the `texas` build and nothing else; every other setting reads
  `conus` (`regions_for`). A Texas server therefore follows `prospect/current/texas.json`, which only a
  dispatch with region `texas` moves (the monthly scheduled run builds `conus`). It never falls back
  to `conus`: the national build has not been run or measured yet (docs/25 "Prospecting layers" item
  9) and would land on the beta's disk and in the sampler during a site-finder run that already peaks
  at 1.2 GB (docs/25 §7), so a missing `texas.json` is "not published", naming the dispatch to run.
- **Not published yet.** When no candidate pointer exists the answer is `status: not_published`
  (exit 0) and the local directory is left as it is, so the other context builders run on and a
  sampler keeps serving what it had.

Environment: the R2 names the beta's env file carries (`R2_BUCKET`, `R2_ACCESS_KEY_ID`,
`R2_SECRET_ACCESS_KEY`, `R2_ACCOUNT_ID` or `R2_ENDPOINT`; `services.context_sample.sync.client_from_env`)
and `PROSPECT_DATA_DIR` (default `<data root>/normalized/prospect`).
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from collections.abc import Sequence
from typing import Any

#: The summary `status` when no build the server could read is published; `infra/scheduler/jobs.py`
#: (`NOT_PUBLISHED`) reports it as "not published", never as a failed builder.
NOT_PUBLISHED = "not_published"
POINTER_KEY = "prospect/current/{region}.json"
#: The build regions `pipeline.context.prospect.build --region` and prospect-layers.yml offer.
TEXAS, CONUS = "texas", "conus"


def regions_for(states: Sequence[str] | None) -> list[str]:
    """The prospect builds that cover `states` (ISO codes; None = every state), in the order tried:
    Texas alone reads the small `texas` build only (never `conus`, module docstring); anything else
    reads `conus`."""
    if states is not None and sorted({s.strip().upper() for s in states}) == ["US-TX"]:
        return [TEXAS]
    return [CONUS]


#: What a "not published" answer tells the operator to run (`.github/workflows/prospect-layers.yml`).
DISPATCH_HINT = "run prospect-layers.yml with region {region} (docs/66 'Site finder go-live')"


def _exists(client: Any, bucket: str, key: str) -> bool:
    from pipeline.connectors.objectstore import S3Backend

    return S3Backend(bucket, prefix="", client=client).exists(key)


def pull(dest: pathlib.Path, *, states: Sequence[str] | None, client: Any, bucket: str) -> dict[str, Any]:
    """Sync `dest` to the first build of `regions_for(states)` whose pointer exists."""
    from services.context_sample.sync import sync

    tried: list[str] = []
    for region in regions_for(states):
        key = POINTER_KEY.format(region=region)
        tried.append(key)
        if _exists(client, bucket, key):
            return {"region": region, **sync(dest, region=region, client=client, bucket=bucket)}
    return {
        "status": NOT_PUBLISHED,
        "pointers": tried,
        "to_publish": DISPATCH_HINT.format(region=" or ".join(regions_for(states))),
    }


def main(argv: list[str] | None = None) -> int:
    from pipeline.connectors.objectstore import StoreError
    from pipeline.context.osm_power import BuildError, default_regions, parse_regions
    from services.context_sample.paths import prospect_dir
    from services.context_sample.sync import SyncError, client_from_env

    parser = argparse.ArgumentParser(
        prog="python -m pipeline.context.prospect.pull", description=__doc__.split("\n\n")[0]
    )
    parser.add_argument(
        "--regions", help="ISO codes (US-TX,US-OK) or 'all'. Default: $GRID_LAYERS_REGIONS, which must be set"
    )
    parser.add_argument(
        "--dest", type=pathlib.Path, help="default: PROSPECT_DATA_DIR or <data>/normalized/prospect"
    )
    args = parser.parse_args(argv)
    try:
        states = parse_regions(args.regions) if args.regions else default_regions()
    except BuildError as exc:
        raise SystemExit(f"prospect pull: {exc}") from exc
    try:
        client, bucket = client_from_env()
        result = pull(args.dest or prospect_dir(), states=states, client=client, bucket=bucket)
    except (SyncError, StoreError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}), file=sys.stderr)  # noqa: T201
        return 1
    print(json.dumps(result))  # noqa: T201 - CLI summary line
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
