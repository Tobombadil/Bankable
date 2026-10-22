"""`/contact` and `/bot`: the two pages the data reader's User-Agent names and every outbound
permission request points to (docs/39 §0.3 found both answering 404 on 2026-10-21)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from pipeline.connectors.http import robots_token, user_agent
from services.api.common import DOMAIN
from web.app import app


@pytest.fixture()
def client() -> TestClient:
    with TestClient(app) as c:
        yield c


def test_the_user_agents_two_links_answer(client: TestClient) -> None:
    ua = user_agent(DOMAIN)
    assert f"https://{DOMAIN}/bot" in ua and f"https://{DOMAIN}/contact" in ua
    for path in ("/bot", "/contact"):
        assert client.get(path).status_code == 200, path


def test_the_bot_page_names_the_reader_as_it_identifies_itself(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("PRODUCT_CONTACT_EMAIL", "reader@example.org")
    html = client.get("/bot").text
    token = robots_token(user_agent(DOMAIN))
    assert token == "Inframation"
    assert f"User-agent: {token}" in html
    assert user_agent(DOMAIN).replace("&", "&amp;") in html
    assert 'href="mailto:reader@example.org"' in html


def test_the_contact_page_gives_the_address_and_the_operator_when_set(
    client: TestClient, monkeypatch
) -> None:
    monkeypatch.setenv("PRODUCT_CONTACT_EMAIL", "hello@example.org")
    monkeypatch.setenv("SENDER_LEGAL_NAME", "Example Operator LLC")
    monkeypatch.setenv("SENDER_POSTAL_ADDRESS", "1 Example St, Denver, CO")
    html = client.get("/contact").text
    assert 'href="mailto:hello@example.org"' in html
    assert "Example Operator LLC" in html and "1 Example St, Denver, CO" in html
    assert 'href="/bot"' in html and 'href="/privacy/request"' in html
