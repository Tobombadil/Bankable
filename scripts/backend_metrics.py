"""Measured back-end structure: per-module lines, functions, longest function, nesting, fan-in and
fan-out; layering edges; near-duplicate function bodies.

Reproducible baseline for the 2026-09-26 back-end refactor (`docs/42-backend-review-2026-09-26.md`;
`docs/00-PLAN.md` decisions log, 2026-09-25 row): every refactor lane reports its delta with this
script, not by eye.

    .venv/bin/python scripts/backend_metrics.py            # full report
    .venv/bin/python scripts/backend_metrics.py --top 5    # shorter tables

Measures **physical lines** (docstrings and blanks included); functions via `ast`; fan-in as the number
of modules whose parsed `import` / `from … import` statements resolve to the module (relative imports
are not counted); nesting as the depth of if/for/while/with/try/match blocks inside a function;
duplicates as function bodies identical after normalising identifiers, attributes and constants, with at
least `--min-statements` statements. Tests, `.venv` and `migrations/versions` are excluded.
"""

from __future__ import annotations

import argparse
import ast
import collections
import copy
import pathlib
import sys
from collections.abc import Callable, Iterable

ROOT = pathlib.Path(__file__).resolve().parents[1]
PACKAGES = ("pipeline", "services", "web")
BLOCKS = (ast.If, ast.For, ast.While, ast.With, ast.Try, ast.AsyncFor, ast.AsyncWith, ast.Match)
LONG_FUNCTION = 80
DEEP_NESTING = 5


def _is_test(path: pathlib.Path) -> bool:
    return path.name.startswith(("test_", "conftest")) or "tests" in path.parts


def _module_files() -> dict[str, pathlib.Path]:
    files: dict[str, pathlib.Path] = {}
    for package in PACKAGES:
        for path in sorted((ROOT / package).rglob("*.py")):
            if ".venv" in path.parts or _is_test(path) or "versions" in path.parts:
                continue
            name = ".".join(path.relative_to(ROOT).with_suffix("").parts)
            files[name.removesuffix(".__init__")] = path
    return files


def _depth(node: ast.AST, current: int = 0) -> int:
    deepest = current
    for child in ast.iter_child_nodes(node):
        step = 1 if isinstance(child, BLOCKS) else 0
        deepest = max(deepest, _depth(child, current + step))
    return deepest


class _Normalise(ast.NodeTransformer):
    """Erase names, attributes and constants so structurally identical bodies compare equal."""

    def visit_Name(self, node: ast.Name) -> ast.AST:
        node.id = "_"
        return node

    def visit_arg(self, node: ast.arg) -> ast.AST:
        node.arg = "_"
        return node

    def visit_Constant(self, node: ast.Constant) -> ast.AST:
        node.value = type(node.value).__name__
        return node

    def visit_Attribute(self, node: ast.Attribute) -> ast.AST:
        self.generic_visit(node)
        node.attr = "_a"
        return node

    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.AST:
        self.generic_visit(node)
        node.name = "_f"
        return node

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> ast.AST:
        self.generic_visit(node)
        node.name = "_f"
        return node


FunctionNode = ast.FunctionDef | ast.AsyncFunctionDef


def _measure(
    files: dict[str, pathlib.Path],
) -> tuple[dict[str, dict[str, object]], dict[str, set[str]], list[tuple[str, FunctionNode]]]:
    rows: dict[str, dict[str, object]] = {}
    imports: dict[str, set[str]] = collections.defaultdict(set)
    functions: list[tuple[str, FunctionNode]] = []
    for module, path in files.items():
        source = path.read_text()
        tree = ast.parse(source)
        found: list[tuple[int, str, int, int]] = []
        for node in ast.walk(tree):
            if isinstance(node, FunctionNode):
                length = (node.end_lineno or node.lineno) - node.lineno + 1
                found.append((length, node.name, node.lineno, _depth(node)))
                functions.append((module, node))
            elif isinstance(node, ast.Import):
                imports[module].update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                imports[module].add(node.module)
                for alias in node.names:
                    if f"{node.module}.{alias.name}" in files:
                        imports[module].add(f"{node.module}.{alias.name}")
        longest = max(found, default=(0, "-", 0, 0))
        rows[module] = {
            "lines": source.count("\n") + 1,
            "funcs": len(found),
            "longest": longest[0],
            "longest_name": f"{longest[1]}:{longest[2]}",
            "maxdepth": max((f[3] for f in found), default=0),
            "big": sum(1 for f in found if f[0] >= LONG_FUNCTION),
            "deep": sum(1 for f in found if f[3] >= DEEP_NESTING),
        }
    return rows, imports, functions


def _edges(files: dict[str, pathlib.Path], imports: dict[str, set[str]]) -> dict[str, set[str]]:
    def resolve(name: str) -> str | None:
        while name and name not in files:
            name = name.rpartition(".")[0]
        return name or None

    edges: dict[str, set[str]] = collections.defaultdict(set)
    for module, names in imports.items():
        targets = {resolve(n) for n in names}
        edges[module] = {t for t in targets if t is not None and t != module}
    return edges


