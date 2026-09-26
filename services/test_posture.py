"""`services/posture.py` at 100% branch coverage: the pure helpers under every input, and the one
environment read under set / unset / garbage."""

from __future__ import annotations

import pytest

from services import posture


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, "commercial"),
        ("", "commercial"),
        ("   ", "commercial"),
        ("commercial", "commercial"),
        ("noncommercial", "noncommercial"),
        ("  NonCommercial ", "noncommercial"),
        ("non-commercial", "commercial"),  # not the vocabulary: fails closed
        ("research", "commercial"),
        ("open", "commercial"),
    ],
)
def test_normalise_posture_fails_closed_to_commercial(raw: str | None, expected: str) -> None:
    assert posture.normalise_posture(raw) == expected


def test_platform_posture_reads_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(posture.ENV_VAR, raising=False)
    assert posture.platform_posture() == "commercial"
    monkeypatch.setenv(posture.ENV_VAR, "noncommercial")
    assert posture.platform_posture() == "noncommercial"
    monkeypatch.setenv(posture.ENV_VAR, "anything-else")
    assert posture.platform_posture() == "commercial"


def test_publishable_classes_per_posture() -> None:
    assert posture.publishable_reuse_classes("commercial") == ("open", "attribution")
    assert posture.publishable_reuse_classes("noncommercial") == ("open", "attribution", "noncommercial")
    # An unrecognised posture handed straight to the helper is the commercial set, not an error
    # and not a wider set.
    assert posture.publishable_reuse_classes("garbage") == ("open", "attribution")


def test_gated_classes_are_the_complement_under_both_postures() -> None:
    for p in posture.PLATFORM_POSTURES:
        publishable = set(posture.publishable_reuse_classes(p))
        gated = set(posture.gated_reuse_classes(p))
        assert publishable.isdisjoint(gated)
        assert publishable | gated == set(posture._ALL_REUSE_CLASSES)
    assert posture.gated_reuse_classes("commercial") == ("noncommercial", "restricted", "unknown")
    assert posture.gated_reuse_classes("noncommercial") == ("restricted", "unknown")
    assert "restricted" in posture.gated_reuse_classes("noncommercial")
    assert "unknown" in posture.gated_reuse_classes("noncommercial")


def test_statement_is_derived_from_the_setting() -> None:
    assert posture.posture_statement("noncommercial").startswith(
        "This platform operates under a noncommercial posture: sources that permit noncommercial reuse "
        "are published with attribution; they will be withdrawn if the posture changes."
    )
    assert posture.posture_statement("commercial").startswith(
        "This platform operates under a commercial posture"
    )
    # Garbage reads as the default, so the sentence never claims a posture the gate is not applying.
    assert posture.posture_statement("garbage") == posture.posture_statement("commercial")
