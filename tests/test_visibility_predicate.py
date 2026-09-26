"""Unit tests for `services/api/visibility.py` itself, as opposed to the endpoint behaviour the
`tests/test_api_*_visibility.py` files prove through the API.

The module is one of docs/04 E-7's 100%-branch-coverage gate modules, and its CI step is now an
enforced gate rather than a report. Its only real branch that no endpoint test can reach is the
module-level drift guard between `PUBLISHABLE_REUSE_CLASSES` and `services.db.models`'
`REUSE_CLASSES`: reaching it needs the shared vocabulary to have moved, which is an import-time
condition, not a query. Covering it with a fresh import of the module under a patched vocabulary
is cheap, so the guard is exercised here instead of being excluded from the gate it is part of.

Since 2026-09-25 the module also reads the platform posture once at import (`services/posture.py`,
docs/26). The same re-import trick executes it under **both** postures below, so the tuple each
one yields is asserted against the module actually running, not against the helper alone.
"""

from __future__ import annotations

import importlib.util
import types

import pytest

import services.api.visibility as visibility
import services.db.models as models


def _reimport_visibility() -> types.ModuleType:
    """Execute `visibility.py` again as a throwaway module, so the module-level guard runs against
    whatever `services.db.models.REUSE_CLASSES` currently holds. `sys.modules` is untouched: the
    real module keeps serving every other test."""
    spec = importlib.util.spec_from_file_location("visibility_drift_probe", visibility.__file__)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_publishable_reuse_classes_are_a_subset_of_the_shared_vocabulary() -> None:
    assert set(visibility.PUBLISHABLE_REUSE_CLASSES) <= set(models.REUSE_CLASSES)


def test_the_drift_guard_refuses_to_import_when_the_shared_vocabulary_moves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If `attribution` were ever dropped from `services.db.models.REUSE_CLASSES`, this module
    would silently publish under a class the rest of the system no longer knows. The guard turns
    that into an import-time failure; this is the test that proves it fires."""
    monkeypatch.setattr(models, "REUSE_CLASSES", ("open", "restricted", "unknown"))
    with pytest.raises(RuntimeError, match="drifted"):
        _reimport_visibility()


def test_the_module_imports_cleanly_while_the_vocabulary_agrees() -> None:
    probe = _reimport_visibility()
    assert probe.PUBLISHABLE_REUSE_CLASSES == visibility.PUBLISHABLE_REUSE_CLASSES


# ------------------------------------------------------------------------------- both postures
def test_under_the_commercial_posture_only_open_and_attribution_are_publishable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PLATFORM_POSTURE", raising=False)
    assert _reimport_visibility().PUBLISHABLE_REUSE_CLASSES == ("open", "attribution")
    monkeypatch.setenv("PLATFORM_POSTURE", "commercial")
    assert _reimport_visibility().PUBLISHABLE_REUSE_CLASSES == ("open", "attribution")


def test_under_the_noncommercial_posture_noncommercial_joins_the_publishable_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PLATFORM_POSTURE", "noncommercial")
    probe = _reimport_visibility()
    assert probe.PUBLISHABLE_REUSE_CLASSES == ("open", "attribution", "noncommercial")
    assert set(probe.PUBLISHABLE_REUSE_CLASSES) <= set(models.REUSE_CLASSES)
    # `restricted`/`unknown` are gated under every posture; the drift guard still holds.
    assert not {"restricted", "unknown"} & set(probe.PUBLISHABLE_REUSE_CLASSES)


def test_an_unrecognised_posture_fails_closed_to_commercial(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PLATFORM_POSTURE", "non-commercial")  # a plausible typo, not the vocabulary
    assert _reimport_visibility().PUBLISHABLE_REUSE_CLASSES == ("open", "attribution")
    monkeypatch.setenv("PLATFORM_POSTURE", "open")
    assert _reimport_visibility().PUBLISHABLE_REUSE_CLASSES == ("open", "attribution")


def test_the_licence_clause_is_byte_identical_across_tiers_under_the_noncommercial_posture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The posture widens the class set for every tier at once, never for one tier."""
    import datetime as dt

    monkeypatch.setenv("PLATFORM_POSTURE", "noncommercial")
    probe = _reimport_visibility()
    now = dt.datetime(2026, 9, 25, tzinfo=dt.UTC)
    clauses = {
        tier: str(
            probe.proposal_visibility_filter(tier, now)[3].compile(compile_kwargs={"literal_binds": True})
        )
        for tier in ("public", "pro", "api")
    }
    assert len(set(clauses.values())) == 1, clauses
    rendered = next(iter(clauses.values()))
    assert "'noncommercial'" in rendered
    assert "'restricted'" not in rendered and "'unknown'" not in rendered
