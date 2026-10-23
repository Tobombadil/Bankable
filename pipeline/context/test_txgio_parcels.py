"""TxGIO land parcels (`pipeline/context/txgio_parcels.py`): the owner key, land-use categories, the
equal-area acreage, holdings of contiguous same-owner parcels, the privacy rule (no owner, care-of,
mailing or situs value in any output, summary, manifest, report or log), the catalogue plan, the county
zip reader, the manifest's carry-forward and gate, the dry run and the server's pull.

Fixtures (`pipeline/context/fixtures/txgio_parcels/`):

- `collections.json` and `resources.json`: the TxGIO catalogue's own answers
  (`api.tnris.org/api/v1/collections/?search=parcels` and the 2026 collection's resource list,
  retrieved 2026-10-23), trimmed to two vintages and ten files.
- `synthetic_county.geojson`: **synthetic**. TxGIO's county downloads answered HTTP 403 (CloudFront) to
  this egress and its map service has query switched off, so no real parcel was read. Sixteen parcels in
  TxGIO's schema beside the site finder fixture's line A, with invented owners ("SYNTH OWNER ..."),
  care-of names, situs and mailing addresses (no real person or place), laid out to exercise each rule:
  three Alpha parcels touching (one with "L.L.C.", one with doubled spaces and "L L C") and a fourth a
  kilometre away; Bravo's two parcels 20 m apart (one holding); Charlie's two 50 m apart (two); two
  adjacent parcels marked confidential and two with blank owners (never joined); Echo's "& SONS" and
  "and Sons"; Delta's 632-acre ranch; a self-crossing outline; and a record with no geometry.
- `stacked_outlines.geojson`: **synthetic** too ("SYNTH STACK ..." owners, invented addresses), the
  cases Texas rolls carry and TxGIO passes through unchanged: one owner's land account and mobile-home
  account on one outline (drawn from another vertex, the other way round); two owners on one outline
  (undivided interests); one owner's two outlines overlapping by half; another owner's lot drawn inside
  the first outline; a 30 m right-of-way strip; exempt land; adjacent lots whose owner is a placeholder
  ("NOT ON FILE", "VARIOUS", "R.O.W.") or withheld ("RESTRICTED"); and a kept name that looks like a
  placeholder ("SYNTH ROAD RUNNER RANCH").
"""

from __future__ import annotations

import json
import logging
import pathlib
import re
import warnings
import zipfile
from collections.abc import Iterator
from typing import Any

import numpy as np
import pyarrow.parquet as pq
import pytest

from pipeline.context import txgio_parcels as tp

FIX = pathlib.Path(__file__).parent / "fixtures" / "txgio_parcels"
FEATURES: list[dict[str, Any]] = json.loads((FIX / "synthetic_county.geojson").read_text(encoding="utf-8"))[
    "features"
]
STACKED: list[dict[str, Any]] = json.loads((FIX / "stacked_outlines.geojson").read_text(encoding="utf-8"))[
    "features"
]
PERSONAL_FIELDS = (
    "OWNER_NAME",
    "NAME_CARE",
    "SITUS_ADDR",
    "SITUS_CITY",
    "MAIL_ADDR",
    "MAIL_LINE1",
    "MAIL_CITY",
    "LEGAL_DESC",
)


def _personal_values(features: list[dict[str, Any]] | None = None) -> set[str]:
    """Every owner, care-of, situs, mailing and legal-description value in the fixture, and each owner's
    normalised key: none may appear in anything the build writes."""
    values: set[str] = set()
    for f in FEATURES if features is None else features:
        for name in PERSONAL_FIELDS:
            v = f["properties"].get(name)
            if v and str(v).strip() and "CONFIDENTIAL" not in str(v):
                values.add(str(v).strip().upper())
                key = tp.owner_key(v)
                if key:
                    values.add(key)
    # Word pairs that identify a synthetic owner even out of context ("OWNER ALPHA", "ECHO AND SONS").
    values |= {"OWNER ALPHA", "OWNER BRAVO", "OWNER CHARLIE", "OWNER DELTA", "OWNER ECHO", "OWNER FOXTROT"}
    values |= {"SYNTH SITUS", "SYNTH MAIL", "SYNTH CARE", "SYNTHVILLE"}
    return values


def _frame() -> tp.CountyFrame:
    frame = tp.frame_from_features(FEATURES, "48999", "Fixture")
    frame.source_url = "https://data.geographic.texas.gov/x/resources/stratmap26-landparcels_48999_lp.zip"
    frame.retrieved_at = "2026-10-23T09:00:00Z"
    return frame


@pytest.fixture()
def built(tmp_path: pathlib.Path) -> tuple[pathlib.Path, dict[str, Any]]:
    summary = tp.build_county(_frame(), tmp_path)
    return tmp_path, summary


