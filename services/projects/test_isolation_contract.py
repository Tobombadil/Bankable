"""The private-projects import contracts (docs/19 §5.2 item 1; `infra/importlinter.ini`) catch what
they say they catch.

Each case builds a scratch package tree with the module names the contracts use, copies the three
`private-projects-*` contracts out of the real config unchanged, and runs import-linter on it in a
subprocess (so the scratch `services` package never meets this process's). A case names the
contracts it must break; every other contract must stay kept, so the composition-root wiring the
site depends on stays allowed.

import-linter is installed by CI's import-linter job, not by `requirements.txt`; the behavioural
cases skip without it. The configuration checks below run everywhere.
"""

from __future__ import annotations

import configparser
import json
import os
import pathlib
import subprocess
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
CONFIG = REPO_ROOT / "infra" / "importlinter.ini"
ISOLATED = "private-projects-isolated"
NAMED_IMPORTERS = "private-projects-named-importers"
IMPORTERS_NAMED = "private-projects-importers-named"
CONTRACTS = (ISOLATED, NAMED_IMPORTERS, IMPORTERS_NAMED)

#: docs/19 §5.2 item 1, as written: the public paths that must never reach private code.
SPEC_PUBLIC_PATHS = frozenset(
    {
        "services.api.visibility",
        "services.api.listing",
        "services.api.geo",
        "services.api.feeds",
        "services.api.records",
        "services.api.matches",
        "services.api.serialize",
        "services.alerts",
        "services.social",
        "services.match",
        "services.crm",
        "web",  # web/sitemaps.py and every page: the site reads through the API only
    }
)
#: The only modules docs/19 §5.2 lets import `services.projects`.
SPEC_PRIVATE_READERS = frozenset(
    {"services.api.my_projects", "services.api.account_erasure", "services.retention"}
)

#: The wiring as it will stand once lane P2 lands: the readers import the private package, the app
#: mounts the private router, the erasure routes call erasure, and the site mounts the app.
WIRING: dict[str, tuple[str, ...]] = {
    "services.api.my_projects": ("services.projects",),
    "services.api.test_my_projects": ("services.api.my_projects", "services.projects"),
    "services.api.account_erasure": ("services.projects",),
    "services.retention.run": ("services.projects",),
    "services.api.auth_routes": ("services.api.account_erasure",),
    "services.api.admin_people": ("services.api.account_erasure",),
    "services.api.app": ("services.api.my_projects", "services.api.auth_routes", "services.api.admin_people"),
    "web.api_client": ("services.api.app",),
    "web.pages": ("web.api_client",),
    "services.api.feeds": ("services.api.serialize",),
}

RUNNER = """
import contextlib, io, json, sys
from importlinter.cli import lint_imports
results = {}
for contract in sys.argv[2:]:
    with contextlib.redirect_stdout(io.StringIO()):
        results[contract] = lint_imports(
            config_filename=sys.argv[1], limit_to_contracts=(contract,), no_cache=True, no_logo=True
        )
print(json.dumps(results))
"""


def _config() -> configparser.ConfigParser:
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(CONFIG, encoding="utf-8")
    return parser


def _values(contract: str, key: str) -> list[str]:
    section = _config()[f"importlinter:contract:{contract}"]
    return [line.strip() for line in section.get(key, "").splitlines() if line.strip()]


def _named_modules() -> set[str]:
    names: set[str] = set()
    for contract in CONTRACTS:
        for key in ("source_modules", "forbidden_modules", "protected_modules", "allowed_importers"):
            names.update(name for name in _values(contract, key) if "*" not in name)
    return names


def _build_tree(root: pathlib.Path, imports: dict[str, tuple[str, ...]]) -> None:
    """Every module as a package (`<dotted path>/__init__.py`) holding its `import` lines."""
    modules = _named_modules() | set(imports) | {target for targets in imports.values() for target in targets}
    for module in sorted(modules):
        parts = module.split(".")
        for depth in range(1, len(parts) + 1):
            package = root.joinpath(*parts[:depth])
            package.mkdir(parents=True, exist_ok=True)
            (package / "__init__.py").touch()
        lines = [f"import {target}\n" for target in imports.get(module, ())]
        (root.joinpath(*parts) / "__init__.py").write_text("".join(lines), encoding="utf-8")


