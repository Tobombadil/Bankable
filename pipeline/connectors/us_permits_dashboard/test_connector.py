"""Parser tests for us.permits_dashboard against a recorded Permitting Dashboard CSV."""

from __future__ import annotations

import csv
import datetime as dt
import io
import json
import pathlib
import tempfile
from typing import Any, ClassVar

import pandas as pd
import pytest
import requests
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.x509.verification import PolicyBuilder, Store, VerificationError

from conftest import connector_for, fixture_path, snapshot
from pipeline.connectors import http as http_module
from pipeline.connectors.base import ParseError, RawSnapshot
from pipeline.connectors.us_permits_dashboard.connector import TLS_INTERMEDIATES, withhold_persons

SOURCE_ID = "us.permits_dashboard"
URL = "https://data.permits.performance.gov/api/views/mcm3-xbid/rows.csv?accessType=DOWNLOAD"


@pytest.fixture(scope="module")
def parsed():
    c = connector_for(SOURCE_ID)
    raw = snapshot("permits_dashboard_projects.csv", URL, "text/csv")
    rows = c.parse(raw)
    return c, raw, rows, c.normalize(rows, raw)


def test_csv_groups_milestone_rows_into_one_row_per_project(parsed):
    """The fixture carries 12 distinct Project IDs; 2 (Aviation, Surface Transportation) are
    out of scope, so 10 project-level records come out of 224 milestone-level CSV rows."""
    _, _, rows, df = parsed
    assert len(rows) == 10
    assert len(df) == 10
    assert df["record_id"].is_unique


def test_out_of_scope_sectors_are_dropped(parsed):
    _, _, rows, _ = parsed
    assert {r["Project Sector"] for r in rows} <= {
        "Renewable Energy Production",
        "Electricity Transmission",
        "Pipelines",
        "Energy Storage",
        "Carbon Capture and Sequestration",
    }
    assert "72011" not in [r["Project ID"] for r in rows]  # Aviation
    assert "72861" not in [r["Project ID"] for r in rows]  # Surface Transportation


def test_milestones_are_nested_in_the_raw_payload(parsed):
    _, _, _rows, df = parsed
    row = df[df["source_record_id"] == "72996"].iloc[0]
    raw = json.loads(row["raw"])
    assert len(raw["milestones"]) == 21
    assert {"Milestone ID", "Milestone Type", "Action Status"} <= set(raw["milestones"][0])


def test_lifecycle_state_derives_from_project_status(parsed):
    _, _, _, df = parsed
    by_id = df.set_index("source_record_id")
    assert by_id.loc["72996", "lifecycle_state"] == "permitted"  # Complete
    assert by_id.loc["73756", "lifecycle_state"] == "studied"  # Paused
    assert by_id.loc["119321", "lifecycle_state"] == "announced"  # Planned
    assert by_id.loc["71031", "lifecycle_state"] == "cancelled"  # Cancelled
    assert by_id.loc["74081", "lifecycle_state"] == "studied"  # In Progress
    assert df["status_rule"].str.startswith("permits_dashboard.").all()
    assert set(df["lifecycle_state"]) <= {"announced", "filed", "studied", "permitted", "cancelled"}


def test_class_of_action_changed_maps_to_filed(parsed):
    c, _, _, _ = parsed
    from pipeline.connectors.canonical import harmonise_status

    state, rule = harmonise_status(c.status_key, {"status_raw": "Class of Action Changed"}, c.status_map)
    assert (state, rule) == ("filed", "permits_dashboard.map")


def test_location_is_the_state_column(parsed):
    _, _, _, df = parsed
    by_id = df.set_index("source_record_id")
    assert by_id.loc["72996", "state"] == "WY"


def test_kind_and_technology_come_from_project_type(parsed):
    _, _, _, df = parsed
    by_id = df.set_index("source_record_id")
    assert by_id.loc["71286", "kind"] == "transmission"
    assert by_id.loc["73651", "technology_raw"] == "Interstate Natural Gas Pipelines"


def test_capacity_is_never_populated_and_not_a_required_field(parsed):
    c, _, _, df = parsed
    assert df["capacity_mw"].isna().all()
    assert "capacity_mw" not in c.dq_required_fields


