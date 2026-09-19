"""Reject commit messages that carry a model identifier (CLAUDE.md: "Never put model identifiers
in commits, code comments, or shipped artifacts").

Two entry points:

* ``commit-msg`` hook: ``python scripts/check_commit_messages.py <message-file>`` — installed by
  ``make hooks`` (``git config core.hooksPath .githooks``).
* CI / audit: ``python scripts/check_commit_messages.py --range origin/main..HEAD`` checks every
  commit in the range (owner decision 2026-09-18: the branch history was rewritten once; this keeps
  it clean).

The pattern is deliberately narrow: product model names and dated model ids. The bare word
"Claude" in a ``Co-Authored-By`` trailer is allowed.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys

MODEL_IDENTIFIER = re.compile(
    r"\b(?:fable|opus|sonnet|haiku)\b(?:[ -]?\d+(?:\.\d+)?)?|\bclaude-(?:fable|opus|sonnet|haiku)[a-z0-9-]*",
    re.IGNORECASE,
)


def offending_lines(message: str) -> list[str]:
    return [line for line in message.splitlines() if MODEL_IDENTIFIER.search(line)]


def check_message(message: str, label: str) -> bool:
    bad = offending_lines(message)
    if not bad:
        return True
    print(f"{label}: commit message carries a model identifier (CLAUDE.md guardrail):", file=sys.stderr)
    for line in bad:
        print(f"    {line}", file=sys.stderr)
    return False


def check_range(rev_range: str) -> bool:
    git = shutil.which("git") or "git"
    shas = subprocess.run(  # noqa: S603 -- fixed argv; the range is the caller's own argument
        [git, "rev-list", rev_range], check=True, capture_output=True, text=True
    ).stdout.split()
    ok = True
    for sha in shas:
        body = subprocess.run(  # noqa: S603 -- fixed argv; sha comes from rev-list above
            [git, "log", "-1", "--format=%B", sha], check=True, capture_output=True, text=True
        ).stdout
        ok = check_message(body, sha[:7]) and ok
    print(f"checked {len(shas)} commit(s) in {rev_range}: {'clean' if ok else 'FAIL'}")
    return ok


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("message_file", nargs="?", help="path to the commit message (commit-msg hook)")
    parser.add_argument(
        "--range", dest="rev_range", help="git revision range to audit, e.g. origin/main..HEAD"
    )
    args = parser.parse_args(argv)
    if args.rev_range:
        return 0 if check_range(args.rev_range) else 1
    if not args.message_file:
        parser.error("a message file or --range is required")
    with open(args.message_file, encoding="utf-8") as fh:
        return 0 if check_message(fh.read(), "commit-msg") else 1


if __name__ == "__main__":
    sys.exit(main())