def _scratch_config(path: pathlib.Path) -> None:
    real = _config()
    scratch = configparser.ConfigParser(interpolation=None)
    scratch["importlinter"] = {"root_packages": "\npipeline\nservices\nweb"}
    for contract in CONTRACTS:
        section = f"importlinter:contract:{contract}"
        scratch[section] = dict(real[section])
    with path.open("w", encoding="utf-8") as handle:
        scratch.write(handle)


def _broken(tmp_path: pathlib.Path, extra: dict[str, tuple[str, ...]]) -> set[str]:
    imports = {module: WIRING.get(module, ()) + extra.get(module, ()) for module in {*WIRING, *extra}}
    root = tmp_path / "tree"
    _build_tree(root, imports)
    config = tmp_path / "contracts.ini"
    _scratch_config(config)
    env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    result = subprocess.run(  # noqa: S603 -- this interpreter, a fixed script, paths from tmp_path
        [sys.executable, "-c", RUNNER, str(config), *CONTRACTS],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    outcome: dict[str, int] = json.loads(result.stdout.strip().splitlines()[-1])
    assert set(outcome) == set(CONTRACTS)
    return {contract for contract, code in outcome.items() if code != 0}


CASES: dict[str, tuple[dict[str, tuple[str, ...]], set[str]]] = {
    "the wiring as it stands is allowed": ({}, set()),
    # The reviewer's reproductions: a public path reaching private code through a named reader.
    "a feed imports my_projects": ({"services.api.feeds": ("services.api.my_projects",)}, {ISOLATED}),
    "alerts import account_erasure": (
        {"services.alerts": ("services.api.account_erasure",)},
        {ISOLATED, IMPORTERS_NAMED},
    ),
    "the record routes import retention": (
        {"services.api.records": ("services.retention.run",)},
        {ISOLATED, IMPORTERS_NAMED},
    ),
    "a page imports my_projects": ({"web.pages": ("services.api.my_projects",)}, {ISOLATED}),
    # Direct and helper routes into the private package itself.
    "the record routes import the private package": (
        {"services.api.records": ("services.projects",)},
        {ISOLATED, NAMED_IMPORTERS},
    ),
    "a helper the map imports reads the private package": (
        {
            "services.api.geo": ("services.api.some_helper",),
            "services.api.some_helper": ("services.projects",),
        },
        {NAMED_IMPORTERS},
    ),
    "a helper the map imports re-exports erasure": (
        {
            "services.api.geo": ("services.api.some_helper",),
            "services.api.some_helper": ("services.api.account_erasure",),
        },
        {IMPORTERS_NAMED},
    ),
}


@pytest.mark.parametrize("case", sorted(CASES))
def test_each_contract_catches_the_imports_it_names(tmp_path: pathlib.Path, case: str) -> None:
    pytest.importorskip("importlinter")
    extra, expected = CASES[case]
    assert _broken(tmp_path, extra) == expected


def test_the_isolated_contract_names_every_public_path_and_every_private_reader() -> None:
    sources = set(_values(ISOLATED, "source_modules"))
    forbidden = set(_values(ISOLATED, "forbidden_modules"))
    assert SPEC_PUBLIC_PATHS <= sources
    assert {"services.projects", *SPEC_PRIVATE_READERS} <= forbidden
    assert _config()[f"importlinter:contract:{ISOLATED}"].get("allow_indirect_imports") == "True"
    allowed = set(_values(NAMED_IMPORTERS, "allowed_importers"))
    assert not allowed & sources, "a public path may never be an allowed importer"


def test_my_projects_is_protected_once_it_exists() -> None:
    """A protected module must exist when import-linter runs, so `services.api.my_projects` can
    join the third contract only in the merge that adds it. This fails until it does."""
    exists = (REPO_ROOT / "services" / "api" / "my_projects.py").exists() or (
        REPO_ROOT / "services" / "api" / "my_projects"
    ).is_dir()
    protected = set(_values(IMPORTERS_NAMED, "protected_modules"))
    assert ("services.api.my_projects" in protected) is exists
    assert {"services.api.account_erasure", "services.retention"} <= protected