def test_source_url_is_the_per_project_permits_page(parsed):
    _, _, _, df = parsed
    row = df[df["source_record_id"] == "72996"].iloc[0]
    assert row["source_url"].startswith("https://www.permits.performance.gov/permitting-project/")


def test_a_layout_change_fails_closed():
    c = connector_for(SOURCE_ID)
    raw = snapshot("permits_dashboard_projects.csv", URL, "text/csv")
    bad = raw.__class__(
        content=b"Not,The,Right,Columns\n1,2,3,4\n",
        content_type="text/csv",
        url=URL,
        retrieved_at=raw.retrieved_at,
        http_status=200,
        ext="csv",
    )
    with pytest.raises(ParseError):
        c.parse(bad)


# ------------------------------------------------------------------ TLS (2026-10-24, docs/28 lane F5)
#: The chain the host served on 2026-10-24 (its certificate, then an intermediate that did not issue
#: it), recorded unedited; certifi supplies the roots. No network.
SERVED_CHAIN = pathlib.Path(__file__).with_name("fixtures") / "served_chain_2026-10-24.pem"
HOST = "data.permits.performance.gov"
VENDORED_SHA256 = "d2ac9dcc7a255029a5f5180d63f1a9ed60e11412a85c148d4b71f6ae1a36695c"
AT = dt.datetime(2026, 10, 24, 2, 22, tzinfo=dt.UTC)


def _certs(path: pathlib.Path) -> list[x509.Certificate]:
    return x509.load_pem_x509_certificates(path.read_bytes())


def _certifi_root(label: str) -> x509.Certificate:
    text = pathlib.Path(requests.certs.where()).read_text()
    block = text.split(f'# Label: "{label}"', 1)[1]
    begin = block.index("-----BEGIN CERTIFICATE-----")
    end = block.index("-----END CERTIFICATE-----") + len("-----END CERTIFICATE-----")
    return x509.load_pem_x509_certificate(block[begin:end].encode())


def _verifier() -> Any:
    store = Store([_certifi_root("GlobalSign Root R46"), _certifi_root("GlobalSign Root CA - R6")])
    return PolicyBuilder().store(store).time(AT).build_server_verifier(x509.DNSName(HOST))


def test_the_host_sends_an_intermediate_that_did_not_issue_its_certificate():
    """The production failure, offline: with the chain as served, no path reaches a root, which
    OpenSSL reports as "unable to get local issuer certificate"."""
    leaf, served = _certs(SERVED_CHAIN)
    assert leaf.issuer != served.subject
    assert "GCC R46 AlphaSSL CA 2025" in leaf.issuer.rfc4514_string()
    assert "GCC R6 AlphaSSL CA 2025" in served.subject.rfc4514_string()
    with pytest.raises(VerificationError):
        _verifier().verify(leaf, [served])


def test_the_vendored_ca_issued_the_hosts_certificate_and_chains_to_a_certifi_root():
    leaf, served = _certs(SERVED_CHAIN)
    (ca,) = _certs(TLS_INTERMEDIATES[0])
    assert ca.fingerprint(hashes.SHA256()).hex() == VENDORED_SHA256
    assert ca.subject == leaf.issuer
    assert ca.issuer.rfc4514_string() == "CN=GlobalSign Root R46,O=GlobalSign nv-sa,C=BE"
    chain = _verifier().verify(leaf, [served, ca])
    root = _certifi_root("GlobalSign Root R46")
    assert [c.subject for c in chain] == [leaf.subject, ca.subject, root.subject]


class _CsvResponse:
    status_code = 200
    headers: ClassVar[dict[str, str]] = {"Content-Type": "text/csv"}

    def __init__(self, body: bytes) -> None:
        self.content = body


class _RecordingHttp:
    def __init__(self, body: bytes) -> None:
        self.body = body
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get(self, url: str, **kwargs: Any) -> _CsvResponse:
        self.calls.append((url, kwargs))
        return _CsvResponse(self.body)


