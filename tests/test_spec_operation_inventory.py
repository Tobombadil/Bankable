"""Pins how the `api-contract` CI step enumerates operations on both sides of the comparison.

That step reports how far `services.api.app` is from `api/openapi.yaml`. Two enumeration traps
made its first version print authoritative-looking wrong numbers, and both are pinned here:

1. **The app side must not be enumerated from `app.routes`.** On this FastAPI version
   `include_router` leaves a single `_IncludedRouter` entry in `app.routes` rather than flattening
   the router's routes into it, so a fresh import shows 25 `APIRoute`s while the app really serves
   about 125 operations. `GET /v1/assets` is served and absent from `app.routes`;
   `app.openapi()` walks the included routers and lists it. The step uses `app.openapi()`, and
   `test_app_routes_under_reports_what_is_served` fails if anyone "simplifies" it back.
2. **The spec side has two groups.** `api/check_story_coverage.py` counts `paths` *and*
   `webhooks` (145 operations); the step compares only `paths` (136) against the app, because a
   webhook is an outbound callback, not an endpoint the app serves. The two numbers are the same
   enumeration over different groups, not a disagreement, and
   `test_spec_enumeration_matches_the_story_coverage_checker` keeps them that way.

The ground truth both pins rest on is a live request: one documented operation that is served
(`GET /v1/assets`), one documented operation that is not (`GET /v1/bulk/proposals`).
"""

from __future__ import annotations

import importlib.util
import pathlib
import subprocess
import sys
import types

import pytest
import yaml
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from services.api.app import app

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = REPO_ROOT / "api" / "openapi.yaml"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
HTTP_METHODS = {"get", "put", "post", "delete", "options", "head", "patch", "trace"}

#: Documented under `paths:` and served (a live request below proves it).
SERVED = ("GET", "/v1/assets")
#: Documented under `paths:` and not implemented (a live request below proves that too).
UNIMPLEMENTED = ("GET", "/v1/bulk/proposals")


@pytest.fixture(scope="module")
def spec() -> dict:
    return yaml.safe_load(SPEC.read_text())


def _spec_ops(doc: dict, group: str) -> set[tuple[str, str]]:
    return {
        (method.upper(), path)
        for path, item in (doc.get(group) or {}).items()
        for method in item
        if method in HTTP_METHODS
    }


def _app_ops() -> set[tuple[str, str]]:
    return {
        (method.upper(), path)
        for path, item in app.openapi()["paths"].items()
        for method in item
        if method in HTTP_METHODS
    }


def _story_coverage_module() -> types.ModuleType:
    """`api/` is not a package, so the checker is loaded by path."""
    spec_ = importlib.util.spec_from_file_location(
        "check_story_coverage_under_test", REPO_ROOT / "api" / "check_story_coverage.py"
    )
    assert spec_ is not None and spec_.loader is not None
    module = importlib.util.module_from_spec(spec_)
    spec_.loader.exec_module(module)
    return module


def _ci_step_script() -> str:
    """The comparison step's Python body, taken from the workflow itself so this test pins the
    script CI actually runs rather than a copy of it."""
    workflow = yaml.safe_load(WORKFLOW.read_text())
    steps = workflow["jobs"]["api-contract"]["steps"]
    run = next(s["run"] for s in steps if "generated spec" in (s.get("name") or ""))
    body = run.split("<<'PY'\n", 1)[1]
    return body.rsplit("\nPY", 1)[0]


def test_the_two_pinned_operations_are_both_documented_under_paths(spec: dict) -> None:
    documented = _spec_ops(spec, "paths")
    assert SERVED in documented
    assert UNIMPLEMENTED in documented


def test_app_openapi_lists_exactly_what_a_live_request_proves(client: TestClient, spec: dict) -> None:
    """The enumeration is only trustworthy if it agrees with the running app, so both directions
    are checked against a real response rather than against another listing."""
    app_ops = _app_ops()

    assert SERVED in app_ops
    assert client.request(*SERVED).status_code == 200

    assert UNIMPLEMENTED not in app_ops
    assert client.request(*UNIMPLEMENTED).status_code == 404


def test_app_routes_under_reports_what_is_served() -> None:
    """Trap 1, pinned as a fact about this FastAPI version: if this ever starts failing,
    `app.routes` has become a usable inventory and the CI step's comment should be revisited —
    until then, enumerating from it would silently lose ~100 served operations."""
    flat = {
        (method.upper(), route.path)
        for route in app.routes
        if isinstance(route, APIRoute)
        for method in route.methods
    }
    assert SERVED not in flat
    assert len(flat) < len(_app_ops())


def test_spec_enumeration_matches_the_story_coverage_checker(spec: dict) -> None:
    """Trap 2: the step and `api/check_story_coverage.py` must enumerate the same operations, so
    their totals can be reconciled (checker = step's `paths` + `webhooks`) instead of read as a
    disagreement."""
    checker = _story_coverage_module()
    assert checker.HTTP_METHODS == HTTP_METHODS
    from_checker = {(method, path) for method, path, _op in checker.operations(spec)}
    assert from_checker == _spec_ops(spec, "paths") | _spec_ops(spec, "webhooks")


def test_ci_step_classifies_both_pinned_operations_correctly() -> None:
    """End to end: run the workflow's own script and read its verdict. `bulkProposals` must be
    reported as missing under the `bulk` area, and nothing about `/v1/assets` may appear — it is
    served and documented, so it belongs in no problem list."""
    result = subprocess.run(  # noqa: S603 - the "untrusted input" is this repo's own workflow file
        [sys.executable, "-c", _ci_step_script()],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    out = result.stdout
    listed = [line for line in out.splitlines() if "bulkProposals" in line]
    assert listed, out
    assert any(line.strip().endswith(f"{UNIMPLEMENTED[0]} {UNIMPLEMENTED[1]}") for line in listed), listed
    assert any(line.startswith("  bulk (") and "missing)" in line for line in out.splitlines()), out
    assert SERVED[1] not in out, "a served, documented operation must not be reported as a gap"
    assert "UNDOCUMENTED (drift — must be 0): 0" in out
    assert result.returncode == 1, "report-only, but non-zero while operations are still missing"
