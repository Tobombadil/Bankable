#!/usr/bin/env python3
"""Verify api/openapi.yaml against docs/10-prd-mvp.md §4 and docs/23-api-spec-outline.md.

Checks, in order:
  1. the document parses and every internal $ref resolves;
  2. every operation carries x-tier, x-prd-stories, x-status and a summary;
  3. every named example validates against the schema it illustrates (docs/04 E-8);
  4. every PRD story id (US-1xx…US-10xx) is claimed by at least one operation, and every
     claimed id exists in the PRD;
  5. prints the story -> operation mapping and the operation / schema counts.

Usage:  python api/check_story_coverage.py [--quiet]
Exit code 0 when every story is covered, 1 otherwise.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "api" / "openapi.yaml"
PRD = ROOT / "docs" / "10-prd-mvp.md"
HTTP_METHODS = {"get", "put", "post", "delete", "options", "head", "patch", "trace"}

# US-908 is a release gate proved by tests over the endpoints, not by an endpoint of its own
# (docs/23 §11). It is satisfied here by GET /v1/health, which exposes the checklist probes.
NOTE = {
    "US-908": "release gate (docs/23 §11) — exposed as launch-checklist probes on GET /v1/health",
}

# components.examples name -> the components.schemas name it illustrates.
EXAMPLE_SCHEMAS = {
    "ProposalListPublic": "ProposalListResponse",
    "ProposalDetailPublic": "ProposalDetailResponse",
    "OpportunityListPublic": "OpportunityListResponse",
    "EventList": "EventListResponse",
    "GeoClusters": "GeoResponse",
    "MatchList": "MatchListResponse",
    "ApiKeyCreated": "ApiKeyCreatedResponse",
    "JsonFeedExample": "JsonFeed",
    "WebhookEventPublished": "WebhookEventPublished",
}
BASE_URI = "https://spec.local/openapi.yaml"


def prd_story_ids(text: str) -> list[str]:
    """Story ids as declared by their PRD §4 headings, in document order."""
    return [m.group(1) for m in re.finditer(r"^\*\*(US-\d{3,4})\b", text, re.MULTILINE)]


def walk_refs(node, path="#"):
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "$ref" and isinstance(value, str):
                yield value, path
            else:
                yield from walk_refs(value, f"{path}/{key}")
    elif isinstance(node, list):
        for i, value in enumerate(node):
            yield from walk_refs(value, f"{path}/{i}")


def resolve(doc, ref: str):
    if not ref.startswith("#/"):
        return None  # external refs are not used in this document
    node = doc
    for part in ref[2:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def check_examples(doc) -> list[str]:
    """Validate every named example, and every inline problem example, against its schema."""
    registry = Registry().with_resource(
        uri=BASE_URI, resource=Resource(contents=doc, specification=DRAFT202012)
    )

    def errors_for(schema_name: str, instance) -> list[str]:
        validator = Draft202012Validator(
            {"$id": BASE_URI + "#inline", "$ref": f"{BASE_URI}#/components/schemas/{schema_name}"},
            registry=registry,
        )
        return [
            f"{'/'.join(str(p) for p in e.path) or '<root>'}: {e.message}"
            for e in sorted(validator.iter_errors(instance), key=lambda e: list(e.path))
        ]

    found: list[str] = []
    for name, example in (doc.get("components", {}).get("examples") or {}).items():
        schema_name = EXAMPLE_SCHEMAS.get(name)
        if schema_name is None:
            found.append(f"example {name}: not listed in EXAMPLE_SCHEMAS, so it is never validated")
            continue
        found += [f"example {name} vs {schema_name}: {m}" for m in errors_for(schema_name, example["value"])]

    for resp_name, response in (doc.get("components", {}).get("responses") or {}).items():
        body = (response.get("content") or {}).get("application/problem+json")
        if not body:
            continue
        instances = {}
        if "example" in body:
            instances[resp_name] = body["example"]
        for ex_name, ex in (body.get("examples") or {}).items():
            instances[f"{resp_name}.{ex_name}"] = ex["value"]
        for label, instance in instances.items():
            found += [f"problem example {label}: {m}" for m in errors_for("Problem", instance)]
    return found


def operations(doc):
    """(method, path, operation) for both `paths` and `webhooks`."""
    for group in ("paths", "webhooks"):
        for path, item in (doc.get(group) or {}).items():
            for method, op in item.items():
                if method in HTTP_METHODS:
                    yield method.upper(), path, op


def main() -> int:
    quiet = "--quiet" in sys.argv
    doc = yaml.safe_load(SPEC.read_text())
    stories = prd_story_ids(PRD.read_text())
    problems: list[str] = []

    # 1. every internal $ref resolves
    broken = sorted({ref for ref, _ in walk_refs(doc) if resolve(doc, ref) is None})
    problems += [f"unresolved $ref: {ref}" for ref in broken]

    # 2. every example validates against the schema it illustrates
    problems += check_examples(doc)

    # 3. required extensions on every operation
    coverage: dict[str, list[str]] = {s: [] for s in stories}
    unknown_claims: set[str] = set()
    op_ids: list[str] = []
    for method, path, op in operations(doc):
        label = f"{method} {path}"
        op_ids.append(op.get("operationId", f"<missing operationId> {label}"))
        for field in ("summary", "x-tier", "x-prd-stories"):
            if not op.get(field):
                problems.append(f"{label}: missing {field}")
        if path not in (doc.get("webhooks") or {}) and not op.get("x-status"):
            problems.append(f"{label}: missing x-status")
        tier = op.get("x-tier")
        if tier and tier not in {"public", "pro", "api", "admin"}:
            problems.append(f"{label}: x-tier '{tier}' is not public|pro|api|admin")
        for story in op.get("x-prd-stories", []):
            if story in coverage:
                coverage[story].append(op.get("operationId", label))
            else:
                unknown_claims.add(f"{label}: x-prd-stories names unknown story {story}")
    problems += sorted(unknown_claims)

    duplicates = sorted({i for i in op_ids if op_ids.count(i) > 1})
    problems += [f"duplicate operationId: {i}" for i in duplicates]

    uncovered = [s for s, ops in coverage.items() if not ops]

    schemas = doc.get("components", {}).get("schemas", {})
    if not quiet:
        print(f"Specification : {SPEC.relative_to(ROOT)}")
        print(f"PRD           : {PRD.relative_to(ROOT)} §4")
        print(f"Operations    : {len(op_ids)}   Schemas: {len(schemas)}   "
              f"Parameters: {len(doc.get('components', {}).get('parameters', {}))}   "
              f"Responses: {len(doc.get('components', {}).get('responses', {}))}   "
              f"Examples: {len(doc.get('components', {}).get('examples', {}))}")
        print(f"PRD stories   : {len(stories)}")
        print()
        print(f"{'Story':<9} {'Ops':>3}  Operations")
        print("-" * 100)
        for story in stories:
            ops = coverage[story]
            note = f"   [{NOTE[story]}]" if story in NOTE else ""
            print(f"{story:<9} {len(ops):>3}  {', '.join(ops) if ops else 'NONE'}{note}")
        print()

    for problem in problems:
        print(f"FAIL {problem}")
    if uncovered:
        print(f"FAIL stories with no operation: {', '.join(uncovered)}")

    if problems or uncovered:
        print(f"\nRESULT: FAIL — {len(problems)} problem(s), "
              f"{len(stories) - len(uncovered)}/{len(stories)} stories covered")
        return 1
    print(f"RESULT: PASS — {len(stories)}/{len(stories)} PRD stories covered by "
          f"{len(op_ids)} operations; all $refs resolve")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
