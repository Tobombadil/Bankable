"""Parser tests for us.iso.caiso.gen_queue against a recorded Public Queue Report."""

from __future__ import annotations

import hashlib
import io

import openpyxl
import pandas as pd
import pytest
import requests

from conftest import connector_for, fixture_path, snapshot
from pipeline.connectors.base import BlockedError, ConnectorError, ParseError, PreviousSnapshot
from pipeline.connectors.http import PoliteSession
from pipeline.connectors.iso_queue import describe_response
from pipeline.vendor.gridstatus import queues

SOURCE_ID = "us.iso.caiso.gen_queue"
URL = "https://www.caiso.com/PublishedDocuments/PublicQueueReport.xlsx"


@pytest.fixture(scope="module")
def parsed():
    c = connector_for(SOURCE_ID)
    raw = snapshot("caiso_public_queue_report.xlsx", URL)
    rows = c.parse(raw)
    return c, raw, rows, c.normalize(rows, raw)


def test_all_three_sheets_are_parsed(parsed):
    _, _, _, df = parsed
    assert len(df) == 39
    assert {"studied", "contracted", "built", "withdrawn"} & set(df["lifecycle_state"])
    assert set(df["status_raw"]) == {"ACTIVE", "COMPLETED", "WITHDRAWN"}


def test_provenance_and_licence(parsed):
    c, _, _, df = parsed
    assert set(df["source_id"]) == {SOURCE_ID}
    assert set(df["licence_id"]) == {c.source.licence_id}
    assert df["retrieved_at"].notna().all()


def test_queue_position_is_the_record_id(parsed):
    _, _, rows, df = parsed
    assert not df["record_id"].duplicated().any()
    assert set(df["source_record_id"]) == {str(r["Queue ID"]).strip() for r in rows}


def test_executed_agreement_promotes_active_rows_to_contracted(parsed):
    _, _, _, df = parsed
    executed = df[df["raw"].str.contains('"Interconnection Agreement Status": "Executed"')]
    active_executed = executed[executed["status_raw"] == "ACTIVE"]
    assert len(active_executed) >= 1
    assert set(active_executed["lifecycle_state"]) == {"contracted"}
    assert set(active_executed["status_rule"]) == {"caiso.active_ia_executed"}


def test_caiso_has_no_sponsor_column_so_sponsor_is_null(parsed):
    _, _, _, df = parsed
    assert df["sponsor_name"].isna().all()


# The bare parser frame over the committed fixture, pinned to what gridstatus 0.36.0's
# `get_interconnection_queue` returned for the same bytes before the library was dropped
# (2026-10-07, pipeline/vendor/gridstatus/README.md): shape, column order, dtypes, every value.
def _frame_digest(df: pd.DataFrame) -> str:
    h = hashlib.sha256()
    h.update(repr(list(df.columns)).encode())
    h.update(repr([str(t) for t in df.dtypes]).encode())
    h.update(pd.util.hash_pandas_object(df, index=True).values.tobytes())
    return h.hexdigest()[:16]


def test_vendored_parser_reproduces_the_gridstatus_0_36_frame():
    df = queues.caiso_queue(fixture_path("caiso_public_queue_report.xlsx").read_bytes())
    assert (df.shape, _frame_digest(df)) == ((39, 36), "8db2855cd8d87abe")


def test_a_renamed_column_fails_the_parse_closed():
    """A layout change surfaces as ParseError (upstream asserted; an assert vanishes under -O).
    Renamed in every sheet: the sheets are concatenated, so one sheet's rename alone leaves the
    column present (null for that sheet's rows), exactly as gridstatus did."""
    wb = openpyxl.load_workbook(fixture_path("caiso_public_queue_report.xlsx"))
    for ws in wb.worksheets:
        for cell in ws[4]:
            if cell.value == "Net MWs to Grid":
                cell.value = "Net MW to Grid"
    buf = io.BytesIO()
    wb.save(buf)
    c = connector_for(SOURCE_ID)
    raw = snapshot("caiso_public_queue_report.xlsx", URL)
    raw.content = buf.getvalue()
    with pytest.raises(ParseError, match="Net MWs to Grid"):
        c.parse(raw)


