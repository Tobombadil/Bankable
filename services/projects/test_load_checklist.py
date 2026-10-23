"""Loading a private checklist pack (docs/19 §2.3): a no-op without `CHECKLIST_PACK_URL`,
validated against the public schema and the generic keys, and silent about its contents."""

from __future__ import annotations

import json
import logging
import pathlib
from collections.abc import Callable
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
import yaml
from sqlalchemy.orm import Session

from services.projects import load_checklist
from services.projects.checklist import GENERIC_PACK_PATH, PackError, active_pack
from services.projects.load_checklist import fetch_pack, load_pack, main, parse_pack
from services.projects.models import ChecklistTemplateItem

SENTINEL = "PRIVATE-GUIDANCE-SENTINEL-7f3a"
SECRET_URL = "https://packs.example.org/pack.yaml?X-Amz-Signature=SIGNATURE-SENTINEL"  # noqa: S105 -- a fake


def _mock_http(monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]) -> None:
    """Every `httpx.Client` the loader builds answers from `handler`, through httpx's own client
    code (which is what logs each request)."""
    real_client = httpx.Client

    def client(**kwargs: Any) -> httpx.Client:
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, "Client", client)


def _template_store(tmp_path: pathlib.Path) -> str:
    url = f"sqlite+pysqlite:///{tmp_path / 'store.db'}"
    engine = sa.create_engine(url)
    ChecklistTemplateItem.__table__.create(engine)
    engine.dispose()
    return url


def _pack(version: str = "private-2026-10", **item_overrides: Any) -> dict[str, Any]:
    generic = yaml.safe_load(GENERIC_PACK_PATH.read_text(encoding="utf-8"))
    items = [dict(item, guidance=f"{SENTINEL} for {item['key']}") for item in generic["items"]]
    items[0].update(item_overrides)
    return {"schema": "inframation.checklist-pack/1", "pack_version": version, "items": items}