def _parcels(out: pathlib.Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = pq.read_table(out / "parcels" / "48999.parquet").to_pylist()
    return rows


def _by_prop(out: pathlib.Path) -> dict[str, dict[str, Any]]:
    return {r["prop_id"]: r for r in _parcels(out)}


def _prop(key: str) -> str:
    """The fixture's property id of parcel `key` (A1 -> 50001 ...)."""
    for f in FEATURES:
        if str(f["properties"]["GEO_ID"]).endswith(f"-{key}"):
            return str(f["properties"]["PROP_ID"])
    raise KeyError(key)


# ------------------------------------------------------------------------------------- small parts
@pytest.mark.parametrize(
    ("raw", "key"),
    [
        ("SYNTH OWNER ALPHA LLC", "SYNTH OWNER ALPHA LLC"),
        ("Synth Owner Alpha, L.L.C.", "SYNTH OWNER ALPHA LLC"),
        ("SYNTH  OWNER ALPHA L L C", "SYNTH OWNER ALPHA LLC"),
        ("Synth Owner Echo & Sons", "SYNTH OWNER ECHO AND SONS"),
        ("Ñandú Ranch Incorporated", "NANDU RANCH INC"),
        ("SYNTH SMITH ET AL", "SYNTH SMITH ET AL"),
        ("", None),
        ("   ", None),
        (None, None),
        ("CONFIDENTIAL OWNER", None),
        ("** Confidential **", None),
        ("Unknown", None),
        ("OWNER UNKNOWN", None),
    ],
)
def test_owner_key_normalises_spelling_and_names_nobody_for_blank_or_withheld(
    raw: Any, key: str | None
) -> None:
    assert tp.owner_key(raw) == key


def test_co_owner_lists_stay_distinct_from_one_owner() -> None:
    assert tp.owner_key("SYNTH A AND SYNTH B") != tp.owner_key("SYNTH A")


@pytest.mark.parametrize(
    ("code", "category"),
    [
        ("A1", "residential"),
        ("B", "residential"),
        ("C1", "vacant"),
        ("C2", "vacant"),
        ("D1", "open_space"),
        ("d2", "open_space"),
        ("E", "rural"),
        ("E3", "rural"),
        ("F1", "commercial"),
        ("F2", "industrial"),
        ("G1", "minerals"),
        ("J4", "utilities"),
        ("M1", "mobile_homes"),
        ("X", "exempt"),
        ("", "not_stated"),
        (None, "not_stated"),
        ("Q9", "unknown"),
    ],
)
def test_land_use_codes_read_as_the_comptrollers_categories(code: Any, category: str) -> None:
    assert tp.land_use_category(code) == category
    assert category in tp.LAND_USE_LABELS


@pytest.mark.parametrize(("lat", "lon"), [(26.0, -97.5), (31.0, -98.0), (36.4, -101.5), (29.8, -104.4)])
def test_the_equal_area_projection_keeps_ellipsoidal_area(lat: float, lon: float) -> None:
    """A 0.01-degree quadrangle, its edges densified, against the closed-form area on GRS80."""
    import shapely

    n = 200
    xs = np.linspace(lon, lon + 0.01, n)
    ys = np.linspace(lat, lat + 0.01, n)
    ring = (
        [(x, lat) for x in xs]
        + [(lon + 0.01, y) for y in ys]
        + [(x, lat + 0.01) for x in xs[::-1]]
        + [(lon, y) for y in ys[::-1]]
    )
    poly = shapely.Polygon(ring)
    projected = shapely.transform(poly, tp.TEXAS_ALBERS.transform)
    exact = tp.ellipsoid_band_area_m2(lat, lat + 0.01, 0.01)
    assert shapely.area(projected) == pytest.approx(exact, rel=1e-6)


@pytest.mark.parametrize(
    ("value", "unit", "acres"),
    [
        (39.5, "Acres", 39.5),
        (39.5, "AC", 39.5),
        (43_560.0, "SqFt", 1.0),
        (43_560.0, "Square Feet", 1.0),
        (4046.8564224, "SqM", 1.0),
        (1.0, "Hectares", 2.4710538),
        (12.0, "furlongs", None),
        (None, "Acres", None),
        (0.0, "Acres", None),
    ],
)
def test_the_stated_gis_area_is_read_in_acres(value: Any, unit: Any, acres: float | None) -> None:
    got = tp.gis_acres(value, unit)
    assert got == (pytest.approx(acres, rel=1e-6) if acres is not None else None)


# --------------------------------------------------------------------------------------- holdings
def test_contiguous_parcels_of_one_owner_form_one_holding(built: tuple[pathlib.Path, dict[str, Any]]) -> None:
    out, summary = built
    rows = _by_prop(out)
    hid = {
        k: rows[_prop(k)]["holding_id"]
        for k in ("A1", "A2", "A3", "A4", "B1", "B2", "C1", "C2", "E1", "E2", "D1")
    }
    # Alpha's three touching parcels, spelt three ways, are one holding; the fourth, a kilometre away, is not.
    assert hid["A1"] == hid["A2"] == hid["A3"] != hid["A4"]
    # Bravo's parcels are 20 m apart (a road): one holding. Charlie's are 50 m apart: two.
    assert hid["B1"] == hid["B2"]
    assert hid["C1"] != hid["C2"]
    assert hid["E1"] == hid["E2"]
    alpha = rows[_prop("A1")]
    assert alpha["holding_parcels"] == 3
    assert alpha["holding_acres"] == pytest.approx(3 * 400 * 400 / tp.ACRE_M2, rel=0.01)
    assert rows[_prop("D1")]["holding_acres"] == pytest.approx(1600 * 1600 / tp.ACRE_M2, rel=0.01)
    assert summary["holdings"] == 12 and summary["parcels"] == 16 and summary["dropped_geometry"] == 1
    assert summary["multi_parcel_holdings"] == 3
    assert summary["largest_holding_acres"] == pytest.approx(632.1, abs=0.5)
    holdings = pq.read_table(out / "holdings" / "48999.parquet").to_pylist()
    assert {h["owner_count"] for h in holdings} == {1}
    assert holdings[0]["acres"] >= holdings[-1]["acres"]  # largest first
    by_id = {h["holding_id"]: h for h in holdings}
    assert by_id[hid["A1"]]["parcels"] == 3 and by_id[hid["A1"]]["land_use"] == "open_space"
    # The appraisal-record link of a holding is its largest parcel's property id.
    assert by_id[hid["D1"]]["link_prop_id"] == _prop("D1")


def test_confidential_and_blank_owners_never_join_and_are_not_marked(
    built: tuple[pathlib.Path, dict[str, Any]],
) -> None:
    out, summary = built
    rows = _by_prop(out)
    for a, b in (("X1", "X2"), ("Y1", "Y2")):
        ra, rb = rows[_prop(a)], rows[_prop(b)]
        assert ra["holding_id"] != rb["holding_id"]
        # A holding of one, like any other single-parcel holding: nothing says why it stands alone.
        assert ra["holding_parcels"] == rb["holding_parcels"] == 1
    columns = set(pq.read_schema(out / "parcels" / "48999.parquet").names)
    columns |= set(pq.read_schema(out / "holdings" / "48999.parquet").names)
    assert not {c for c in columns if re.search(r"owner_name|confidential|withheld|mail|situs|care|legal", c)}
    assert "confidential" not in json.dumps(summary).lower()


def test_acres_come_from_the_geometry_with_the_stated_gis_area_beside_them(
    built: tuple[pathlib.Path, dict[str, Any]],
) -> None:
    out, _ = built
    rows = _by_prop(out)
    a3 = rows[_prop("A3")]
    assert a3["acres"] == pytest.approx(400 * 400 / tp.ACRE_M2, rel=0.01)
    assert a3["gis_acres"] == pytest.approx(1_720_620 / 43_560, rel=1e-6)  # stated in square feet
    assert rows[_prop("B1")]["gis_acres"] is None
    # The self-crossing outline is repaired, not dropped: two triangles, half the square.
    assert rows[_prop("F1")]["acres"] == pytest.approx(0.5 * 400 * 400 / tp.ACRE_M2, rel=0.02)
    assert rows[_prop("Y2")]["land_use"] == "not_stated"


def test_holding_ids_are_random_per_build_and_never_derived_from_the_owner(tmp_path: pathlib.Path) -> None:
    first = tp.build_county(_frame(), tmp_path / "one")
    second = tp.build_county(_frame(), tmp_path / "two")
    a, b = _by_prop(tmp_path / "one"), _by_prop(tmp_path / "two")
    ids_a = {r["holding_id"] for r in a.values()}
    ids_b = {r["holding_id"] for r in b.values()}
    assert not ids_a & ids_b
    assert all(re.fullmatch(r"h[0-9a-f]{12}", i) for i in ids_a)

    # The same partition of parcels into holdings, whatever the ids.
    def partition(rows: dict[str, dict[str, Any]]) -> set[frozenset[str]]:
        groups: dict[str, set[str]] = {}
        for prop, r in rows.items():
            groups.setdefault(r["holding_id"], set()).add(prop)
        return {frozenset(g) for g in groups.values()}

    assert partition(a) == partition(b)
    assert first["holdings"] == second["holdings"]


def test_holding_ids_are_unique_even_when_the_random_source_repeats(tmp_path: pathlib.Path) -> None:
    draws: Iterator[str] = iter(["aaaaaaaaaaaa", "aaaaaaaaaaaa", *[f"{i:012x}" for i in range(1, 100)]])
    tp.build_county(_frame(), tmp_path, rng=lambda _n: next(draws))
    ids = {r["holding_id"] for r in _parcels(tmp_path)}
    assert len(ids) == 12


def test_no_owner_or_address_value_reaches_any_output_summary_or_log(
    tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    caplog.set_level(logging.DEBUG)
    plan = tp.parse_plan(
        json.loads((FIX / "collections.json").read_text(encoding="utf-8")),
        json.loads((FIX / "resources.json").read_text(encoding="utf-8")),
    )
    summary = tp.build_county(_frame(), tmp_path)
    (tmp_path / "counties").mkdir()
    (tmp_path / "counties" / "48999.json").write_text(json.dumps(summary), encoding="utf-8")
    manifest, _ = tp.build_manifest(tmp_path, plan, "v1/20261001-20261023T0900Z")
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    report = tp.report_markdown(manifest)
    texts = [report, capsys.readouterr().out, caplog.text]
    for path in sorted(tmp_path.rglob("*")):
        if path.suffix == ".parquet":
            pf = pq.ParquetFile(path)
            texts.append(json.dumps(pf.read().to_pylist(), default=str))
            texts.append(str(pf.schema_arrow.metadata))
        elif path.is_file():
            texts.append(path.read_bytes().decode("utf-8", "replace"))
    blob = "\n".join(texts).upper()
    leaked = sorted(v for v in _personal_values() if v.upper() in blob)
    assert not leaked, leaked


def test_a_county_of_another_state_is_refused(tmp_path: pathlib.Path) -> None:
    frame = _frame()
    frame.fips = "22019"
    with pytest.raises(tp.ParcelsError):
        tp.build_county(frame, tmp_path)


# ------------------------------------------------------------------------------------ the catalogue
def _plan() -> tp.Plan:
    return tp.parse_plan(
        json.loads((FIX / "collections.json").read_text(encoding="utf-8")),
        json.loads((FIX / "resources.json").read_text(encoding="utf-8")),
    )


def test_the_plan_takes_the_newest_vintage_and_its_county_zips() -> None:
    plan = _plan()
    assert plan.collection_id == "1d33dafb-6e9c-40ac-887d-f4577909ed9b"
    assert (plan.acquisition_date, plan.publication_date) == ("2026-09-25", "2026-10-01")
    assert plan.license_abbreviation == "CC0-1.0" and plan.spatial_reference == "4326"
    assert plan.excluded_note == "Donley"
    newton = plan.county("48351")
    assert newton.name == "Newton" and newton.bytes == 13946446
    assert newton.url.endswith("stratmap26-landparcels_48351_lp.zip")
    assert plan.statewide is not None and plan.statewide.bytes == 2714312383
    assert "48" not in {c.fips for c in plan.counties}
    assert tp.Plan.from_dict(plan.as_dict()).as_dict() == plan.as_dict()


def test_counties_are_chosen_by_fips_or_name() -> None:
    plan = _plan()
    assert [c.fips for c in tp.select_counties(plan, "48351, Pecos")] == ["48351", "48371"]
    assert len(tp.select_counties(plan, "all")) == len(plan.counties)
    with pytest.raises(tp.ParcelsError):
        tp.select_counties(plan, "Atlantis")
    with pytest.raises(tp.ParcelsError):
        tp.select_counties(plan, "48113")  # Dallas is not in the trimmed fixture


# ------------------------------------------------------------------------------- fake HTTP session
class _Resp:
    def __init__(self, status: int, content: bytes = b"", headers: dict[str, str] | None = None) -> None:
        self.status_code = status
        self.content = content
        self.headers = headers or {}

    def json(self) -> Any:
        return json.loads(self.content)

    def iter_content(self, chunk_size: int = 1) -> Iterator[bytes]:
        for i in range(0, len(self.content), chunk_size):
            yield self.content[i : i + chunk_size]

    def close(self) -> None:
        pass


class _Session:
    def __init__(self, routes: dict[str, _Resp]) -> None:
        self.routes = routes
        self.calls: list[tuple[str, str]] = []

    def get(self, url: str, **_: Any) -> _Resp:
        self.calls.append(("GET", url))
        return self.routes.get(url, _Resp(404))

    def request(self, method: str, url: str, **_: Any) -> _Resp:
        self.calls.append((method, url))
        return self.routes.get(url, _Resp(404))


def test_the_dry_run_asks_each_zip_its_size_and_downloads_nothing() -> None:
    plan = _plan()
    chosen = tp.select_counties(plan, "48351,48371")
    newton, pecos = chosen
    session = _Session(
        {
            newton.url: _Resp(200, headers={"Content-Length": str(newton.bytes)}),
            pecos.url: _Resp(403, headers={"Server": "CloudFront"}),
        }
    )
    checks = tp.check_files(session, chosen)
    assert [c["ok"] for c in checks] == [True, False]
    assert checks[1]["reason"] == "HTTP 403 (CloudFront)"
    assert {m for m, _ in session.calls} == {"HEAD"}
    report = tp.report_markdown({}, checks)
    assert "1 of 2" in report and "| Pecos | 48371 | HTTP 403 (CloudFront) |" in report


def test_a_download_is_checked_against_the_catalogue_size(tmp_path: pathlib.Path) -> None:
    county = tp.CountyFile("48999", "Fixture", "https://data.geographic.texas.gov/x/f.zip", 10)
    with pytest.raises(tp.ParcelsError, match="catalogue says 10"):
        tp.download(_Session({county.url: _Resp(200, b"PK\x03\x04short")}), county, tmp_path)
    assert not (tmp_path / "48999.zip").exists()
    with pytest.raises(tp.ParcelsError, match="HTTP 403"):
        tp.download(_Session({county.url: _Resp(403)}), county, tmp_path)


def test_a_failed_county_writes_its_reason_and_deletes_the_download(tmp_path: pathlib.Path) -> None:
    plan = _plan()
    newton = plan.county("48351")
    session = _Session({newton.url: _Resp(403, headers={"Server": "CloudFront"})})
    summary = tp.run_county(plan, "48351", tmp_path / "out", tmp_path / "work", session=session)
    assert summary["status"] == "failed" and "HTTP 403" in summary["reason"]
    written = json.loads((tmp_path / "out" / "counties" / "48351.json").read_text(encoding="utf-8"))
    assert written["status"] == "failed" and written["vintage"]["publication_date"] == "2026-10-01"
    assert not list((tmp_path / "work").glob("*.zip"))


# ----------------------------------------------------------------------------------- the zip reader
def _write_zip(tmp_path: pathlib.Path, *, gdb: bool) -> pathlib.Path:
    """The synthetic county written by GDAL the way TxGIO ships a county: a file geodatabase (full field
    names) and a shapefile (ten-character names), in one zip."""
    pyogrio = pytest.importorskip("pyogrio")
    import shapely

    rows = [f for f in FEATURES if f.get("geometry")]
    names = list(rows[0]["properties"])
    geoms = np.asarray([shapely.to_wkb(shapely.geometry.shape(f["geometry"])) for f in rows], dtype=object)
    data = []
    fields = []
    for name in names:
        if name == "OBJECTID":
            continue
        values = [f["properties"].get(name) for f in rows]
        if all(isinstance(v, (int, float)) or v is None for v in values) and any(
            v is not None for v in values
        ):
            data.append(np.asarray([np.nan if v is None else float(v) for v in values], dtype=np.float64))
        else:
            data.append(np.asarray(["" if v is None else str(v) for v in values], dtype=object))
        fields.append(name)
    stage = tmp_path / "stage"
    stage.mkdir()
    base = "stratmap26-landparcels_48999_lp"
    with warnings.catch_warnings():
        # GDAL truncates the shapefile's field names to ten characters, as in TxGIO's own zips.
        warnings.filterwarnings("ignore", "Normalized/laundered field name", RuntimeWarning)
        pyogrio.raw.write(
            str(stage / f"{base}.shp"),
            geoms,
            data,
            fields,
            driver="ESRI Shapefile",
            geometry_type="Polygon",
            crs="EPSG:4326",
        )
    if gdb:
        pyogrio.raw.write(
            str(stage / f"{base}.gdb"),
            geoms,
            data,
            fields,
            layer="stratmap26_landparcels_48999",
            driver="OpenFileGDB",
            geometry_type="Polygon",
            crs="EPSG:4326",
        )
    zpath = tmp_path / f"{base}.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        for path in sorted(stage.rglob("*")):
            if path.is_file():
                zf.write(path, path.relative_to(stage).as_posix())
    return zpath


@pytest.mark.parametrize("gdb", [True, False], ids=["geodatabase", "shapefile-only"])
def test_a_county_zip_is_read_with_the_columns_the_build_needs_only(
    tmp_path: pathlib.Path, gdb: bool
) -> None:
    zpath = _write_zip(tmp_path, gdb=gdb)
    frame = tp.read_county_zip(zpath, "48999", "Fixture")
    assert len(frame) == 16
    assert sorted(p for p in frame.prop_id if p) == sorted(
        str(f["properties"]["PROP_ID"]) for f in FEATURES if f.get("geometry")
    )
    lu = dict(zip(frame.prop_id, frame.land_use_code, strict=True))
    assert lu[_prop("C1")] == "F1"  # STAT_LAND_USE, or the shapefile's STAT_LAND_
    assert {tp.owner_key(o) for o in frame.owner} >= {"SYNTH OWNER ALPHA LLC", "SYNTH OWNER BRAVO", None}
    # The care-of, mailing, situs and legal columns are never read into memory.
    assert not {"name_care", "mail_addr", "situs_addr", "legal_desc"} & {a.lower() for a in vars(frame)}
    out = tmp_path / "out"
    summary = tp.build_county(frame, out)
    assert summary["holdings"] == 12


def test_run_county_builds_from_a_local_zip_and_removes_nothing_it_did_not_download(
    tmp_path: pathlib.Path,
) -> None:
    zpath = _write_zip(tmp_path, gdb=False)
    plan = _plan()
    summary = tp.run_county(
        plan,
        "48999",
        tmp_path / "out",
        tmp_path / "work",
        zip_path=zpath,
        retrieved_at="2026-10-23T10:00:00Z",
    )
    assert summary["status"] == "ok", summary.get("reason")
    assert summary["retrieved_at"] == "2026-10-23T10:00:00Z" and summary["zip_bytes"] == zpath.stat().st_size
    assert zpath.exists()
    files = summary["files"]
    for kind in ("parcels", "holdings"):
        assert (tmp_path / "out" / files[kind]["path"]).exists() and len(files[kind]["sha256"]) == 64


def test_parcels_are_geoparquet_with_a_bbox_covering_in_z_order(
    built: tuple[pathlib.Path, dict[str, Any]],
) -> None:
    out, _ = built
    pf = pq.ParquetFile(out / "parcels" / "48999.parquet")
    geo = json.loads(pf.schema_arrow.metadata[b"geo"])
    assert geo["version"] == "1.1.0"
    assert geo["columns"]["geometry"]["covering"]["bbox"]["xmin"] == ["bbox", "xmin"]
    table = pf.read()
    assert table.column("source_id").to_pylist()[0] == tp.SOURCE_ID
    assert set(table.column("licence").to_pylist()) == {tp.LICENCE}


# ------------------------------------------------------------------------------------- the manifest
def _county_summary(fips: str, name: str, parcels: int, status: str = "ok") -> dict[str, Any]:
    doc: dict[str, Any] = {"fips": fips, "name": name, "status": status}
    if status == "ok":
        doc.update(
            parcels=parcels,
            holdings=parcels // 2,
            largest_holding_acres=640.0,
            files={
                "parcels": {"path": f"parcels/{fips}.parquet", "sha256": "a" * 64, "bytes": 1},
                "holdings": {"path": f"holdings/{fips}.parquet", "sha256": "b" * 64, "bytes": 1},
            },
            seconds={"total": 1.0},
        )
    else:
        doc["reason"] = "ParcelsError: https://data.geographic.texas.gov/x answered HTTP 403"
    return doc


def _write_summaries(out: pathlib.Path, docs: list[dict[str, Any]]) -> None:
    (out / "counties").mkdir(parents=True, exist_ok=True)
    for doc in docs:
        (out / "counties" / f"{doc['fips']}.json").write_text(json.dumps(doc), encoding="utf-8")


def test_the_manifest_carries_failed_counties_forward_and_refuses_to_lose_one(tmp_path: pathlib.Path) -> None:
    plan = _plan()
    previous = {
        "version": "v1/20251001-20251101T0000Z",
        "counties": {
            "48351": {**_county_summary("48351", "Newton", 1000), "version": "v1/20251001-20251101T0000Z"},
            "48371": {**_county_summary("48371", "Pecos", 800), "version": "v1/20251001-20251101T0000Z"},
            "48475": {**_county_summary("48475", "Ward", 500), "version": "v1/20251001-20251101T0000Z"},
        },
    }
    _write_summaries(
        tmp_path, [_county_summary("48351", "Newton", 1100), _county_summary("48371", "Pecos", 0, "failed")]
    )
    manifest, problems = tp.build_manifest(tmp_path, plan, "v1/20261001-20261023T0900Z", previous=previous)
    assert manifest["counties"]["48351"]["version"] == "v1/20261001-20261023T0900Z"
    # Pecos failed: its published entry stays, under its own version, and the failure is listed.
    assert manifest["counties"]["48371"]["carried_forward"] is True
    assert manifest["counties"]["48371"]["version"] == "v1/20251001-20251101T0000Z"
    assert [f["fips"] for f in manifest["failures"]] == ["48371"]
    # Ward was published and not built: a full build may not drop it silently.
    assert problems == ["48475 (Ward) is published but was not built; allow it to drop explicitly"]
    _, problems = tp.build_manifest(
        tmp_path, plan, "v1/20261001-20261023T0900Z", previous=previous, allow_dropped=["48475"]
    )
    assert problems == []
    partial, problems = tp.build_manifest(
        tmp_path, plan, "v1/20261001-20261023T0900Z", previous=previous, partial=True
    )
    assert problems == [] and partial["counties"]["48475"]["carried_forward"] is True


def test_the_manifest_refuses_a_county_that_lost_more_than_half_its_parcels(tmp_path: pathlib.Path) -> None:
    previous = {"counties": {"48351": {**_county_summary("48351", "Newton", 1000), "version": "v1/x-1"}}}
    _write_summaries(tmp_path, [_county_summary("48351", "Newton", 400)])
    _, problems = tp.build_manifest(tmp_path, _plan(), "v1/20261001-20261023T0900Z", previous=previous)
    assert problems == ["48351 (Newton): parcels fell from 1000 to 400"]


def test_a_county_whose_job_never_reported_is_a_failure(tmp_path: pathlib.Path) -> None:
    _write_summaries(tmp_path, [_county_summary("48351", "Newton", 10)])
    manifest, problems = tp.build_manifest(
        tmp_path, _plan(), "v1/20261001-20261023T0900Z", expected=["48351", "48241"]
    )
    assert [f["fips"] for f in manifest["failures"]] == ["48241"] and problems == []
    report = tp.report_markdown(manifest)
    assert "| 48241 | no summary: the county job did not finish |" in report
    assert "| Newton | 48351 | 10 |" in report


# ------------------------------------------------------------------------------------------ the pull
class _FakeS3:
    """The two calls `pull` makes of a boto3 S3 client, answered from a dict of object keys."""

    def __init__(self, objects: dict[str, bytes]) -> None:
        self.objects = objects
        self.calls: list[tuple[str, str, str]] = []

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803 - boto3's names
        self.calls.append(("get_object", Bucket, Key))
        if Key not in self.objects:
            raise KeyError(f"NoSuchKey: {Key}")

        class Body:
            def __init__(self, data: bytes) -> None:
                self.data = data

            def read(self) -> bytes:
                return self.data

        return {"Body": Body(self.objects[Key])}

    def download_file(self, bucket: str, key: str, filename: str) -> None:
        self.calls.append(("download_file", bucket, key))
        if key not in self.objects:
            raise KeyError(f"NoSuchKey: {key}")
        pathlib.Path(filename).write_bytes(self.objects[key])


def test_pull_reads_the_private_object_store_checks_every_file_and_writes_current_last(
    tmp_path: pathlib.Path, built: tuple[pathlib.Path, dict[str, Any]]
) -> None:
    out, summary = built
    version = "v1/20261001-20261023T0900Z"
    prefix = tp.OBJECT_PREFIX
    assert prefix == "data/normalized/context/us.tx.txgio.parcels"  # the private bucket's data prefix
    latest = {"version": version, "counties": {"48999": {**summary, "version": version}}}
    objects = {f"{prefix}/latest.json": json.dumps(latest).encode()}
    for kind in ("parcels", "holdings"):
        rel = summary["files"][kind]["path"]
        objects[f"{prefix}/{version}/{rel}"] = (out / rel).read_bytes()
    client = _FakeS3(objects)
    dest = tmp_path / "server"
    result = tp.pull(client=client, bucket="objects", counties={"48999", "48351"}, out_dir=dest)
    assert (
        result["status"] == "pulled" and result["files_fetched"] == 2 and result["not_published"] == ["48351"]
    )
    assert {b for _, b, _ in client.calls} == {"objects"}
    assert all(k.startswith(prefix + "/") for _, _, k in client.calls)
    current = json.loads((dest / tp.CURRENT_NAME).read_text(encoding="utf-8"))
    assert current["version"] == version and set(current["counties"]) == {"48999"}
    assert current["object_prefix"] == prefix and "base_url" not in current
    local = dest / current["counties"]["48999"]["files"]["parcels"]["local"]
    assert local.read_bytes() == (out / "parcels" / "48999.parquet").read_bytes()
    again = tp.pull(client=client, bucket="objects", counties={"48999", "48351"}, out_dir=dest)
    assert again["status"] == "unchanged"
    # A file that does not match its sha256 stops the pull before current.json moves.
    objects[f"{prefix}/{version}/holdings/48999.parquet"] = b"tampered"
    (dest / tp.CURRENT_NAME).unlink()
    (dest / version / "holdings" / "48999.parquet").unlink()
    with pytest.raises(tp.ParcelsError, match="sha256"):
        tp.pull(client=client, bucket="objects", counties=None, out_dir=dest)
    assert not (dest / tp.CURRENT_NAME).exists()
    assert not list((dest / version).rglob("*.part"))
    # A missing object names its key, not a public URL.
    with pytest.raises(tp.ParcelsError, match=r"latest\.json"):
        tp.pull(client=_FakeS3({}), bucket="objects", counties=None, out_dir=dest)


def test_the_pull_takes_the_object_store_credentials_and_has_no_public_route() -> None:
    with pytest.raises(tp.ParcelsError, match=r"R2_BUCKET.*R2_ENDPOINT or R2_ACCOUNT_ID"):
        tp.object_store_client({"R2_ACCESS_KEY_ID": "k", "R2_SECRET_ACCESS_KEY": "s"})
    # No setting, constant or argument points the pull at the public tiles host any more.
    assert not hasattr(tp, "default_base_url") and not hasattr(tp, "TILES_PREFIX")
    import inspect

    assert "base_url" not in inspect.signature(tp.pull).parameters
    source = pathlib.Path(tp.__file__).read_text(encoding="utf-8")
    assert "PARCELS_LAYER_URL" not in source and "tiles." not in source


def test_a_server_must_say_which_counties_it_serves() -> None:
    with pytest.raises(tp.ParcelsError):
        tp.wanted_counties("")
    assert tp.wanted_counties("all") is None
    assert tp.wanted_counties("48351, 48241") == {"48351", "48241"}
    with pytest.raises(tp.ParcelsError):
        tp.wanted_counties("22019")


def test_the_cli_plans_in_batches_from_recorded_answers(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    tp.main(
        [
            "plan",
            "--catalogue",
            str(FIX / "collections.json"),
            "--resources",
            str(FIX / "resources.json"),
            "--out",
            str(tmp_path / "plan.json"),
            "--batches",
            "3",
        ]
    )
    batches = json.loads(capsys.readouterr().out)
    assert len(batches) == 3 and sum(len(b.split(",")) for b in batches) == len(_plan().counties)
    assert (
        json.loads((tmp_path / "plan.json").read_text(encoding="utf-8"))["publication_date"] == "2026-10-01"
    )


# ------------------------------------------------------------------------------- the owner screen
@pytest.mark.parametrize(
    "raw",
    [
        "NOT ON FILE",
        "Name Not On File",
        "No Name",
        "VARIOUS",
        "Various Owners",
        "ROW",
        "R.O.W.",
        "Right of Way",
        "PUBLIC ROAD",
        "SEE ACCOUNT 12345",
        "See Acct R1234",
        "SAME AS R1234",
        "12345",
        "UNKNOWN HEIRS OF SYNTH ESTATE",
        "OWNER",
    ],
)
def test_placeholder_owners_name_nobody(raw: str) -> None:
    """Two unrelated neighbours recorded as "NOT ON FILE" (or "VARIOUS", "ROW", "SEE ACCT ...") must
    never become one holding with one owner."""
    assert tp.owner_screen(raw) == (None, "placeholder")
    assert tp.owner_key(raw) is None


@pytest.mark.parametrize(
    "raw",
    [
        "CONFIDENTIAL OWNER",
        "Confidental",
        "CONFID",
        "25.025",
        "§25.025 ELECTION",
        "RESTRICTED",
        "Owner Protected",
        "PRIVATE",
        "Private Owner",
        "WITHHELD",
        "SYNTH OWNER - REDACTED",
    ],
)
def test_confidentiality_markers_are_withheld(raw: str) -> None:
    assert tp.owner_screen(raw) == (None, "withheld")


def test_names_with_marker_words_inside_stay_names_and_are_counted_as_suspect() -> None:
    assert tp.owner_screen("SYNTH PRIVATE EQUITY LP") == ("SYNTH PRIVATE EQUITY LP", "named")
    assert tp.owner_screen("SYNTH ROAD RUNNER RANCH") == ("SYNTH ROAD RUNNER RANCH", "named")
    assert tp.owner_suspect("SYNTH ROAD RUNNER RANCH") and tp.owner_suspect("SYNTH PRIVATE EQUITY LP")
    assert not tp.owner_suspect("SYNTH OWNER ALPHA LLC")
    assert tp.owner_screen("") == (None, "blank") and tp.owner_screen(None) == (None, "blank")


def test_the_county_frame_never_shows_an_owner_in_its_repr() -> None:
    frame = _frame()
    text = repr(frame) + str(frame)
    assert "owner=" not in text
    assert not [v for v in _personal_values() if v in text.upper()]


def test_the_parcel_file_keeps_only_the_listed_fields(built: tuple[pathlib.Path, dict[str, Any]]) -> None:
    """Spec: TxGIO parcel id, county FIPS, the district's property id, acres (computed and stated), land
    use code and category, land and market value with the tax year, the holding's figures, the outline,
    and the provenance quartet. No geographic id: it is not on the list."""
    out, _ = built
    names = set(pq.read_schema(out / "parcels" / "48999.parquet").names)
    assert names == {
        "parcel_key",
        "txgio_id",
        "fips",
        "prop_id",
        "acres",
        "gis_acres",
        "land_use_code",
        "land_use",
        "land_value",
        "market_value",
        "tax_year",
        "holding_id",
        "holding_acres",
        "holding_parcels",
        "holding_land_use",
        "holding_value_per_acre",
        "holding_link_prop_id",
        "holding_kind",
        "holding_overlaps",
        "holding_overlap_rest_acres",
        "geometry",
        "bbox",
        "source_id",
        "source_url",
        "retrieved_at",
        "licence",
    }
    holding_names = set(pq.read_schema(out / "holdings" / "48999.parquet").names)
    assert not {n for n in holding_names | names if "geo_id" in n}
    assert "GEO_ID" not in tp.FIELD_NAMES and "geo_id" not in tp.FIELD_NAMES


# --------------------------------------------------------------------- stacked and overlapping outlines
def _stacked(
    tmp_path: pathlib.Path,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
    """The stacked fixture built: (summary, parcel rows by fixture key, holding rows by holding id, and the
    eight placeholder or withheld lots, which share one key)."""
    frame = tp.frame_from_features(STACKED, "48999", "Fixture")
    frame.source_url = "https://data.geographic.texas.gov/x/resources/stratmap26-landparcels_48999_lp.zip"
    frame.retrieved_at = "2026-10-23T09:00:00Z"
    summary = tp.build_county(frame, tmp_path)
    by_prop = {r["prop_id"]: r for r in _parcels(tmp_path)}
    rows: dict[str, dict[str, Any]] = {}
    lots: list[dict[str, Any]] = []
    for f in STACKED:
        key = str(f["properties"]["GEO_ID"]).split("-", 1)[1]
        row = by_prop[str(f["properties"]["PROP_ID"])]
        if key == "lot":
            lots.append(row)
        else:
            rows[key] = row
    holdings = {
        h["holding_id"]: h for h in pq.read_table(tmp_path / "holdings" / "48999.parquet").to_pylist()
    }
    return summary, rows, holdings, lots


#: The fixture's squares: 0.01 degree on a side from 30.50 N (about 263 acres on GRS80).
SQUARE_ACRES = tp.ellipsoid_band_area_m2(30.50, 30.51, 0.01) / tp.ACRE_M2


def test_accounts_stacked_on_one_outline_are_one_tract(tmp_path: pathlib.Path) -> None:
    """One owner's land account and mobile-home account on one outline: one holding of one parcel (two
    accounts), its acres the outline's once, its land use the land's, its link the land account."""
    _, rows, holdings, _ = _stacked(tmp_path)
    land, home = rows["S1-land"], rows["S1-home"]
    assert land["holding_id"] == home["holding_id"]
    h = holdings[land["holding_id"]]
    assert h["parcels"] == 1 and h["accounts"] == 2
    assert h["acres"] == pytest.approx(SQUARE_ACRES, rel=0.002)  # not twice the square
    assert h["land_use"] == "open_space" and h["link_prop_id"] == land["prop_id"]
    assert land["holding_parcels"] == 1 and land["holding_acres"] == pytest.approx(SQUARE_ACRES, rel=0.002)
    # Value per acre: both accounts' values over the tract's acres once.
    assert h["value_per_acre"] == round((300000 + 40000) / h["acres"])


def test_two_owners_on_one_outline_share_it_and_say_so(tmp_path: pathlib.Path) -> None:
    """Undivided interests (or a mobile home on someone's land): two holdings of one owner each, each the
    whole outline, each listing the other as sharing all of it, so no assemblage counts it twice."""
    _, rows, holdings, _ = _stacked(tmp_path)
    a, b = rows["S2-a"], rows["S2-b"]
    assert a["holding_id"] != b["holding_id"]
    for one, other in ((a, b), (b, a)):
        assert holdings[one["holding_id"]]["acres"] == pytest.approx(SQUARE_ACRES, rel=0.002)
        assert holdings[one["holding_id"]]["overlap_acres"] == pytest.approx(SQUARE_ACRES, rel=0.002)
        shared = {e["holding_id"]: e["acres"] for e in one["holding_overlaps"]}
        assert shared[other["holding_id"]] == pytest.approx(SQUARE_ACRES, rel=0.002)
        assert one["holding_overlap_rest_acres"] == 0.0


def test_overlapping_outlines_of_one_owner_count_as_their_union(tmp_path: pathlib.Path) -> None:
    _, rows, holdings, _ = _stacked(tmp_path)
    first, second = rows["S3"], rows["S3-shifted"]
    assert first["holding_id"] == second["holding_id"]
    h = holdings[first["holding_id"]]
    assert h["parcels"] == 2 and h["acres"] == pytest.approx(1.5 * SQUARE_ACRES, rel=0.005)


def test_a_lot_drawn_inside_another_owners_outline_is_shared_land(tmp_path: pathlib.Path) -> None:
    _, rows, holdings, _ = _stacked(tmp_path)
    inner, outer = rows["S1-inner"], rows["S1-land"]
    lot = 0.04 * SQUARE_ACRES
    assert holdings[inner["holding_id"]]["acres"] == pytest.approx(lot, rel=0.01)
    assert {e["holding_id"]: e["acres"] for e in inner["holding_overlaps"]} == {
        outer["holding_id"]: pytest.approx(lot, rel=0.01)
    }
    assert holdings[outer["holding_id"]]["overlap_acres"] == pytest.approx(lot, rel=0.01)


def test_the_county_summary_counts_stacked_and_overlapping_outlines(tmp_path: pathlib.Path) -> None:
    summary, _, _, _ = _stacked(tmp_path)
    assert summary["parcels"] == 19 and summary["holdings"] == 16
    assert summary["outlines"] == {
        "distinct": 17,
        "accounts_on_a_repeated_outline": 2,
        "repeats_within_a_holding": 1,
        "outlines_shared_by_holdings": 1,
        "overlapping_pairs": 2,
        "overlapping_pairs_over_10_percent": 2,
        "overlap_acres": pytest.approx(0.5 * SQUARE_ACRES + 0.04 * SQUARE_ACRES, abs=0.5),
        "holdings_measured_as_union": 1,
        "holdings_overlapping_another": 4,
    }
    # The county's acres count each outline once.
    assert summary["acres"] < sum(r["acres"] for r in _parcels(tmp_path))
    assert summary["largest_holding_acres"] == pytest.approx(1.5 * SQUARE_ACRES, abs=1.0)


def test_right_of_way_strips_and_exempt_land_are_kinds_apart(tmp_path: pathlib.Path) -> None:
    summary, rows, holdings, _ = _stacked(tmp_path)
    strip = holdings[rows["ROW"]["holding_id"]]
    assert strip["kind"] == "strip" and strip["acres"] >= tp.STRIP_MIN_ACRES
    assert strip["mean_width_m"] == pytest.approx(30.0, abs=1.5)
    assert holdings[rows["X"]["holding_id"]]["kind"] == "exempt"
    assert rows["ROW"]["holding_kind"] == "strip" and rows["S1-land"]["holding_kind"] == "land"
    assert summary["holding_kinds"] == {"land": 14, "strip": 1, "exempt": 1, "utility": 0}


def test_placeholder_and_withheld_owners_never_join_and_are_counted_only(tmp_path: pathlib.Path) -> None:
    summary, rows, holdings, lots = _stacked(tmp_path)
    assert len(lots) == 8 and len({r["holding_id"] for r in lots}) == 8
    assert all(holdings[r["holding_id"]]["parcels"] == 1 for r in lots)
    suspects = [rows["suspect-1"], rows["suspect-2"]]
    assert len({r["holding_id"] for r in suspects}) == 1  # a kept name still groups
    assert summary["owners"] == {
        "named": 11,
        "blank": 0,
        "withheld": 2,
        "placeholder": 6,
        "suspect_kept": 2,
        "suspect_in_multi_parcel_holdings": 1,
    }


def test_no_value_of_the_stacked_fixture_reaches_any_output(tmp_path: pathlib.Path) -> None:
    summary, _, _, _ = _stacked(tmp_path)
    texts = [json.dumps(summary)]
    for path in sorted(tmp_path.rglob("*.parquet")):
        pf = pq.ParquetFile(path)
        texts.append(json.dumps(pf.read().to_pylist(), default=str))
        texts.append(str(pf.schema_arrow.metadata))
    blob = "\n".join(texts).upper()
    values = _personal_values(STACKED) | {"NOT ON FILE", "VARIOUS", "R.O.W.", "RESTRICTED", "ROAD RUNNER"}
    values |= {str(f["properties"]["GEO_ID"]).upper() for f in STACKED}
    assert not sorted(v for v in values if v.upper() in blob)
