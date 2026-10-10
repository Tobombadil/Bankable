"""The hand-check generator and scorer (`services/sites/handcheck.py`, `handcheck_stats.py`): the
statistics, the strata, reading a store through the served view only (no hidden source, no natural
person, no unpublished record id), the workbook's shape, and the scorer on a synthetic filled sheet."""

from __future__ import annotations

import csv
import datetime as dt
import itertools
import json
import pathlib
from collections import Counter
from collections.abc import Iterator

import pytest
from openpyxl import load_workbook
from sqlalchemy.orm import Session, sessionmaker

from services.db.models import (
    InterconnectionPoint,
    Licence,
    Location,
    Organization,
    Proposal,
    ProposalSource,
    Source,
)
from services.db.session import get_engine, get_sessionmaker, init_db
from services.ids import public_id, slugify
from services.sites import build, handcheck
from services.sites import handcheck_stats as stats
from services.sites.handcheck import (
    H_ITEM,
    H_MERGED_KIND,
    H_MERGED_VERDICT,
    H_NOTES,
    H_SITE_PROBLEM,
    H_SITE_VERDICT,
    Frame,
    Link,
    Member,
    MergedItem,
    SiteItem,
)
from tests.db_template import disposing, fresh_engine

UTC = dt.UTC
T0 = dt.datetime(2026, 10, 1, tzinfo=UTC)
NOW = dt.datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
CAISO, EIA, PJM, DERIVED = "us.iso.caiso.gen_queue", "us.eia.860m", "us.iso.pjm.queue", "us.test.derived_only"
_seq = itertools.count(1)


# ----------------------------------------------------------------------------------- statistics
def test_wilson_known_values() -> None:
    lo, hi = stats.wilson(0, 50)
    assert lo == 0.0 and hi == pytest.approx(0.0713, abs=5e-4)
    lo, hi = stats.wilson(5, 50)
    assert (lo, hi) == (pytest.approx(0.0435, abs=5e-4), pytest.approx(0.2136, abs=5e-4))
    assert stats.wilson(0, 0) == (0.0, 1.0)
    assert stats.wilson(50, 50)[1] == 1.0


def test_decisive_counts_for_fifty() -> None:
    """With 50 judged: 0 wrong keeps the interval under 10 %; 6 puts the point above; 10 the lower bound."""
    assert stats.decisive_counts(50) == (0, 6, 10)


def test_allocate_certainty_floor_cap_and_total() -> None:
    pops = {"largest": 5, "a/high": 400, "b/high": 100, "c/low": 9, "d/medium": 1}
    sizes = stats.allocate(pops, 50, minimum=2, certainty=("largest",))
    assert sizes["largest"] == 5
    assert sum(sizes.values()) == 50
    assert sizes["d/medium"] == 1  # capped at its population
    assert sizes["c/low"] >= 2
    assert sizes["a/high"] > sizes["b/high"] > sizes["c/low"]
    # Square root, not proportional: 400 vs 100 gets about twice, not four times.
    assert 1.5 <= sizes["a/high"] / sizes["b/high"] <= 2.5


def test_allocate_takes_everything_when_small_and_floors_when_tight() -> None:
    assert stats.allocate({"a": 3, "b": 4}, 50) == {"a": 3, "b": 4}
    tight = stats.allocate({"a": 100, "b": 50, "c": 10}, 4, minimum=2)
    assert sum(tight.values()) == 4 and tight["a"] == 2 and tight["b"] == 2


