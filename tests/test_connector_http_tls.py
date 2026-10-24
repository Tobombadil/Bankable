"""`pipeline/connectors/http.py`, 2026-10-24 (docs/28 lane F5): a certificate that does not verify is
recorded once, not retried; no error carries a query string; `ca_bundle_with` adds a CA to the
bundle requests would use. The rest of the module's politeness is `tests/test_connector_http.py`.

No network: a stub session raises what `requests` raises."""

from __future__ import annotations

import pathlib
import stat
import tempfile
from typing import Any

import certifi
import pytest
import requests

from pipeline.connectors import http as http_module
from pipeline.connectors.http import (
    HttpBlocked,
    HttpFailed,
    HttpTlsUnverified,
    PoliteSession,
    ca_bundle_with,
    safe_url,
    strip_queries,
    tls_verify_failure,
)

#: What requests raised for data.permits.performance.gov from production and from the sandbox.
VERIFY_FAILED = (
    "HTTPSConnectionPool(host='data.permits.performance.gov', port=443): Max retries exceeded with url: "
    "/api/views/mcm3-xbid/rows.csv?accessType=DOWNLOAD (Caused by SSLError(SSLCertVerificationError(1, "
    "'[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: unable to get local issuer certificate "
    "(_ssl.c:1006)')))"
)
URL = "https://data.permits.performance.gov/api/views/mcm3-xbid/rows.csv?accessType=DOWNLOAD"


class _Raising:
    def __init__(self, exc: Exception) -> None:
        self.exc = exc
        self.calls = 0
        self.headers: dict[str, str] = {}

    def request(self, method: str, url: str, **kwargs: Any) -> Any:
        self.calls += 1
        raise self.exc


def _session(exc: Exception) -> tuple[PoliteSession, _Raising, list[float]]:
    slept: list[float] = []
    stub = _Raising(exc)
    return PoliteSession(session=stub, sleep=slept.append), stub, slept  # type: ignore[arg-type]


def test_a_certificate_that_does_not_verify_is_raised_once_and_never_retried() -> None:
    ps, stub, slept = _session(requests.exceptions.SSLError(VERIFY_FAILED))
    with pytest.raises(HttpTlsUnverified) as caught:
        ps.get(URL, honour_robots=False)
    assert stub.calls == 1 and slept == []
    message = str(caught.value)
    assert "data.permits.performance.gov" in message
    assert "unable to get local issuer certificate" in message
    assert "accessType" not in message
    assert isinstance(caught.value, HttpFailed)  # the runner still records it as a failed run


def test_another_tls_error_is_still_retried() -> None:
    eof = requests.exceptions.SSLError("EOF occurred in violation of protocol (_ssl.c:2427)")
    ps, stub, _slept = _session(eof)
    with pytest.raises(HttpFailed) as caught:
        ps.get(URL, honour_robots=False)
    assert not isinstance(caught.value, HttpTlsUnverified)
    assert stub.calls == ps.max_attempts


def test_no_error_carries_a_query_string() -> None:
    secret = "api_key=" + "k" * 24
    exc = requests.exceptions.ConnectionError(
        f"Max retries exceeded with url: /v2/electricity?{secret} (Caused by"
    )
    ps, _, _ = _session(exc)
    with pytest.raises(HttpFailed) as caught:
        ps.get(f"https://api.eia.gov/v2/electricity?{secret}", honour_robots=False)
    assert secret not in str(caught.value)
    assert "https://api.eia.gov/v2/electricity?…" in str(caught.value)
    assert "/v2/electricity?…" in str(caught.value).split("attempts:", 1)[1]


@pytest.mark.parametrize(
    ("url", "safe"),
    [
        ("https://u:p@host.example:8443/a/b?k=v#f", "https://host.example:8443/a/b?…"),
        ("https://host.example/a", "https://host.example/a"),
        ("/relative?x=1", "/relative?…"),
    ],
)
def test_safe_url_drops_credentials_query_and_fragment(url: str, safe: str) -> None:
    assert safe_url(url) == safe


def test_strip_queries_reads_urls_inside_text() -> None:
    text = "GET wss://h.example/app/1?token=abc -> 403; with url: /p/q?sig=1 (Caused by x)"
    assert strip_queries(text) == "GET wss://h.example/app/1?… -> 403; with url: /p/q?… (Caused by x)"


