"""Parser tests for us.eia.860 against a recorded 60-row Ownership-sheet fixture, plus the
index-page scrape (link filtering and placeholder handling), same shape as
`pipeline/connectors/us_eia_860m/test_connector.py`.
"""

from __future__ import annotations

import pytest

from conftest import connector_for, snapshot
from pipeline.connectors.base import ConnectorError, ParseError
from pipeline.connectors.http import HttpBlocked
from pipeline.connectors.us_eia_860.connector import (
    INDEX_URL,
    find_owner_member,
    find_zip_links,
)

SOURCE_ID = "us.eia.860"
URL = "https://www.eia.gov/electricity/data/eia860/xls/eia8602024.zip"


@pytest.fixture(scope="module")
def parsed():
    c = connector_for(SOURCE_ID)
    raw = snapshot("eia860_owner_2024_sample.zip", URL, content_type="application/zip")
    rows = c.parse(raw)
    return c, raw, rows, c.normalize(rows, raw)


def test_ownership_sheet_header_is_on_the_second_row(parsed):
    _, _, rows, _ = parsed
    assert {"Plant Code", "Generator ID", "Owner Name", "Percent Owned"} <= set(rows[0])


def test_sixty_rows_parsed_from_the_sample(parsed):
    _, _, rows, _ = parsed
    assert len(rows) == 60


def test_record_id_is_stable_and_unique(parsed):
    _, _, _, df = parsed
    assert not df["record_id"].duplicated().any()
    assert (df["source_id"] == "us.eia.860").all()


def test_document_kind_fields(parsed):
    _, _, _, df = parsed
    assert (df["lifecycle_state"] == "filed").all()
    assert df["capacity_mw"].isna().all()
    assert df["doc_type"].iloc[0] == "eia860_schedule4_owner"


def test_find_owner_member_matches_the_real_naming():
    names = ["1___Utility_Y2024.xlsx", "4___Owner_Y2024.xlsx", "LayoutY2024.xlsx"]
    found = find_owner_member(names)
    assert found == ("4___Owner_Y2024.xlsx", 2024)


def test_find_owner_member_absent_is_none():
    assert find_owner_member(["1___Utility_Y2024.xlsx"]) is None


def test_zip_links_exclude_archive_paths_and_sort_newest_year_first():
    """eia.gov robots.txt disallows `/*archive/`; those links must never be requested."""
    html = (
        '<a href="/electricity/data/eia860/archive/xls/eia8602023.zip">2023</a>'
        '<a href="/electricity/data/eia860/xls/eia8602025ER.zip">2025 ER</a>'
        '<a href="/electricity/data/eia860/archive/xls/eia8602024.zip">2024 archive</a>'
        '<a href="/electricity/data/eia860/xls/eia8602025ER.zip">dup</a>'
    )
    links = find_zip_links(html)
    assert links == ["https://www.eia.gov/electricity/data/eia860/xls/eia8602025ER.zip"]


class _Answer:
    def __init__(self, content: bytes, content_type: str) -> None:
        self.status_code = 200
        self.content = content
        self.text = content.decode()
        self.headers = {"Content-Type": content_type}


class _Http:
    """Index page, one robots-blocked candidate, one HTML placeholder, then a real zip."""

    def __init__(self, blocked: str) -> None:
        self.blocked = blocked
        self.urls: list[str] = []

    def get(self, url: str, **_: object) -> _Answer:
        self.urls.append(url)
        if url == INDEX_URL:
            return _Answer(
                b'<a href="/electricity/data/eia860/xls/eia8602026ER.zip">b</a>'
                b'<a href="/electricity/data/eia860/xls/eia8602025ER.zip">er</a>'
                b'<a href="/electricity/data/eia860/xls/eia8602024.zip">real</a>',
                "text/html",
            )
        if url == self.blocked:
            raise HttpBlocked(f"robots.txt disallows {url}")
        if url == URL:
            return _Answer(b"PK\x03\x04real", "application/octet-stream")
        return _Answer(b"<html>placeholder</html>", "text/html")


def test_fetch_skips_a_robots_blocked_candidate_and_a_placeholder_then_records_both():
    blocked = "https://www.eia.gov/electricity/data/eia860/xls/eia8602026ER.zip"
    http = _Http(blocked)
    c = connector_for(SOURCE_ID, http=http)
    raw = c.fetch()
    assert raw.url == URL
    assert raw.content.startswith(b"PK")
    tried = raw.meta["candidates"]
    assert tried[0] == {"url": blocked, "blocked": f"robots.txt disallows {blocked}"}
    assert tried[1]["zip"] is False and tried[2]["zip"] is True
    assert raw.requests_made == 1 + len(tried)


def test_fetch_fails_cleanly_when_every_candidate_is_blocked_or_a_placeholder():
    class _NoRealZip(_Http):
        def get(self, url: str, **_: object) -> _Answer:
            if url == URL:
                return _Answer(b"<html>placeholder</html>", "text/html")
            return super().get(url)

    c = connector_for(SOURCE_ID, http=_NoRealZip(""))
    with pytest.raises(ConnectorError):
        c.fetch()


def test_a_placeholder_release_is_a_parse_error():
    """The 2025 early release answers HTTP 200 with a 55 KB HTML placeholder, not a real zip."""
    c = connector_for(SOURCE_ID)
    raw = snapshot("eia860_owner_2024_sample.zip", URL)
    raw.content = b"<!doctype html><html><body>Early release coming soon</body></html>"
    with pytest.raises(ParseError):
        c.parse(raw)