def test_prn_is_deterministic_and_coordinates_across_stores() -> None:
    assert stats.prn(7, "us.eia.860m:69798-PMN1") == stats.prn(7, "us.eia.860m:69798-PMN1")
    assert stats.prn(7, "x") != stats.prn(8, "x")
    assert all(0.0 <= stats.prn(s, "k") < 1.0 for s in range(50))
    # The same real-world items under different public ids draw the same members.
    keys = [f"{CAISO}:Q{i}" for i in range(40)]
    one = [(f"prop_a{i}", k) for i, k in enumerate(keys)]
    two = [(f"prop_b{i}", k) for i, k in enumerate(reversed(keys))]

    def pick(items: list[tuple[str, str]]) -> set[str]:
        return {k for _p, k in handcheck.draw(items, lambda _i: "h", lambda i: i[1], {"h": 5}, 3)}

    assert pick(one) == pick(two)


def test_estimate_weighted_and_unweighted() -> None:
    """Stratum A (N=900): 1 error in 10 judged; stratum B (N=100): 5 errors in 10. Unweighted 6/20 =
    30 %; weighted 0.9 x 10 % + 0.1 x 50 % = 14 %. An unsure and a blank are counted, not judged."""
    items = [stats.Judged("A", 900, 11, i == 0) for i in range(10)]
    items += [stats.Judged("B", 100, 11, i < 5) for i in range(10)]
    items += [stats.Judged("A", 900, 11, None, unsure=True), stats.Judged("B", 100, 11, None)]
    e = stats.estimate(items)
    assert (e.sampled, e.judged, e.errors, e.unsure, e.blank) == (22, 20, 6, 1, 1)
    assert e.rate == pytest.approx(0.30)
    assert e.weighted_rate == pytest.approx(0.14)
    # Kish: weights 90 x10 and 10 x10 -> (1000)^2 / (81000 + 1000) = 12.2
    assert e.effective_n == pytest.approx(1000**2 / 82000)
    assert e.weighted_interval[0] < 0.14 < e.weighted_interval[1]
    assert e.covered == pytest.approx(1.0)


# ------------------------------------------------------------------------------- categories
def _link(source: str, record: str, name: str | None = None, mw: float | None = 100.0) -> Link:
    return Link(
        source, source, f"{source}:{record}", record, name, mw, "solar", "filed", "request", "https://x"
    )


def test_merged_flags_cover_each_category() -> None:
    assert handcheck.merged_flags([_link(EIA, "100-1", "A"), _link(EIA, "200-1", "A")]) == ("multi_plant",)
    assert handcheck.merged_flags([_link(CAISO, "Q1", "Ridge"), _link(CAISO, "Q2", "Ridge")]) == (
        "multi_request",
    )
    assert handcheck.merged_flags([_link(CAISO, "Q1", "Darden"), _link(EIA, "69661-1", "Darden IV")]) == (
        "phase_markers",
        "cross_register",
    )
    assert handcheck.merged_flags([_link(EIA, "300-1", "Big"), _link(EIA, "300-2", "Big")]) == (
        "multi_generator",
    )
    assert handcheck.merged_flags([_link(CAISO, "Q1", "Kings"), _link(EIA, "400-1", "Kings")]) == (
        "cross_register",
    )


# ---------------------------------------------------------------------------------- a store
@pytest.fixture()
def factory() -> Iterator[sessionmaker[Session]]:
    with disposing(fresh_engine()) as engine:
        yield get_sessionmaker(engine)


def _licence(s: Session, lid: str, reuse: str, raw: bool) -> None:
    s.add(
        Licence(
            id=lid,
            name=lid,
            reuse_class=reuse,
            allows_derived_publication=True,
            allows_raw_publication=raw,
            allows_api_redistribution=True,
            allows_bulk_export=True,
            allows_commercial_use=True,
            gate_flag=False,
            evidence_url="https://example.org/terms",
            evidence_retrieved_at=T0,
            classified_by="legal-compliance",
        )
    )