# ------------------------------------------------------------------ fetch (lane national-queues)
# Production (2026-10-23) had no successful CAISO or NYISO run and nothing recorded to say why.
# The fetch is now conditional on the last snapshot's validators, and a refusal names the edge.
WORKBOOK = fixture_path("caiso_public_queue_report.xlsx").read_bytes()


class _Answer:
    def __init__(self, status: int, content: bytes = b"", headers: dict[str, str] | None = None) -> None:
        self.status_code = status
        self.content = content
        self.headers = headers or {}


class _Http:
    def __init__(self, *answers: _Answer) -> None:
        self.answers = list(answers)
        self.calls: list[tuple[str, dict[str, str]]] = []

    def get(self, url: str, **kwargs: object) -> _Answer:
        headers = kwargs.get("headers") or {}
        self.calls.append((url, dict(headers)))  # type: ignore[call-overload]
        return self.answers.pop(0)


def _previous(content: bytes, meta: dict[str, str], url: str = URL) -> PreviousSnapshot:
    record = {
        "id": "run-1",
        "snapshot": {"sha256": hashlib.sha256(content).hexdigest(), "fetched_url": url, "meta": meta},
    }
    return PreviousSnapshot(record=record, load=lambda: content)


def test_a_first_fetch_is_unconditional_and_keeps_the_validators():
    http = _Http(_Answer(200, WORKBOOK, {"ETag": '"abc"', "Last-Modified": "Fri, 23 Oct 2026 08:03:52 GMT"}))
    c = connector_for(SOURCE_ID, http=http)
    raw = c.fetch()
    assert http.calls == [(URL, {})]
    assert raw.content == WORKBOOK and raw.http_status == 200
    assert raw.meta == {
        "requested_url": URL,
        "etag": '"abc"',
        "last_modified": "Fri, 23 Oct 2026 08:03:52 GMT",
    }


def test_a_304_reuses_the_stored_workbook_in_one_request():
    http = _Http(_Answer(304))
    c = connector_for(SOURCE_ID, http=http)
    c.previous = _previous(WORKBOOK, {"etag": '"abc"', "last_modified": "Thu, 22 Oct 2026 08:00:00 GMT"})
    raw = c.fetch()
    assert http.calls == [
        (URL, {"If-None-Match": '"abc"', "If-Modified-Since": "Thu, 22 Oct 2026 08:00:00 GMT"})
    ]
    assert raw.content == WORKBOOK  # the runner's SHA comparison then records `unchanged`
    assert raw.meta["not_modified"] is True and raw.meta["etag"] == '"abc"'
    assert raw.requests_made == 1


def test_a_304_with_an_unreadable_stored_object_downloads_once():
    http = _Http(_Answer(304), _Answer(200, WORKBOOK, {"ETag": '"new"'}))
    c = connector_for(SOURCE_ID, http=http)
    c.previous = _previous(b"not the bytes the record names", {"etag": '"abc"'})
    c.previous.record["snapshot"]["sha256"] = "0" * 64
    raw = c.fetch()
    assert [h for _, h in http.calls] == [{"If-None-Match": '"abc"'}, {}]
    assert raw.content == WORKBOOK and raw.requests_made == 2 and "not_modified" not in raw.meta


def test_validators_of_another_url_are_not_sent():
    http = _Http(_Answer(200, WORKBOOK))
    c = connector_for(SOURCE_ID, http=http)
    c.previous = _previous(WORKBOOK, {"etag": '"abc"'}, url="https://www.caiso.com/elsewhere.xlsx")
    c.fetch()
    assert http.calls == [(URL, {})]