def test_no_pack_url_is_a_no_op(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    monkeypatch.delenv("CHECKLIST_PACK_URL", raising=False)

    def _no_database(*args: object, **kwargs: object) -> None:
        raise AssertionError("the database must not be touched without a pack")

    monkeypatch.setattr("services.db.session.get_engine", _no_database)
    with caplog.at_level(logging.INFO):
        assert main([]) == 0
    assert "generic checklist names stay in use" in caplog.text


def test_loads_a_pack_from_a_file_and_it_becomes_active(
    db: Session, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "pack.yaml"
    path.write_text(yaml.safe_dump(_pack(pillar="sponsor")), encoding="utf-8")
    database = tmp_path / "store.db"
    engine = sa.create_engine(f"sqlite+pysqlite:///{database}")
    ChecklistTemplateItem.__table__.create(engine)
    engine.dispose()
    monkeypatch.setenv("CHECKLIST_PACK_URL", f"file://{path}")
    with caplog.at_level(logging.INFO):
        assert main(["--database-url", f"sqlite+pysqlite:///{database}"]) == 0
    assert "checklist pack private-2026-10 loaded: 32 items, 0 removed" in caplog.text
    assert SENTINEL not in caplog.text and str(path) not in caplog.text
    engine = sa.create_engine(f"sqlite+pysqlite:///{database}")
    with engine.connect() as conn:
        rows = conn.execute(
            sa.text("SELECT key, pillar, guidance FROM checklist_template_item ORDER BY sort_order")
        ).all()
    engine.dispose()
    assert len(rows) == 32 and rows[0].key == "ppa" and rows[0].pillar == "sponsor"
    assert rows[0].guidance.startswith(SENTINEL)


def test_load_pack_replaces_a_versions_rows_and_the_newest_pack_wins(db: Session) -> None:
    first = load_pack(db, _pack("private-1"))
    assert (first.items, first.removed) == (32, 0)
    smaller = _pack("private-1")
    smaller["items"] = smaller["items"][:30]
    assert load_pack(db, smaller).removed == 2
    count = db.execute(sa.select(sa.func.count()).select_from(ChecklistTemplateItem)).scalar()
    assert count == 30
    load_pack(db, _pack("private-2", name="Offtake PPA"))
    assert active_pack(db).pack_version == "private-2"
    assert active_pack(db).items[0].name == "Offtake PPA"


@pytest.mark.parametrize(
    "document, message",
    [
        (lambda: _pack(key="water_rights"), "not in generic-v1"),
        (lambda: _pack(folder="finance"), "moves a generic item"),
        (lambda: _pack("generic-v1"), "reserved"),
        (lambda: _pack(guidance=""), "items/0/guidance"),
        (lambda: _pack(name=SENTINEL * 20), "items/0/name"),
        (
            lambda: {
                "schema": "inframation.checklist-pack/1",
                "pack_version": "x",
                "items": [{"key": "ppa"}],
            },
            "items/0",
        ),
    ],
)
def test_refused_packs_say_where_never_what(db: Session, document: Any, message: str) -> None:
    with pytest.raises(PackError) as excinfo:
        load_pack(db, document())
    assert message in str(excinfo.value)
    assert SENTINEL not in str(excinfo.value)
    assert db.execute(sa.select(sa.func.count()).select_from(ChecklistTemplateItem)).scalar() == 0


def test_a_refused_pack_exits_non_zero_and_logs_nothing_private(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "pack.json"
    path.write_text(json.dumps(_pack(key="not_a_generic_key")), encoding="utf-8")
    monkeypatch.setenv("DATABASE_URL", f"sqlite+pysqlite:///{tmp_path / 'unused.db'}")
    with caplog.at_level(logging.INFO):
        assert main(["--pack-url", str(path)]) == 1
    assert "checklist pack refused" in caplog.text
    assert SENTINEL not in caplog.text and "not_a_generic_key" not in caplog.text


def test_a_database_error_is_named_not_dumped(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "pack.yaml"
    path.write_text(yaml.safe_dump(_pack()), encoding="utf-8")
    with caplog.at_level(logging.INFO):
        # A store with no template table: the INSERT's parameters would carry the guidance.
        assert (
            main(["--pack-url", str(path), "--database-url", f"sqlite+pysqlite:///{tmp_path / 'empty.db'}"])
            == 1
        )
    assert "database error (OperationalError)" in caplog.text
    assert SENTINEL not in caplog.text


def test_a_pack_url_without_a_database_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert main(["--pack-url", str(tmp_path / "x.yaml")]) == 2


def test_fetch_https_without_logging_the_url(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    body = yaml.safe_dump(_pack()).encode()
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if "missing" in str(request.url):
            return httpx.Response(404)
        if "redirect" in str(request.url):
            return httpx.Response(302, headers={"Location": "https://elsewhere.example.org/"})
        return httpx.Response(200, content=body)

    _mock_http(monkeypatch, handler)
    levels = {name: logging.getLogger(name).level for name in load_checklist.HTTP_CLIENT_LOGGERS}
    # DEBUG at the root: httpx would log "HTTP Request: GET <the signed URL>" at INFO.
    with caplog.at_level(logging.DEBUG):
        assert fetch_pack(SECRET_URL) == body
        assert seen == [SECRET_URL]
        for url, message in (
            ("https://packs.example.org/missing?sig=SIGNATURE-SENTINEL", "HTTP 404"),
            ("https://packs.example.org/redirect?sig=SIGNATURE-SENTINEL", "HTTP 302"),
        ):
            with pytest.raises(PackError) as excinfo:
                fetch_pack(url)
            assert message in str(excinfo.value) and "packs.example.org" not in str(excinfo.value)
        with pytest.raises(PackError, match="larger than"):
            fetch_pack(SECRET_URL, max_bytes=100)
    assert "SIGNATURE-SENTINEL" not in caplog.text and "packs.example.org" not in caplog.text
    assert not [r for r in caplog.records if r.name.startswith(load_checklist.HTTP_CLIENT_LOGGERS)]
    # The quiet lasts only for the fetch: the process's own levels come back.
    assert {name: logging.getLogger(name).level for name in load_checklist.HTTP_CLIENT_LOGGERS} == levels


def test_only_https_or_a_local_file(tmp_path: pathlib.Path) -> None:
    for url in (
        "http://packs.example.org/pack.yaml",
        "ftp://packs.example.org/pack.yaml",
        "s3://bucket/pack.yaml",
    ):
        with pytest.raises(PackError, match="https"):
            fetch_pack(url)
    with pytest.raises(PackError, match="does not exist"):
        fetch_pack(str(tmp_path / "absent.yaml"))
    big = tmp_path / "big.yaml"
    big.write_bytes(b"x" * 200)
    with pytest.raises(PackError, match="larger than"):
        fetch_pack(str(big), max_bytes=100)
    assert fetch_pack(f"file://{big}") == b"x" * 200


def test_parse_errors_hide_the_text() -> None:
    with pytest.raises(PackError) as excinfo:
        parse_pack(f"items: [{SENTINEL}".encode())
    assert SENTINEL not in str(excinfo.value)
    with pytest.raises(PackError):
        parse_pack(b"\xff\xfe")
    assert parse_pack(b'{"a": 1}') == {"a": 1}


@pytest.mark.parametrize("status", [200, 403])
def test_the_pack_url_from_the_environment_never_reaches_the_output(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
    status: int,
) -> None:
    """The deploy step as it runs: `CHECKLIST_PACK_URL` (a signed URL) in the environment, no flag,
    root logging at DEBUG. Neither the log nor stdout or stderr shows the URL, loaded or refused."""
    body = yaml.safe_dump(_pack()).encode()
    _mock_http(monkeypatch, lambda request: httpx.Response(status, content=body))
    monkeypatch.setenv("CHECKLIST_PACK_URL", SECRET_URL)
    with caplog.at_level(logging.DEBUG):
        code = main(["--database-url", _template_store(tmp_path)])
    out, err = capsys.readouterr()
    assert code == (0 if status == 200 else 1)
    assert ("loaded: 32 items" in caplog.text) is (status == 200)
    for text in (caplog.text, out, err):
        assert "SIGNATURE-SENTINEL" not in text and "packs.example.org" not in text
        assert SENTINEL not in text


@pytest.mark.parametrize(
    "url",
    [
        "https://packs.example.org/pack.yaml?X-Amz-Signature=SIGNATURE-SENTINEL\x00",
        "https://[::1/pack.yaml?X-Amz-Signature=SIGNATURE-SENTINEL",
        "https://[::1",
    ],
)
def test_a_malformed_url_is_a_pack_error_that_does_not_quote_it(url: str) -> None:
    """httpx's `InvalidURL` is not an `HTTPError`, and `urlsplit` raises `ValueError`; both used to
    escape as a traceback whose message could quote the URL."""
    with pytest.raises(PackError) as excinfo:
        fetch_pack(url)
    assert "SIGNATURE-SENTINEL" not in str(excinfo.value) and "::1" not in str(excinfo.value)


def test_an_unexpected_failure_is_named_by_type_only(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "pack.yaml"
    path.write_text(yaml.safe_dump(_pack()), encoding="utf-8")

    def broken(*args: object, **kwargs: object) -> None:
        raise KeyError(f"{SENTINEL} and SIGNATURE-SENTINEL")

    monkeypatch.setattr(load_checklist, "load_pack", broken)
    with caplog.at_level(logging.INFO):
        assert main(["--pack-url", str(path), "--database-url", _template_store(tmp_path)]) == 1
    assert "unexpected error (KeyError)" in caplog.text
    assert SENTINEL not in caplog.text and "SIGNATURE-SENTINEL" not in caplog.text