def registry(s: Session) -> None:
    _licence(s, "open", "open", True)
    _licence(s, "derived", "attribution", False)
    s.flush()
    for sid, name, licence, state in (
        (CAISO, "CAISO interconnection queue", "open", "public"),
        (EIA, "EIA-860M", "open", "public"),
        (PJM, "PJM queue", "open", "ingest_only"),
        (DERIVED, "Derived-only register", "derived", "public"),
    ):
        s.add(
            Source(
                id=sid,
                name=name,
                category="generation_queue",
                jurisdiction="US",
                operator="Test",
                url=f"https://example.org/{sid}",
                access="bulk_file",
                cadence="weekly",
                licence_id=licence,
                publish_state=state,
                manifest_version="2026-09-12",
                manifest_hash="0" * 64,
            )
        )
    s.flush()


def org(s: Session, name: str, *, person: bool = False) -> Organization:
    o = Organization(
        public_id="",
        slug="",
        name_canonical=name,
        name_normalised=name.lower(),
        type="developer",
        country="US",
        personal_data=person,
    )
    s.add(o)
    s.flush()
    o.public_id = public_id("org", o.id)
    o.slug = slugify(name) + f"-{next(_seq)}"
    s.flush()
    return o


def point(s: Session, name: str) -> InterconnectionPoint:
    n = next(_seq)
    pt = InterconnectionPoint(
        public_id=f"poi_{n:010d}",
        operator="CAISO",
        name_display=name,
        name_key=f"k{n}",
        key_rule="t",
        kind="substation",
        source_id=CAISO,
        source_url="https://example.org/q",
        retrieved_at=T0,
        licence_id="open",
    )
    s.add(pt)
    s.flush()
    return pt


def exact(s: Session, lon: float = -101.5, lat: float = 35.2) -> Location:
    loc = Location(
        kind="point",
        geom=(lon, lat),
        precision="exact",
        country="US",
        source_id=EIA,
        source_url="https://example.org/eia",
        retrieved_at=T0,
        licence_id="open",
    )
    s.add(loc)
    s.flush()
    return loc


def prop(
    s: Session,
    name: str,
    links: list[tuple[str, str, str]],
    *,
    mw: float = 100.0,
    state: str = "filed",
    technology: str = "solar",
    sponsor: Organization | None = None,
    poi: InterconnectionPoint | None = None,
    location: Location | None = None,
) -> Proposal:
    """`links`: (source id, source record id, the link's own name)."""
    n = next(_seq)
    p = Proposal(
        public_id="",
        slug="",
        kind="generation",
        name_canonical=name,
        technology=technology,
        capacity_mw=mw,
        jurisdiction="US-CA",
        lifecycle_state=state,
        publish_state="public",
        published_at=T0,
        public_at=T0,
        min_reuse_class="open",
        source_count=len(links),
        sponsor_org_id=sponsor.id if sponsor else None,
        interconnection_point_id=poi.id if poi else None,
        location_id=location.id if location else None,
    )
    s.add(p)
    s.flush()
    p.public_id = public_id("prop", p.id)
    p.slug = f"p-{n}"
    for source_id, record, link_name in links:
        s.add(
            ProposalSource(
                proposal_id=p.id,
                source_id=source_id,
                source_record_id=record,
                # A derived-only register links its landing page, not the row (the id is not printed).
                source_url=f"https://example.org/{source_id}"
                + ("" if source_id == DERIVED else "/" + record),
                retrieved_at=T0,
                licence_id="derived" if source_id == DERIVED else "open",
                raw={},
                normalised={"name_canonical": link_name, "capacity_mw": mw, "lifecycle_state": state},
            )
        )
    s.flush()
    return p