def _duplicates(
    functions: Iterable[tuple[str, FunctionNode]], min_statements: int
) -> list[list[tuple[str, str, int, int, int]]]:
    groups: dict[str, list[tuple[str, str, int, int, int]]] = collections.defaultdict(list)
    for module, node in functions:
        statements = sum(1 for n in ast.walk(node) if isinstance(n, ast.stmt))
        if statements < min_statements:
            continue
        body = ast.Module(body=copy.deepcopy(node.body), type_ignores=[])
        first = body.body[0] if body.body else None
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
            body.body = body.body[1:]  # drop the docstring
        key = ast.dump(_Normalise().visit(body))
        length = (node.end_lineno or node.lineno) - node.lineno + 1
        groups[key].append((module, node.name, node.lineno, length, statements))
    return sorted((g for g in groups.values() if len(g) > 1), key=lambda g: -g[0][4])


def render(top: int, min_statements: int) -> str:
    files = _module_files()
    rows, imports, functions = _measure(files)
    edges = _edges(files, imports)
    fan_in = collections.Counter(t for targets in edges.values() for t in targets)
    for module, row in rows.items():
        row["fanin"] = fan_in[module]
        row["fanout"] = len(edges.get(module, ()))
    out: list[str] = []
    out.append(
        f"modules={len(rows)} total_lines={sum(int(r['lines']) for r in rows.values())} "
        f"funcs={len(functions)} funcs>={LONG_FUNCTION}={sum(int(r['big']) for r in rows.values())} "
        f"funcs_depth>={DEEP_NESTING}={sum(int(r['deep']) for r in rows.values())}"
    )

    def table(key: str, k: int) -> None:
        out.append(f"\n## top {k} by {key}")
        for module, r in sorted(rows.items(), key=lambda kv: -int(kv[1][key]))[:k]:
            out.append(
                f"{int(r[key]):>6}  {module:<48} lines={r['lines']:<5} funcs={r['funcs']:<3} "
                f"longest={r['longest']}({r['longest_name']}) depth={r['maxdepth']} "
                f"big{LONG_FUNCTION}={r['big']} deep{DEEP_NESTING}={r['deep']} "
                f"fanin={r['fanin']} fanout={r['fanout']}"
            )

    table("lines", top)
    table("longest", top)
    table("fanin", top)
    table("fanout", min(top, 8))

    out.append("\n## layering edges")

    def show(title: str, pred: Callable[[str, str], bool]) -> None:
        out.append(f"### {title}")
        for module in sorted(edges):
            hits = sorted(t for t in edges[module] if pred(module, t))
            if hits:
                out.append(f"  {module} -> {', '.join(hits)}")

    lower = ("ingest", "resolve", "social", "alerts")
    show("web -> services.*", lambda m, t: m.startswith("web") and t.startswith("services"))
    show("web -> pipeline.*", lambda m, t: m.startswith("web") and t.startswith("pipeline"))
    show(
        "services.api -> services.{ingest,resolve,social,alerts}",
        lambda m, t: m.startswith("services.api") and any(t.startswith(f"services.{x}") for x in lower),
    )
    show(
        "services.ingest/resolve -> services.api",
        lambda m, t: m.startswith(("services.ingest", "services.resolve")) and t.startswith("services.api"),
    )
    show("pipeline -> services.*", lambda m, t: m.startswith("pipeline") and t.startswith("services"))
    show("services.* -> pipeline.*", lambda m, t: m.startswith("services") and t.startswith("pipeline"))

    def package(module: str) -> str | None:
        parts = module.split(".")
        return parts[1] if parts[0] == "services" and len(parts) > 1 else None

    matrix: collections.Counter[tuple[str, str]] = collections.Counter()
    for module, targets in edges.items():
        for target in targets:
            a, b = package(module), package(target)
            if a and b and a != b:
                matrix[(a, b)] += 1
    out.append("### services cross-package edge counts (from -> to: n)")
    out.extend(f"  {a} -> {b}: {n}" for (a, b), n in sorted(matrix.items(), key=lambda kv: -kv[1]))

    groups = _duplicates(functions, min_statements)
    out.append(f"\n## near-duplicate function bodies (normalised AST, >={min_statements} statements)")
    out.append(f"groups={len(groups)}")
    for group in groups:
        out.append(
            "  "
            + " | ".join(f"{m}::{n}:{line} ({length} lines, {s} stmts)" for m, n, line, length, s in group)
        )
    return "\n".join(out) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--top", type=int, default=15, help="rows per table (default 15)")
    parser.add_argument("--min-statements", type=int, default=15, help="duplicate threshold (default 15)")
    args = parser.parse_args(argv)
    sys.stdout.write(render(args.top, args.min_statements))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