def test_fetch_verifies_against_the_usual_bundle_plus_the_vendored_ca(monkeypatch, tmp_path):
    monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)
    monkeypatch.delenv("CURL_CA_BUNDLE", raising=False)
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(http_module, "_BUNDLES", {})
    monkeypatch.setattr(http_module, "_BUNDLE_DIR", None)
    http = _RecordingHttp(fixture_path("permits_dashboard_projects.csv").read_bytes())
    raw = connector_for(SOURCE_ID, http=http).fetch()
    ((url, kwargs),) = http.calls
    assert url == URL and kwargs["honour_robots"] is False
    verify = kwargs["verify"]
    assert isinstance(verify, str) and verify.startswith(str(tmp_path))  # a CA file, never False
    bundle = pathlib.Path(verify).read_bytes()
    assert bundle.startswith(pathlib.Path(requests.certs.where()).read_bytes().rstrip(b"\n"))
    (ca,) = _certs(TLS_INTERMEDIATES[0])
    assert ca.public_bytes(serialization.Encoding.PEM).strip() in bundle
    assert len(connector_for(SOURCE_ID).parse(raw)) == 10


# ------------------------------------------------------------------ natural persons as sponsors
PERSON = "Jane Example"  # made up; services.personal_names reads it as a person


def _with_sponsor(project_id: str, sponsor: str) -> bytes:
    """The recorded CSV with one project's sponsor cell rewritten in memory (every milestone row)."""
    text = fixture_path("permits_dashboard_projects.csv").read_bytes().decode("utf-8-sig")
    rows = list(csv.reader(io.StringIO(text, newline="")))
    pid, col = rows[0].index("Project ID"), rows[0].index("Project Sponsor")
    for row in rows[1:]:
        if row[pid] == project_id:
            row[col] = sponsor
    out = io.StringIO(newline="")
    csv.writer(out, lineterminator="\n").writerows(rows)
    return out.getvalue().encode("utf-8")


def _raw(content: bytes) -> RawSnapshot:
    return RawSnapshot(
        content=content, content_type="text/csv", url=URL, retrieved_at=AT, http_status=200, ext="csv"
    )


@pytest.mark.parametrize(("sponsor", "kept"), [(PERSON, None), (f"Acme Wind LLC;{PERSON}", "Acme Wind LLC")])
def test_a_person_named_as_sponsor_never_reaches_the_snapshot_or_the_rows(sponsor, kept):
    content = _with_sponsor("72996", sponsor)
    stored = connector_for(SOURCE_ID).redact(content)
    assert PERSON.encode() not in stored
    for body in (stored, content):  # redacted, and a snapshot stored before `redact` existed
        c = connector_for(SOURCE_ID)
        if body is stored:
            c.redact(content)  # a run calls redact, then parse, on one connector
        raw = _raw(body)
        df = c.normalize(c.parse(raw), raw)
        row = df[df["source_record_id"] == "72996"].iloc[0]
        assert (row["sponsor_name"] if pd.notna(row["sponsor_name"]) else None) == kept
        assert not df["raw"].str.contains(PERSON).any()
        assert raw.meta["sponsors_withheld_as_persons"] == 1
        others = df[df["source_record_id"] != "72996"]
        assert (
            others["sponsor_name"].notna().sum()
            == connector_for(SOURCE_ID)
            .normalize(*_parsed_untouched())
            .query("source_record_id != '72996'")["sponsor_name"]
            .notna()
            .sum()
        )


def _parsed_untouched() -> tuple[list[dict[str, Any]], RawSnapshot]:
    raw = snapshot("permits_dashboard_projects.csv", URL, "text/csv")
    return connector_for(SOURCE_ID).parse(raw), raw


def test_redact_changes_nothing_when_no_sponsor_is_a_person():
    content = fixture_path("permits_dashboard_projects.csv").read_bytes()
    assert connector_for(SOURCE_ID).redact(content) == content
    assert withhold_persons("Idaho Power ;Idaho Power Company") == ("Idaho Power ;Idaho Power Company", 0)
    assert withhold_persons("  ") == (None, 0)
    assert withhold_persons(None) == (None, 0)