def populate(s: Session) -> dict[str, Proposal]:
    """Sites: Darden I/II at one POI (poi_sponsor) plus a hidden PJM row that groups with them; a
    plant of four units; a site whose sponsor is a natural person; a site with one visible member.
    Merged records: two registers, two plants, two requests, a derived-only link, a hidden second
    link, a person sponsor."""
    registry(s)
    acme, solaris = org(s, "Acme Renewables LLC"), org(s, "Solaris Power Inc")
    person = org(s, "Jonathan Quillfeather", person=True)
    sub = point(s, "Darden 500kV")
    out: dict[str, Proposal] = {}
    out["darden1"] = prop(s, "Darden I Solar", [(CAISO, "Q1949", "Darden I")], mw=300, sponsor=acme, poi=sub)
    out["darden2"] = prop(
        s, "Darden II Solar", [(CAISO, "Q1950", "Darden II")], mw=200, sponsor=acme, poi=sub
    )
    out["hidden"] = prop(
        s, "Secret Darden III", [(PJM, "AB1-001", "Secret Darden III")], sponsor=acme, poi=sub
    )
    loc = exact(s)
    for i in range(1, 5):
        out[f"unit{i}"] = prop(
            s,
            "Matador Gas",
            [(EIA, f"69799-G{i}", "Matador Gas")],
            mw=260 if i == 1 else 40,
            technology="gas_ct",
            sponsor=solaris,
            location=loc,
        )
    far = point(s, "Quill 230kV")
    out["person1"] = prop(
        s, "Quill Ranch Solar I", [(CAISO, "Q7001", "Quill Ranch I")], sponsor=person, poi=far
    )
    out["person2"] = prop(
        s, "Quill Ranch Solar II", [(CAISO, "Q7002", "Quill Ranch II")], sponsor=person, poi=far
    )
    lone = point(s, "Lone 115kV")
    out["lone"] = prop(s, "Lone Mesa Solar", [(CAISO, "Q8001", "Lone Mesa")], sponsor=solaris, poi=lone)
    out["lone_hidden"] = prop(
        s, "Lone Mesa Hidden", [(PJM, "AB9-9", "Lone Mesa II")], sponsor=solaris, poi=lone
    )
    # Merged records.
    out["kings"] = prop(s, "Kings Solar", [(CAISO, "Q2001", "Kings"), (EIA, "64001-1", "Kings Solar")])
    out["twoplants"] = prop(s, "Twin Plants", [(EIA, "65001-1", "Twin A"), (EIA, "65002-1", "Twin B")])
    out["tworeq"] = prop(s, "Ridge Wind", [(CAISO, "Q3001", "Ridge Wind"), (CAISO, "Q3002", "Ridge Wind")])
    out["derived"] = prop(
        s, "Mesa Verde", [(DERIVED, "SECRET-ID-42", "Mesa Verde"), (EIA, "66001-1", "Mesa Verde")]
    )
    out["half_hidden"] = prop(
        s, "Half Hidden", [(CAISO, "Q4001", "Half Hidden"), (PJM, "AC1-1", "Half Hidden PJM")]
    )
    out["person_merged"] = prop(
        s,
        "Quill Home Solar",
        [(CAISO, "Q9001", "Quill Home"), (EIA, "67001-1", "Quill Home")],
        sponsor=person,
    )
    s.flush()
    return out


@pytest.fixture()
def store(factory: sessionmaker[Session]) -> Iterator[tuple[Session, dict[str, Proposal]]]:
    with factory() as s:
        recs = populate(s)
        build.rebuild_sites(s, now=T0)
        s.commit()
        yield s, recs


def _all_text(out: pathlib.Path) -> str:
    """Every value written: the CSVs, the README, the manifest and every workbook cell."""
    parts = [
        p.read_text(encoding="utf-8") for p in sorted(out.iterdir()) if p.suffix in (".csv", ".md", ".json")
    ]
    wb = load_workbook(out / handcheck.WORKBOOK)
    for ws in wb.worksheets:
        parts.extend(str(c.value) for row in ws.iter_rows() for c in row if c.value is not None)
    return "\n".join(parts)


