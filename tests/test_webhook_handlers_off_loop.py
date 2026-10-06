"""A slow provider call inside a webhook handler does not stall other requests (backend audit
2026-09-30 F7).

The Stripe and Attio handlers were `async def` and called synchronous adapters (30 s timeouts,
`time.sleep` back-off) and a synchronous session on the event loop. The audit measured `/v1/health`
at 2.70 s during a 3 s webhook, against 0.012 s alone. Here the provider call blocks until the test
releases it; a concurrent request must complete while it is still blocked. On the base tree the
concurrent request waited for the handler.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from services.api.app import app
from services.sor.wiring import get_billing_port, get_crm_port

#: How long the blocked handler waits before giving up on its own (a safety net, never reached
#: when the fix holds).
HANDLER_WAIT_S = 5.0
#: How long the concurrent request may take while the handler is blocked.
CONCURRENT_BUDGET_S = 2.0


class _BlockingPort:
    """Stands in for both ports: the webhook parse blocks until released."""

    sor_kind = "stripe"
    live = False

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()

    def _block(self) -> list[Any]:
        self.entered.set()
        self.release.wait(HANDLER_WAIT_S)
        return []

    def handle_webhook(self, *, body: bytes, headers: Any) -> list[Any]:
        return self._block()

    def parse_webhook(self, *, body: bytes, headers: Any) -> list[Any]:
        return self._block()


@pytest.fixture()
def blocking(client: TestClient) -> Iterator[_BlockingPort]:
    port = _BlockingPort()
    app.dependency_overrides[get_billing_port] = lambda: port
    app.dependency_overrides[get_crm_port] = lambda: port
    yield port
    port.release.set()


@pytest.mark.parametrize("path", ["/webhooks/stripe", "/webhooks/attio"])
def test_a_blocked_webhook_handler_does_not_stall_a_concurrent_request(
    client: TestClient, blocking: _BlockingPort, path: str
) -> None:
    webhook: dict[str, Any] = {}

    def post_webhook() -> None:
        webhook["response"] = client.post(path, content=b"{}", headers={"Stripe-Signature": "t=1,v1=x"})

    other: dict[str, Any] = {}

    def get_other() -> None:
        other["response"] = client.get("/v1/licences")

    hook = threading.Thread(target=post_webhook)
    hook.start()
    try:
        assert blocking.entered.wait(HANDLER_WAIT_S), "the webhook handler never reached the provider call"
        concurrent = threading.Thread(target=get_other)
        concurrent.start()
        concurrent.join(CONCURRENT_BUDGET_S)
        finished_while_blocked = not concurrent.is_alive()
    finally:
        blocking.release.set()
        hook.join(HANDLER_WAIT_S * 2)
    concurrent.join(HANDLER_WAIT_S * 2)
    assert finished_while_blocked, "a concurrent request waited for the blocked webhook handler"
    assert other["response"].status_code == 200
    assert webhook["response"].status_code == 200