def test_tls_verify_failure_names_openssls_reason_only_for_verification() -> None:
    assert tls_verify_failure(requests.exceptions.SSLError(VERIFY_FAILED)) == (
        "unable to get local issuer certificate"
    )
    expired = (
        "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: certificate has expired (_ssl.c:1006)"
    )
    assert tls_verify_failure(Exception(expired)) == "certificate has expired"
    assert tls_verify_failure(Exception("EOF occurred in violation of protocol")) is None


def test_ca_bundle_with_appends_to_the_bundle_requests_would_use(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    base = tmp_path / "proxy-ca.pem"
    base.write_bytes(b"-----BEGIN CERTIFICATE-----\nBASE\n-----END CERTIFICATE-----\n")
    extra = tmp_path / "extra.pem"
    extra.write_bytes(b"# a comment\n-----BEGIN CERTIFICATE-----\nEXTRA\n-----END CERTIFICATE-----\n")
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(base))
    monkeypatch.delenv("CURL_CA_BUNDLE", raising=False)
    (tmp_path / "tmp").mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "tmp"))
    monkeypatch.setattr(http_module, "_BUNDLES", {})
    monkeypatch.setattr(http_module, "_BUNDLE_DIR", None)
    path = ca_bundle_with(extra)
    body = pathlib.Path(path).read_bytes()
    assert body.startswith(base.read_bytes()) and body.endswith(extra.read_bytes())
    assert ca_bundle_with(extra) == path  # once per process
    folder = pathlib.Path(path).parent
    assert folder.parent == tmp_path / "tmp" and stat.S_IMODE(folder.stat().st_mode) == 0o700  # private
    monkeypatch.delenv("REQUESTS_CA_BUNDLE")
    assert (
        pathlib.Path(ca_bundle_with(extra))
        .read_bytes()
        .startswith(pathlib.Path(certifi.where()).read_bytes().rstrip(b"\n"))
    )
    empty = tmp_path / "empty.pem"
    empty.write_text("# no certificate here\n")
    with pytest.raises(ValueError, match="no PEM certificate"):
        ca_bundle_with(empty)


def test_a_bundle_changed_after_it_was_written_is_written_again(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    extra = tmp_path / "extra.pem"
    extra.write_bytes(b"-----BEGIN CERTIFICATE-----\nEXTRA\n-----END CERTIFICATE-----\n")
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(http_module, "_BUNDLES", {})
    monkeypatch.setattr(http_module, "_BUNDLE_DIR", None)
    path = pathlib.Path(ca_bundle_with(extra))
    good = path.read_bytes()
    path.write_bytes(good + b"-----BEGIN CERTIFICATE-----\nPLANTED\n-----END CERTIFICATE-----\n")
    assert pathlib.Path(ca_bundle_with(extra)).read_bytes() == good
    path.unlink()
    assert pathlib.Path(ca_bundle_with(extra)).read_bytes() == good


def test_a_robots_refusal_carries_no_query_string() -> None:
    class _Robots:
        def __init__(self) -> None:
            self.headers: dict[str, str] = {}

        def get(self, url: str, **kwargs: Any) -> Any:
            response = requests.Response()
            response.status_code = 200
            response._content = b"User-agent: *\nDisallow: /\n"
            return response

    ps = PoliteSession(session=_Robots(), sleep=lambda _s: None)  # type: ignore[arg-type]
    with pytest.raises(HttpBlocked) as caught:
        ps.get("https://h.example/data?api_key=SECRET123")
    assert "SECRET123" not in str(caught.value) and "https://h.example/data?…" in str(caught.value)


def test_a_request_level_verify_reaches_the_session() -> None:
    seen: dict[str, Any] = {}

    class _Ok:
        def __init__(self) -> None:
            self.headers: dict[str, str] = {}

        def request(self, method: str, url: str, **kwargs: Any) -> Any:
            seen.update(kwargs)
            response = requests.Response()
            response.status_code = 200
            response._content = b"ok"
            return response

    PoliteSession(session=_Ok(), sleep=lambda _s: None).get(URL, honour_robots=False, verify="/x.pem")  # type: ignore[arg-type]
    assert seen["verify"] == "/x.pem"
