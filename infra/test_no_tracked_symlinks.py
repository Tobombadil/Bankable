"""The repository tracks no symbolic links.

On 2026-10-07 a lane commit picked up a worktree's `.venv` and `data/normalized` symlinks, because
`.gitignore` listed them as `.venv/` and `data/normalized/`, and a trailing-slash pattern matches only
a directory, never a symlink. Checking that commit out replaced the real directories with links to
themselves. The ignore patterns now match both; this test stops any symlink from being committed.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


def test_no_tracked_symlinks() -> None:
    git = shutil.which("git")
    if git is None or not (REPO / ".git").exists():
        pytest.skip("not a git checkout")
    listing = subprocess.run(  # noqa: S603 -- fixed argv, no shell
        [git, "ls-files", "-s"], cwd=REPO, capture_output=True, text=True, check=True
    ).stdout
    links = [line.split("\t", 1)[1] for line in listing.splitlines() if line.startswith("120000 ")]
    assert links == [], f"tracked symlinks: {links}"


@pytest.mark.parametrize("path", [".venv", "data/normalized"])
def test_local_only_paths_are_ignored_as_links_too(path: str) -> None:
    patterns = {line.strip() for line in (REPO / ".gitignore").read_text().splitlines()}
    assert path in patterns, f"{path} must be ignored without a trailing slash so a symlink matches"
