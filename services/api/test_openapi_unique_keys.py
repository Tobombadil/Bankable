"""`api/openapi.yaml` holds no mapping key twice, and each `country` parameter keeps its own meaning.

PyYAML keeps the last of two equal keys without a word, so a second `components.parameters.Country`
(review of lane front-api, 2026-10-23) silently replaced the organisations' parameter: `GET
/v1/organizations` was documented with the record-jurisdiction semantics and a lower-case pattern
its exact, case-sensitive handler does not have, and every spec test still passed. Strict loaders
(js-yaml, used by Redoc, Swagger UI and Spectral) refuse such a file. This test reads the spec the
strict way.
"""

from __future__ import annotations

import pathlib
from typing import Any

import yaml

SPEC_PATH = pathlib.Path(__file__).resolve().parents[2] / "api" / "openapi.yaml"
_MERGE_TAG = "tag:yaml.org,2002:merge"


class _StrictLoader(yaml.SafeLoader):
    """`SafeLoader` that refuses a mapping naming one key twice (merge keys `<<` aside: a key a merge
    brings in may be overridden, which is what a merge is for)."""


def _unique_mapping(loader: _StrictLoader, node: yaml.MappingNode) -> dict[Any, Any]:
    seen: dict[Any, int] = {}
    for key_node, _value in node.value:
        if key_node.tag == _MERGE_TAG:
            continue
        key = loader.construct_object(key_node, deep=True)
        if key in seen:
            raise yaml.constructor.ConstructorError(
                "while reading a mapping",
                node.start_mark,
                f"duplicate key {key!r} (first at line {seen[key]})",
                key_node.start_mark,
            )
        seen[key] = key_node.start_mark.line + 1
    mapping: dict[Any, Any] = loader.construct_mapping(node, deep=True)
    return mapping


_StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _unique_mapping)


def strict_load(text: str) -> Any:
    return yaml.load(text, Loader=_StrictLoader)  # noqa: S506 -- a SafeLoader subclass


def test_the_spec_names_no_key_twice() -> None:
    spec = strict_load(SPEC_PATH.read_text())
    assert spec == yaml.safe_load(SPEC_PATH.read_text())


def test_the_strict_loader_refuses_a_duplicate() -> None:
    try:
        strict_load("a:\n  b: 1\n  b: 2\n")
    except yaml.constructor.ConstructorError as exc:
        assert "duplicate key 'b'" in str(exc)
    else:  # pragma: no cover - the assertion is the point
        raise AssertionError("a duplicate key was accepted")
    assert strict_load("base: &b {x: 1}\nchild:\n  <<: *b\n  x: 2\n")["child"] == {"x": 2}


def test_each_country_parameter_keeps_its_own_meaning() -> None:
    spec = strict_load(SPEC_PATH.read_text())
    params = spec["components"]["parameters"]
    # The organisations' filter: an exact, upper-case code (`services/api/organizations.py`).
    assert params["Country"]["schema"]["items"]["pattern"] == "^[A-Z]{2}$"
    # The records' filter: any case, read from the jurisdiction (`services/api/country.py`).
    assert params["RecordCountry"]["name"] == "country"
    assert "jurisdiction" in params["RecordCountry"]["description"]

    def refs(path: str) -> set[str]:
        return {p.get("$ref", "") for p in spec["paths"][path]["get"].get("parameters", [])}

    assert "#/components/parameters/Country" in refs("/v1/organizations")
    for path in (
        "/v1/proposals",
        "/v1/proposals/geo",
        "/v1/opportunities",
        "/v1/opportunities/geo",
        "/v1/events",
        "/v1/stats/capacity",
        "/v1/pulse",
    ):
        assert "#/components/parameters/RecordCountry" in refs(path), path
        assert "#/components/parameters/Country" not in refs(path), path
