"""The parcel layer reader (`services/context_sample/parcels.py`) on a pulled layer laid out by the site
finder's fixture (`services/site_finder/testing.py::write_parcels`: the TxGIO builder's synthetic county):
row-group pruning by the bbox covering, the manifest's counties, a new pull noticed by its mtime, the
data-directory setting, and reads from several threads at once."""

from __future__ import annotations

import json
import os
import pathlib
import threading
import time

import pytest

from services.context_sample import parcels as cp
from services.site_finder.testing import PARCELS_VERSION, write_parcels


@pytest.fixture()
def root(tmp_path: pathlib.Path) -> pathlib.Path:
    write_parcels(tmp_path)
    cp.clear_cache()
    return tmp_path


def test_no_pull_means_no_layer(tmp_path: pathlib.Path) -> None:
    cp.clear_cache()
    assert cp.default_layer(tmp_path) is None


def test_the_layer_reads_the_pulled_manifest_and_its_counties(root: pathlib.Path) -> None:
    layer = cp.default_layer(root)
    assert layer is not None and layer.version == PARCELS_VERSION
    assert set(layer.counties) == {"48999"} and layer.loaded("48999") and not layer.loaded("48001")
    county = layer.counties["48999"]
    assert county.parcels == 16 and county.holdings == 12 and county.name == "Fixture County"
    assert layer.counties_in((-98.0, 31.0, -97.99, 31.01)) == ["48999"]
    assert layer.counties_in((-90.0, 40.0, -89.0, 41.0)) == []
    summary = layer.summary()
    assert summary["counties"]["48999"]["parcels"] == 16 and summary["source_id"] == cp.SOURCE_ID


def test_a_box_read_keeps_only_the_parcels_that_meet_it(root: pathlib.Path) -> None:
    layer = cp.default_layer(root)
    assert layer is not None
    everything = layer.read_bbox((-98.1, 30.9, -97.9, 31.1))
    assert everything is not None and everything.num_rows == 16
    # A box around Alpha's first parcel (100-500 m north of the line, 0-400 m east of -97.99).
    small = layer.read_bbox((-97.989, 31.0015, -97.987, 31.003), columns=cp.MAP_COLUMNS)
    assert small is not None and 1 <= small.num_rows <= 4
    assert set(small.column_names) >= {*cp.MAP_COLUMNS, "geometry", "bbox"}
    assert all(h for h in small.column("holding_id").to_pylist())
    assert layer.read_bbox((-97.5, 30.5, -97.4, 30.6)) is None


def test_a_new_pull_is_noticed(root: pathlib.Path) -> None:
    layer = cp.default_layer(root)
    assert layer is not None
    path = layer.manifest_path
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["version"] = "v1/20261001-20261101T0000Z"
    time.sleep(0.01)
    path.write_text(json.dumps(doc), encoding="utf-8")
    os.utime(path, (time.time() + 5, time.time() + 5))
    again = cp.default_layer(root)
    assert again is layer and again.version == "v1/20261001-20261101T0000Z"


def test_the_directory_can_be_set_apart_from_the_data_root(
    root: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PARCELS_DATA_DIR", str(root / cp.RELATIVE_DIR))
    cp.clear_cache()
    layer = cp.default_layer(pathlib.Path("/nonexistent"))
    assert layer is not None and layer.loaded("48999")
    monkeypatch.delenv("PARCELS_DATA_DIR")
    monkeypatch.setenv("INFRAQUE_DATA_DIR", str(root))
    assert cp.parcels_dir() == root / cp.RELATIVE_DIR


def test_reads_from_many_threads_agree(root: pathlib.Path) -> None:
    layer = cp.default_layer(root)
    assert layer is not None
    box = (-98.1, 30.9, -97.9, 31.1)
    expected = sorted(layer.read_bbox(box, columns=cp.MAP_COLUMNS).column("parcel_key").to_pylist())
    results: list[list[str]] = []
    errors: list[BaseException] = []

    def work() -> None:
        try:
            for _ in range(5):
                results.append(
                    sorted(layer.read_bbox(box, columns=cp.MAP_COLUMNS).column("parcel_key").to_pylist())
                )
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=work) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors and all(r == expected for r in results) and len(results) == 40
