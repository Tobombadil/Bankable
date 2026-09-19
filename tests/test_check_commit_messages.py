"""Tests for scripts/check_commit_messages.py (the commit-msg guard behind CLAUDE.md's
"never put model identifiers in commits" rule)."""

from scripts.check_commit_messages import offending_lines


def test_plain_trailer_is_allowed() -> None:
    assert offending_lines("Fix x\n\nCo-Authored-By: Claude <noreply@anthropic.com>\n") == []


def test_model_names_and_ids_are_rejected() -> None:
    assert offending_lines("Co-Authored-By: Claude Fable 5.1 <x>") == ["Co-Authored-By: Claude Fable 5.1 <x>"]
    assert offending_lines("Co-Authored-By: Claude Opus 5 <x>") != []
    assert offending_lines("built with claude-sonnet-5") != []


def test_ordinary_message_passes() -> None:
    assert offending_lines("Add the opportunity feed\n\nGroups county rows at the state region.") == []