def test_a_403_from_the_edge_is_blocked_and_names_who_refused():
    """The record says which edge answered, so the admin source-health view can tell a WAF block
    from an outage without anyone reproducing the request."""
    page = (
        b"<html><head><title>ERROR: The request could not be satisfied</title></head><body>403</body></html>"
    )
    headers = {
        "Server": "CloudFront",
        "X-Cache": "Error from cloudfront",
        "X-Amz-Cf-Pop": "IAD61-P10",
        "Content-Type": "text/html",
        "Set-Cookie": "never-recorded",
    }
    c = connector_for(SOURCE_ID, http=_Http(_Answer(403, page, headers)))
    with pytest.raises(BlockedError) as e:
        c.fetch()
    message = str(e.value)
    assert "HTTP 403" in message and "server=CloudFront" in message
    assert "x-cache=Error from cloudfront" in message and "x-amz-cf-pop=IAD61-P10" in message
    assert "The request could not be satisfied" in message
    assert "never-recorded" not in message


def test_a_server_error_is_a_failure_not_a_block():
    c = connector_for(SOURCE_ID, http=_Http(_Answer(502, b"", {"Server": "caiso"})))
    with pytest.raises(ConnectorError) as e:
        c.fetch()
    assert not isinstance(e.value, BlockedError)
    assert "HTTP 502" in str(e.value) and "server=caiso" in str(e.value)


def test_a_200_html_page_is_a_parse_error_with_the_page_named():
    http = _Http(_Answer(200, b"<html>Maintenance</html>", {"Content-Type": "text/html"}))
    c = connector_for(SOURCE_ID, http=http)
    with pytest.raises(ParseError, match="Maintenance"):
        c.fetch()


def test_describe_response_skips_binary_bodies_and_absent_headers():
    assert describe_response(404, {"content-type": "application/octet-stream"}, b"PK\x03\x04") == (
        "HTTP 404; content-type=application/octet-stream"
    )
    assert describe_response(403, {}, b"") == "HTTP 403"


class _Edge(requests.adapters.BaseAdapter):
    """A transport that answers from a list instead of the network (the socket guard stays on):
    robots.txt is 404, every other request takes the next (status, headers, body)."""

    def __init__(self, *answers: tuple[int, dict[str, str], bytes]) -> None:
        super().__init__()
        self.answers = list(answers)
        self.sent: list[str] = []

    def send(self, request, **kwargs):  # type: ignore[no-untyped-def, override]
        self.sent.append(str(request.url))
        status, headers, body = (
            (404, {}, b"") if str(request.url).endswith("/robots.txt") else self.answers.pop(0)
        )
        resp = requests.Response()
        resp.status_code, resp._content, resp.url, resp.request = status, body, request.url, request
        resp.headers.update(headers)
        return resp

    def close(self) -> None:
        return None


def _polite(edge: _Edge) -> PoliteSession:
    session = requests.Session()
    session.mount("https://", edge)
    return PoliteSession(session=session, sleep=lambda _s: None, max_attempts=2)


def test_a_5xx_that_outlasts_the_retries_names_the_edge_too():
    """Review nit, 2026-10-23: a 503 the session retried and gave up on surfaced as the bare
    "HTTP 503" of `HttpFailed`, with none of the edge headers a refusal records."""
    unavailable = (
        503,
        {"Server": "CloudFront", "X-Cache": "Error from cloudfront", "Content-Type": "text/html"},
        b"<html><body>503 Service Unavailable</body></html>",
    )
    edge = _Edge(unavailable, unavailable)
    c = connector_for(SOURCE_ID, http=_polite(edge))
    with pytest.raises(ConnectorError) as e:
        c.fetch()
    assert not isinstance(e.value, BlockedError)
    message = str(e.value)
    assert "after 2 answers" in message and "HTTP 503" in message and "server=CloudFront" in message
    assert "x-cache=Error from cloudfront" in message and "503 Service Unavailable" in message
    assert edge.sent.count(URL) == 2


def test_a_challenge_page_is_blocked_and_names_the_edge():
    challenge = (
        403,
        {"Server": "cloudflare", "cf-mitigated": "challenge", "cf-ray": "abc-IAD"},
        b"Just a moment...",
    )
    c = connector_for(SOURCE_ID, http=_polite(_Edge(challenge)))
    with pytest.raises(BlockedError) as e:
        c.fetch()
    assert "challenge page" in str(e.value) and "cf-ray=abc-IAD" in str(e.value)
