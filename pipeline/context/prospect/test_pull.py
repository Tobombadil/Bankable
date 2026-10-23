# ruff: noqa: S101 - asserts are the point (docs/04 E-6)
"""pipeline.context.prospect.pull: the server's monthly pull of the published prospect build, for the
states GRID_LAYERS_REGIONS names, and "not published" (never a failure) while no build exists."""

from __future__ import annotations

import contextlib
import io
import json
import pathlib
from typing import Any

import pytest

from pipeline.context.prospect import pull as pull_mod

BUCKET = "private"


class _Missing(Exception):
    """What botocore raises for a missing key, structurally (`objectstore._error_code` reads it)."""

    def __init__(self) -> None:
        super().__init__("Not Found")
        self.response = {"Error": {"Code": "404"}, "ResponseMetadata": {"HTTPStatusCode": 404}}


class FakeS3:
    def __init__(self, keys: set[str]) -> None:
        self.keys = keys
        self.heads: list[str] = []

    def head_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803 - boto3's names
        assert Bucket == BUCKET
        self.heads.append(Key)
        if Key not in self.keys:
            raise _Missing()
        return {}


@pytest.fixture()
def synced(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """`services.context_sample.sync.sync` replaced by a recorder (its own tests cover the download)."""
    calls: list[str] = []

    def fake_sync(dest: pathlib.Path, *, region: str, client: Any, bucket: str) -> dict[str, Any]:
        calls.append(region)
        return {"status": "updated", "build_id": "20261103T064100Z"}

    import services.context_sample.sync as sync_mod

    monkeypatch.setattr(sync_mod, "sync", fake_sync)
    return calls


def test_a_texas_server_reads_the_texas_build_only() -> None:
    assert pull_mod.regions_for(["US-TX"]) == ["texas"]
    assert pull_mod.regions_for([" us-tx "]) == ["texas"]
    assert pull_mod.regions_for(["US-OK", "US-TX"]) == ["conus"]
    assert pull_mod.regions_for(None) == ["conus"]  # `all`


def test_the_published_build_is_synced(tmp_path: pathlib.Path, synced: list[str]) -> None:
    both = FakeS3({"prospect/current/texas.json", "prospect/current/conus.json"})
    result = pull_mod.pull(tmp_path, states=["US-TX"], client=both, bucket=BUCKET)
    assert result["region"] == "texas" and result["status"] == "updated" and synced == ["texas"]
    result = pull_mod.pull(tmp_path, states=["US-TX", "US-OK"], client=both, bucket=BUCKET)
    assert result["region"] == "conus" and synced == ["texas", "conus"]


def test_a_texas_server_never_falls_back_to_the_national_build(
    tmp_path: pathlib.Path, synced: list[str]
) -> None:
    """Review 2026-10-23: with no `texas.json` the pull took `conus`, the national build nobody has
    run or measured, onto the beta's disk and into a site-finder run that peaks at 1.2 GB. Now a
    Texas server waits for its own build and says which dispatch publishes it."""
    conus_only = FakeS3({"prospect/current/conus.json"})
    result = pull_mod.pull(tmp_path, states=["US-TX"], client=conus_only, bucket=BUCKET)
    assert result["status"] == pull_mod.NOT_PUBLISHED and synced == []
    assert conus_only.heads == ["prospect/current/texas.json"]
    assert "region texas" in result["to_publish"]


def test_no_published_build_is_not_published_and_touches_nothing(
    tmp_path: pathlib.Path, synced: list[str]
) -> None:
    (tmp_path / "layers.json").write_text('{"build_id": "20261001T000000Z"}')
    result = pull_mod.pull(tmp_path, states=["US-TX"], client=FakeS3(set()), bucket=BUCKET)
    assert result == {
        "status": pull_mod.NOT_PUBLISHED,
        "pointers": ["prospect/current/texas.json"],
        "to_publish": "run prospect-layers.yml with region texas (docs/66 'Site finder go-live')",
    }
    assert (
        synced == [] and json.loads((tmp_path / "layers.json").read_text())["build_id"] == "20261001T000000Z"
    )


def test_the_cli_reads_grid_layers_regions_and_exits_0_when_not_published(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, synced: list[str]
) -> None:
    import services.context_sample.sync as sync_mod

    client = FakeS3(set())
    monkeypatch.setattr(sync_mod, "client_from_env", lambda: (client, BUCKET))
    monkeypatch.setenv("GRID_LAYERS_REGIONS", "US-TX")
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = pull_mod.main(["--dest", str(tmp_path)])
    assert code == 0
    assert json.loads(out.getvalue().strip().splitlines()[-1])["status"] == pull_mod.NOT_PUBLISHED
    assert client.heads == ["prospect/current/texas.json"]


def test_the_cli_refuses_to_run_without_a_regions_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GRID_LAYERS_REGIONS", raising=False)
    with pytest.raises(SystemExit, match="GRID_LAYERS_REGIONS is not set"):
        pull_mod.main([])