def test_generate_reads_only_the_served_view(
    store: tuple[Session, dict[str, Proposal]], tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SITES_ENABLED", raising=False)
    s, recs = store
    sample = handcheck.generate(
        s, tmp_path, seed=11, n_sites=10, n_merged=10, largest=1, now=NOW, store="test store"
    )
    site_names = {site.name for site in sample.site_frame.items}
    # Darden I+II served (the PJM row is not); the plant; the person's site and the lone site are not.
    assert site_names == {"Darden I Solar", "Matador Gas"}
    assert sample.site_frame.excluded["a printed name may be a natural person's"] == 1
    darden = next(x for x in sample.site_frame.items if x.name == "Darden I Solar")
    assert [m.public_id for m in darden.members] == [recs["darden1"].public_id, recs["darden2"].public_id]
    assert darden.members[1].relation == "phase_of" and darden.weakest_rule == "poi_sponsor"
    plant = next(x for x in sample.site_frame.items if x.name == "Matador Gas")
    assert plant.plant_ids == ("69799",) and len(plant.members) == 4
    assert {m.relation for m in plant.members[1:]} == {"unit_of"}
    # Merged frame: half_hidden has one served link; person_merged names a person.
    merged = {m.name: m for m in sample.merged_frame.items}
    assert set(merged) == {"Kings Solar", "Twin Plants", "Ridge Wind", "Mesa Verde"}
    assert merged["Twin Plants"].stratum == "multi_plant"
    assert merged["Ridge Wind"].stratum == "multi_request"
    assert merged["Kings Solar"].stratum == "cross_register"
    assert sample.merged_frame.unserved["fewer than two links served"] == 1
    assert sample.merged_frame.excluded["a printed name may be a natural person's"] == 1
    # A record id the licence does not let the API print is hashed, never written.
    mesa_keys = [link.key for link in merged["Mesa Verde"].links]
    assert any(k.startswith(f"{DERIVED}:#") for k in mesa_keys)
    # Everything frame-sized is drawn (small store); the strata add up.
    assert len(sample.sites) == 2 and len(sample.merged) == 4
    assert sum(st.population for st in sample.strata if st.sheet == handcheck.SHEET_MERGED) == 4
    text = _all_text(tmp_path)
    for leaked in ("Secret Darden III", "AB1-001", "Half Hidden PJM", "AC1-1", "SECRET-ID-42", "Lone Mesa"):
        assert leaked not in text
    for person in ("Quillfeather", "Quill Ranch", "Quill Home"):
        assert person not in text
    assert "Darden II" in text and "us.eia.860m:69799-G1" in text and "us.iso.caiso.gen_queue:Q1949" in text


def test_workbook_shape(
    store: tuple[Session, dict[str, Proposal]], tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SITES_ENABLED", raising=False)
    s, _recs = store
    handcheck.generate(s, tmp_path, seed=11, n_sites=10, n_merged=10, largest=1, now=NOW)
    assert {p.name for p in tmp_path.iterdir()} == {
        handcheck.WORKBOOK,
        "sites.csv",
        "merged_records.csv",
        "site_members.csv",
        "merged_links.csv",
        "strata.csv",
        "manifest.json",
        "README.md",
    }
    wb = load_workbook(tmp_path / handcheck.WORKBOOK)
    assert wb.sheetnames == [
        "Instructions",
        "Sites",
        "Merged records",
        "Site members",
        "Merged links",
        "Strata",
    ]
    assert "BaseURL" in wb.defined_names
    sites = wb["Sites"]
    header = [c.value for c in sites[1]]
    assert header[:2] == [H_ITEM, "Site"] and H_SITE_VERDICT in header and H_SITE_PROBLEM in header
    assert sites.freeze_panes == "C2"
    link = sites.cell(row=2, column=2).value
    assert link.startswith('=HYPERLINK(BaseURL&"/sites/site_')
    lists = {dv.formula1 for dv in sites.data_validations.dataValidation}
    assert '"Yes,No,Unsure"' in lists
    assert '"Unrelated record grouped,Missing record,Wrong lead,Wrong label,Other"' in lists
    merged = wb["Merged records"]
    mlists = {dv.formula1 for dv in merged.data_validations.dataValidation}
    assert '"Yes,No (several projects),Unsure"' in mlists
    assert merged.cell(row=2, column=2).value.startswith('=HYPERLINK(BaseURL&"/proposals/')
    verdict_col = header.index(H_SITE_VERDICT) + 1
    assert sites.cell(row=2, column=verdict_col).fill.fgColor.rgb.endswith("FFF2CC")
    members_col = header.index("Members (in site order)") + 1
    assert sites.column_dimensions[chr(ord("A") + members_col - 1)].width >= 60
    assert sites.cell(row=2, column=members_col).alignment.wrap_text
    assert all(c.font.name == "Arial" for c in sites[2])
    # The CSV twin carries the same rows, with paths instead of formulas.
    with (tmp_path / "sites.csv").open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert [r[H_ITEM] for r in rows] == ["S01", "S02"] and rows[0]["Site link"].startswith("/sites/site_")
    assert {r["Site"] for r in rows} == {"Darden I Solar", "Matador Gas"}
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    assert manifest["seed"] == 11 and manifest["sites"]["sample"] == 2
    assert set(manifest["files"]) >= {"handcheck.xlsx", "sites.csv"}


def test_same_seed_same_sample(
    store: tuple[Session, dict[str, Proposal]], tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SITES_ENABLED", raising=False)
    s, _recs = store
    a = handcheck.generate(s, tmp_path / "a", seed=5, n_sites=1, n_merged=2, largest=0, now=NOW)
    b = handcheck.generate(s, tmp_path / "b", seed=5, n_sites=1, n_merged=2, largest=0, now=NOW)
    assert [x.key for x in a.sites] == [x.key for x in b.sites]
    assert [x.key for x in a.merged] == [x.key for x in b.merged]
    assert (tmp_path / "a" / "sites.csv").read_bytes() == (tmp_path / "b" / "sites.csv").read_bytes()


def test_cli_generates_read_only_from_a_file_store(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SITES_ENABLED", raising=False)
    url = f"sqlite+pysqlite:///{tmp_path / 'store.db'}"
    engine = get_engine(url)
    init_db(engine)
    with get_sessionmaker(engine)() as s:
        populate(s)
        build.rebuild_sites(s, now=T0)
        s.commit()
    engine.dispose()
    before = (tmp_path / "store.db").read_bytes()
    out = tmp_path / "out"
    assert (
        handcheck.main(
            ["generate", "--database-url", url, "--out", str(out), "--seed", "3", "--largest", "1"]
        )
        == 0
    )
    assert (out / handcheck.WORKBOOK).exists()
    assert (tmp_path / "store.db").read_bytes() == before
    readme = (out / "README.md").read_text()
    assert "sqlite+pysqlite" in readme and "regenerate" in readme


# --------------------------------------------------------------------------------- the scorer
def _member(rank: int, name: str, relation: str, confidence: str, rule: str) -> Member:
    return Member(
        rank=rank,
        public_id=f"prop_{name}",
        slug=name.lower(),
        name=name,
        capacity_mw=100.0,
        technology="solar",
        lifecycle_state="filed",
        jurisdiction="US-TX",
        sponsor="Acme Renewables LLC",
        relation=relation,
        relation_rule="r",
        confidence=confidence,
        grouping_rule=rule,
        group_key=f"proposal:prop_{name}",
        parent_rank=None,
        connection_point="Sub 345kV",
        links=(_link(CAISO, f"Q-{name}", name),),
    )


def synthetic_sample() -> handcheck.Sample:
    """A served frame of 20 large/poi_sponsor sites and 30 poi_stem/low ones (no store), 50 merged
    records in two categories; sample 10 + 10 sites (largest 2) and 20 merged."""
    sites: list[SiteItem] = []
    for i in range(200):
        rule, conf, rel = (
            ("poi_sponsor", "high", "phase_of") if i < 150 else ("poi_stem", "low", "superseded_by")
        )
        size = 2 if i >= 2 else 6
        members = [_member(0, f"S{i}A", "lead", "high", rule)]
        members += [_member(k, f"S{i}M{k}", rel, conf, rule) for k in range(1, size)]
        sites.append(SiteItem(f"site_{i:04d}", f"S{i}A", members))
    merged: list[MergedItem] = []
    for i in range(300):
        links = (
            (_link(EIA, f"{70000 + i}-1", "Plant"), _link(EIA, f"{80000 + i}-1", "Plant"))
            if i < 60
            else (_link(CAISO, f"Q{i}", "One"), _link(EIA, f"{90000 + i}-1", "One"))
        )
        merged.append(
            MergedItem(
                public_id=f"prop_m{i}",
                slug=f"m-{i}",
                name=f"Merged {i}",
                capacity_mw=50.0,
                technology="wind",
                lifecycle_state="filed",
                jurisdiction="US-TX",
                sponsor=None,
                links=links,
                flags=handcheck.merged_flags(links),
            )
        )
    return handcheck.build_sample(
        Frame(sites, Counter()),
        Frame(merged, Counter()),
        seed=1,
        n_sites=20,
        n_merged=20,
        largest=2,
        generated_at=NOW,
        store="synthetic",
    )


def fill(path: pathlib.Path) -> None:
    """Sites: largest both Yes; poi_sponsor/high: 1 No (unrelated) and 1 No (wrong lead), rest Yes;
    poi_stem/low: 3 No (unrelated), 1 Unsure, 1 blank, rest Yes. Merged: multi_plant 5 No of the
    sampled; cross_register 1 No; one Unsure."""
    wb = load_workbook(path)
    for name, verdict_h, extra_h in (
        ("Sites", H_SITE_VERDICT, H_SITE_PROBLEM),
        ("Merged records", H_MERGED_VERDICT, H_MERGED_KIND),
    ):
        ws = wb[name]
        header = [c.value for c in ws[1]]
        col = {h: header.index(h) + 1 for h in header}
        seen: Counter[str] = Counter()
        for r in range(2, ws.max_row + 1):
            stratum = ws.cell(row=r, column=col["Stratum"]).value
            seen[stratum] += 1
            k = seen[stratum]
            verdict, extra = "Yes", None
            if name == "Sites":
                if stratum == "poi_sponsor/high" and k == 1:
                    verdict, extra = "No", "Unrelated record grouped"
                elif stratum == "poi_sponsor/high" and k == 2:
                    verdict, extra = "No", "Wrong lead"
                elif stratum == "poi_stem/low" and k <= 3:
                    verdict, extra = "No", "Unrelated record grouped"
                elif stratum == "poi_stem/low" and k == 4:
                    verdict = "Unsure"
                elif stratum == "poi_stem/low" and k == 5:
                    verdict = None
            else:
                if stratum == "multi_plant" and k <= 5:
                    verdict, extra = "No (several projects)", "Phases of one development"
                elif stratum == "cross_register" and k == 1:
                    verdict, extra = "No (several projects)", "Unrelated projects"
                elif stratum == "cross_register" and k == 2:
                    verdict = "Unsure"
            ws.cell(row=r, column=col[verdict_h], value=verdict)
            ws.cell(row=r, column=col[extra_h], value=extra)
            ws.cell(row=r, column=col[H_NOTES], value="checked" if verdict else None)
    wb.save(path)


def test_score_synthetic_filled_sheet(tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]) -> None:
    sample = synthetic_sample()
    sizes = {(s.sheet, s.name): (s.population, s.sample) for s in sample.strata}
    assert sizes[("Sites", "largest")] == (2, 2)
    assert sum(n for (sheet, _h), (_p, n) in sizes.items() if sheet == "Sites") == 20
    assert sum(n for (sheet, _h), (_p, n) in sizes.items() if sheet == "Merged records") == 20
    handcheck.write_outputs(tmp_path, sample)
    path = tmp_path / handcheck.WORKBOOK
    fill(path)
    score = handcheck.score_rows(*handcheck.read_filled(path))
    pop_sponsor, n_sponsor = sizes[("Sites", "poi_sponsor/high")]
    pop_stem, n_stem = sizes[("Sites", "poi_stem/low")]
    s = score.sites
    assert (s.sampled, s.unsure, s.blank) == (20, 1, 1)
    assert s.judged == 18 and s.errors == 4  # the wrong-lead No is a quality issue, not a grouping error
    assert s.rate == pytest.approx(4 / 18)
    expected = (2 * 0 + pop_sponsor * (1 / n_sponsor) + pop_stem * (3 / (n_stem - 2))) / (
        2 + pop_sponsor + pop_stem
    )
    assert s.weighted_rate == pytest.approx(expected)
    assert score.sites_any_no.errors == 5
    assert score.site_problems == Counter({"unrelated record grouped": 4, "wrong lead": 1})
    k = score.kill_switch()
    assert k["crossed_on_point"] is (expected > 0.10)
    assert k["crossed_on_lower_bound"] is (s.weighted_interval[0] > 0.10)
    assert k["sample_crossed_on_point"] is True
    m = score.merged
    assert (m.sampled, m.judged, m.errors, m.unsure) == (20, 19, 6, 1)
    by = {st.stratum: st for st in m.strata}
    assert by["multi_plant"].errors == 5 and by["cross_register"].errors == 1
    assert score.merged_kinds == Counter({"Phases of one development": 5, "Unrelated projects": 1})
    # The CSV directory scores the same once the same verdicts are copied in; the CLI prints it.
    assert handcheck.main(["score", str(path)]) == 0
    printed = capsys.readouterr().out
    assert "1-in-10 rule on the weighted point estimate" in printed and "MERGED RECORDS" in printed
    assert handcheck.main(["score", str(path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["sites"]["errors"] == 4 and payload["merged"]["errors"] == 6


def test_score_reads_the_csv_twin(tmp_path: pathlib.Path) -> None:
    sample = synthetic_sample()
    handcheck.write_outputs(tmp_path, sample)
    for name, verdict_h in (("sites.csv", H_SITE_VERDICT), ("merged_records.csv", H_MERGED_VERDICT)):
        with (tmp_path / name).open(newline="", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        for i, row in enumerate(rows):
            row[verdict_h] = ("No" if name == "sites.csv" else "No (several projects)") if i < 2 else "yes"
        with (tmp_path / name).open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
    score = handcheck.score_rows(*handcheck.read_filled(tmp_path))
    assert (score.sites.judged, score.sites.errors) == (20, 2)
    assert (score.merged.judged, score.merged.errors) == (20, 2)


def test_unreadable_verdict_is_blank_and_reported() -> None:
    rows = [
        {H_ITEM: "S01", "Stratum": "a", "Stratum population": 5, "Stratum sample": 1, H_SITE_VERDICT: "maybe"}
    ]
    score = handcheck.score_rows(rows, [])
    assert score.sites.blank == 1 and score.unreadable == ["S01: 'maybe'"]


def test_members_text_summarises_long_unit_lists() -> None:
    lead = _member(0, "Big", "lead", "high", "eia_plant")
    units = [
        Member(
            **{
                **_member(k, f"U{k}", "unit_of", "high", "eia_plant").__dict__,
                "group_key": lead.group_key,
                "parent_rank": 0,
            }
        )
        for k in range(1, 8)
    ]
    text = handcheck.members_text(SiteItem("site_x", "Big", [lead, *units]))
    lines = text.split("\n")
    assert lines[0].startswith("#1 Big") and "LEAD" in lines[0]
    assert len(lines) == 1 + 2 + 1 and "and 5 more units" in lines[-1]
    assert "unit of #1 (high)" in lines[1]
