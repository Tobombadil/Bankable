"""`scripts/posture_report.py`: the per-source table the switch-back runbook (docs/26 §5) reads."""

from __future__ import annotations

import datetime as dt
import json
import pathlib

import sqlalchemy as sa
from sqlalchemy.orm import Session

from scripts.posture_report import collect, main, render
from services.api.conftest import make_event, make_open_licence, make_public_source, make_visible_proposal
from services.db.models import Licence
from services.db.session import get_engine, init_db

UTC = dt.UTC


def _seed(engine: sa.Engine) -> None:
    with Session(engine) as db:
        open_src = make_public_source(db, make_open_licence(db))
        p1 = make_visible_proposal(db, open_src, public_id_suffix="1")
        make_event(db, p1, open_src)
        nc = Licence(
            id="nc-lic",
            name="State site noncommercial grant",
            reuse_class="noncommercial",
            attribution_required=True,
            requires_link_back=True,
            allows_derived_publication=True,
            allows_raw_publication=True,
            allows_commercial_use=False,
            gate_flag=False,
            evidence_url="https://example.org/site-policies",
            evidence_retrieved_at=dt.datetime(2026, 9, 25, tzinfo=UTC),
            classified_by="legal-compliance",
        )
        db.add(nc)
        db.flush()
        nc_src = make_public_source(db, nc, id_="us.test.nc_register")
        p2 = make_visible_proposal(db, nc_src, public_id_suffix="2")
        p2.min_reuse_class = "noncommercial"
        make_event(db, p2, nc_src)
        p3 = make_visible_proposal(db, nc_src, public_id_suffix="3")
        p3.min_reuse_class = "noncommercial"
        db.commit()


def test_report_lists_noncommercial_rows_per_source_and_table(tmp_path: pathlib.Path) -> None:
    url = f"sqlite+pysqlite:///{tmp_path / 'posture.db'}"
    engine = get_engine(url)
    init_db(engine)
    _seed(engine)

    report = collect(engine)
    assert [lic["source_id"] for lic in report["licences"]] == ["us.test.nc_register"]
    by_key = {(r["table"], r["source_id"]): r["rows"] for r in report["rows"]}
    assert by_key[("proposal_source", "us.test.nc_register")] == 2
    assert by_key[("event", "us.test.nc_register")] == 1
    # The open-licence source never appears: the table is by licence class, not by source.
    assert not any(r["source_id"] == "us.test.public_source" for r in report["rows"])

    text = render(report)
    assert "us.test.nc_register" in text
    assert "proposal_source" in text
    assert "total" in text


def test_report_says_none_on_a_store_without_the_class(tmp_path: pathlib.Path) -> None:
    url = f"sqlite+pysqlite:///{tmp_path / 'empty.db'}"
    engine = get_engine(url)
    init_db(engine)
    with Session(engine) as db:
        make_public_source(db, make_open_licence(db))
        db.commit()
    report = collect(engine)
    assert report["licences"] == [] and report["rows"] == []
    assert render(report).startswith("none:")


def test_cli_prints_json_and_is_read_only(tmp_path: pathlib.Path, capsys) -> None:
    url = f"sqlite+pysqlite:///{tmp_path / 'cli.db'}"
    engine = get_engine(url)
    init_db(engine)
    _seed(engine)
    with engine.connect() as conn:
        before = conn.execute(sa.text("SELECT count(*) FROM proposal_source")).scalar()

    assert main(["--database-url", url, "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["reuse_class"] == "noncommercial"
    assert any(r["table"] == "proposal_source" and r["rows"] == 2 for r in out["rows"])

    assert main(["--database-url", url]) == 0
    assert "us.test.nc_register" in capsys.readouterr().out
    with engine.connect() as conn:
        assert conn.execute(sa.text("SELECT count(*) FROM proposal_source")).scalar() == before
        assert (
            conn.execute(sa.text("SELECT count(*) FROM licence WHERE reuse_class='noncommercial'")).scalar()
            == 1
        )
