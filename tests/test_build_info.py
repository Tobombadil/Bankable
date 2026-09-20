"""`services/api/build_info.py`: which commit is serving, and how fresh its rows are.

The question this answers is "am I seeing the latest version?", which is two questions — current
code and current data — that move independently, so both are pinned here.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy.orm import Session

from services.api import build_info as bi
from services.api.conftest import make_open_licence, make_public_source, make_visible_proposal

UTC = dt.UTC


@pytest.fixture(autouse=True)
def _clear_cache() -> None:
    bi.build_info.cache_clear()


def test_a_stamped_commit_wins_over_the_working_tree(monkeypatch: pytest.MonkeyPatch) -> None:
    """An image stamps the commit it was built from; the container has no `.git` to read."""
    monkeypatch.setenv("GIT_SHA", "abcdef0123456789")
    info = bi.build_info()
    assert info["commit"] == "abcdef012345"  # short, 12 characters
    assert info["commit_source"] == "GIT_SHA"
    assert info["dirty"] is None  # a stamped build cannot know, and does not guess


def test_the_working_tree_is_read_when_nothing_is_stamped(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in bi.COMMIT_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    info = bi.build_info()
    assert info["commit_source"] in {"working-tree", "unavailable"}
    if info["commit_source"] == "working-tree":
        assert info["commit"] and len(info["commit"]) == 12
        assert info["dirty"] in (True, False)


def test_a_command_that_printed_nothing_is_not_a_failed_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_git` must distinguish "ran, printed nothing" from "did not run".

    This is where the bug was. `git status --porcelain` prints nothing for a clean tree, and
    `stdout.strip() or None` turned that into the same `None` a missing git binary produces, so a
    clean checkout reported `dirty: None`. It stayed invisible on any machine with work in
    progress -- a dirty tree prints something -- and failed only on CI, which checks out clean
    every time. Stubbing `_git` cannot catch this; the empty-string handling has to be exercised
    here, one level down.
    """

    class _Result:
        returncode = 0
        stdout = "\n"

    monkeypatch.setattr(bi.shutil, "which", lambda _name: "/usr/bin/git")
    monkeypatch.setattr(bi.pathlib.Path, "exists", lambda _self: True)
    monkeypatch.setattr(bi.subprocess, "run", lambda *a, **k: _Result())
    assert bi._git("status", "--porcelain") == ""


def test_a_command_that_failed_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """The other half of the same distinction, so neither side can drift back."""

    class _Result:
        returncode = 128
        stdout = ""

    monkeypatch.setattr(bi.shutil, "which", lambda _name: "/usr/bin/git")
    monkeypatch.setattr(bi.pathlib.Path, "exists", lambda _self: True)
    monkeypatch.setattr(bi.subprocess, "run", lambda *a, **k: _Result())
    assert bi._git("status", "--porcelain") is None


def test_a_clean_tree_is_reported_clean_not_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    """`git status --porcelain` prints nothing for a clean tree. That empty string is the answer
    "no uncommitted edits", not a failed call, and the two must not collapse into one `None`.

    This is the regression: locally the tree is almost always dirty, so `dirty` came back a real
    boolean and the bug hid; CI checks out clean every time, so it failed there and only there.
    """
    for name in bi.COMMIT_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    calls: list[tuple[str, ...]] = []

    def fake_git(*args: str) -> str | None:
        calls.append(args)
        return "0123456789abcdef0123456789abcdef01234567" if args[0] == "rev-parse" else ""

    monkeypatch.setattr(bi, "_git", fake_git)
    info = bi.build_info()
    assert info == {
        "commit": "0123456789ab",
        "commit_source": "working-tree",
        "dirty": False,
    }
    assert ("status", "--porcelain") in calls


def test_an_unreadable_status_is_unknown_rather_than_clean(monkeypatch: pytest.MonkeyPatch) -> None:
    """The other side of the same distinction: a `git status` that actually failed says nothing
    about the tree, so `dirty` is null. Claiming "clean" there would be a guess."""
    for name in bi.COMMIT_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(
        bi,
        "_git",
        lambda *args: "0123456789abcdef" if args[0] == "rev-parse" else None,
    )
    assert bi.build_info()["dirty"] is None


def test_nothing_raises_when_git_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """A read-only checkout, a container without git, a stripped image: the footer renders nothing
    rather than failing the page."""
    for name in bi.COMMIT_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(bi.shutil, "which", lambda _name: None)
    assert bi.build_info() == {"commit": None, "commit_source": "unavailable", "dirty": None}


def test_data_as_of_is_the_newest_fetch_not_the_publish_lag(db: Session) -> None:
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    make_visible_proposal(db, src, public_id_suffix="1")
    db.commit()

    stamp = bi.data_as_of(db)

    assert stamp is not None and stamp.endswith("Z")
    parsed = dt.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    # `make_visible_proposal` stamps the source link's `retrieved_at` a day ago; the assertion is
    # that this reports the fetch date rather than a publication timestamp. (The two used to be
    # a fortnight apart, which made the distinction vivid; since 2026-09-19 a record publishes
    # immediately, so the test now turns on the day of ingest lag in the fixture.)
    assert dt.datetime.now(UTC) - parsed < dt.timedelta(days=3)


def test_data_as_of_is_none_on_an_empty_store(db: Session) -> None:
    assert bi.data_as_of(db) is None


def test_health_reports_the_build_and_the_data_vintage(client) -> None:
    """The endpoint a deploy check reads, and the source the site footer reads."""
    body = client.get("/v1/health").json()
    assert "build" in body and "commit_source" in body["build"]
    assert "source_data_as_of" in body
