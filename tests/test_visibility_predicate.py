"""Unit tests for `services/api/visibility.py` itself, as opposed to the endpoint behaviour the
`tests/test_api_*_visibility.py` files prove through the API.

The module is one of docs/04 E-7's 100%-branch-coverage gate modules, and its CI step is now an
enforced gate rather than a report. Its only real branch that no endpoint test can reach is the
module-level drift guard between `PUBLISHABLE_REUSE_CLASSES` and `services.db.models`'
`REUSE_CLASSES`: reaching it needs the shared vocabulary to have moved, which is an import-time
condition, not a query. Covering it with a fresh import of the module under a patched vocabulary
is cheap, so the guard is exercised here instead of being excluded from the gate it is part of.
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
