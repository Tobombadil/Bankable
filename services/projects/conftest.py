"""Fixtures for the private-project tests: a fresh SQLite store with the private tables, users in
every state the authorisation rules distinguish, and the `AuthContext` each would arrive with. For
the data room: `room_env` (switch on, a local store, the test key) and `s3_standin`, a local
S3-compatible object-store stand-in on loopback."""

from __future__ import annotations

import base64
import datetime as dt
import http.server
import pathlib
import threading
import urllib.parse
from collections.abc import Callable, Iterator

import pytest
from sqlalchemy.orm import Session, sessionmaker

import services.projects.models  # noqa: F401 -- registers the private tables before the template is built
from services.api.auth import AuthContext, generate_api_key, register_user
from services.db.models import ApiKey, User
from services.db.session import get_sessionmaker
from services.ids import public_id
from tests.db_template import disposing, fresh_engine

VERIFIED_AT = dt.datetime(2026, 10, 1, tzinfo=dt.UTC)
TEST_PASSWORD = "correct horse battery"  # noqa: S105 -- a fixture value, not a credential

UserFactory = Callable[..., User]


@pytest.fixture(autouse=True)
def _test_pepper(monkeypatch: pytest.MonkeyPatch) -> None:
    """Address hashes are peppered (`services.api.audit.hash_identifier`); tests use their own
    obviously fake pepper rather than the development fallback."""
    monkeypatch.setenv("AUDIT_HASH_PEPPER", "test-pepper-private-projects")


@pytest.fixture()
def db_sessionmaker() -> Iterator[sessionmaker[Session]]:
    with disposing(fresh_engine()) as engine:
        yield get_sessionmaker(engine)


@pytest.fixture()
def db(db_sessionmaker: sessionmaker[Session]) -> Iterator[Session]:
    with db_sessionmaker() as session:
        yield session


@pytest.fixture()
def make_user(db: Session) -> UserFactory:
    counter = iter(range(1, 10_000))

    def _make(
        email: str | None = None,
        *,
        verified: bool = True,
        status: str = "active",
        role: str = "member",
        name: str | None = None,
    ) -> User:
        n = next(counter)
        user = register_user(
            db,
            email=email or f"user{n}@example.org",
            password=TEST_PASSWORD,
            name=name or f"User {n}",
        )
        user.email_verified_at = VERIFIED_AT if verified else None
        user.role = role
        if status != "active":
            user.status = status
            if status == "anonymised":
                user.anonymised_at = VERIFIED_AT
        db.flush()
        return user

    return _make


def session_ctx(user: User) -> AuthContext:
    """What `services.api.auth.build_auth_context` builds for a valid session cookie."""
    return AuthContext(
        user=user,
        account=user.account,
        api_key=None,
        entitlement=user.account.entitlement,
        scopes=frozenset(),
    )


def api_key_ctx(
    db: Session, user: User, *, scopes: tuple[str, ...] = ("admin:*", "read:live")
) -> AuthContext:
    """What a bearer key resolves to: no user, a key, its account."""
    _, key_hash, last4 = generate_api_key()
    key = ApiKey(
        public_id="",
        account_id=user.account_id,
        created_by_user_id=user.id,
        name="test key",
        last4=last4,
        key_hash=key_hash,
        scopes=list(scopes),
        tier="api",
    )
    db.add(key)
    db.flush()
    key.public_id = public_id("key", key.id)
    db.flush()
    return AuthContext(
        user=None, account=user.account, api_key=key, entitlement="api", scopes=frozenset(scopes)
    )


# ------------------------------------------------------------------------------ data rooms
#: A fixed test key (32 bytes, base64): obviously not a deployment's.
TEST_DATA_ROOM_KEY = base64.urlsafe_b64encode(b"data-room-test-key-32-bytes-long").decode()


@pytest.fixture()
def room_env(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> pathlib.Path:
    """Data rooms on, a local store under `tmp_path`, the test key, no scanner. Returns the store root."""
    root = tmp_path / "data-room-store"
    monkeypatch.setenv("DATA_ROOMS", "on")
    monkeypatch.setenv("DATA_ROOM_STORE", "local")
    monkeypatch.setenv("DATA_ROOM_LOCAL_DIR", str(root))
    monkeypatch.setenv("DATA_ROOM_KEY", TEST_DATA_ROOM_KEY)
    monkeypatch.delenv("DATA_ROOM_SCANNER", raising=False)
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    return root


class S3StandIn:
    """A local object-store stand-in speaking just enough of the S3 API for the data room (PUT, GET,
    HEAD, DELETE of an object; ListObjectsV2 by prefix), on 127.0.0.1, so the real S3 client path is
    exercised without the network. Authentication is not checked."""

    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, dt.datetime]] = {}
        self.requests: list[tuple[str, str]] = []
        standin = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args: object) -> None:
                return

            def _key(self) -> tuple[str, dict[str, list[str]]]:
                parsed = urllib.parse.urlsplit(self.path)
                parts = parsed.path.lstrip("/").split("/", 1)
                key = urllib.parse.unquote(parts[1]) if len(parts) > 1 else ""
                return key, urllib.parse.parse_qs(parsed.query)

            def _send(self, status: int, body: bytes = b"", content_type: str = "application/xml") -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("ETag", '"0"')
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            def do_PUT(self) -> None:
                key, _ = self._key()
                length = int(self.headers.get("Content-Length") or 0)
                standin.objects[key] = (self.rfile.read(length), dt.datetime.now(dt.UTC))
                standin.requests.append(("PUT", key))
                self._send(200)

            def do_GET(self) -> None:
                key, query = self._key()
                if not key and "list-type" in query:
                    prefix = (query.get("prefix") or [""])[0]
                    rows = "".join(
                        f"<Contents><Key>{k}</Key><LastModified>{at.strftime('%Y-%m-%dT%H:%M:%S.000Z')}"
                        f'</LastModified><Size>{len(data)}</Size><ETag>"0"</ETag></Contents>'
                        for k, (data, at) in sorted(standin.objects.items())
                        if k.startswith(prefix)
                    )
                    body = (
                        '<?xml version="1.0" encoding="UTF-8"?><ListBucketResult>'
                        f"<Name>b</Name><Prefix>{prefix}</Prefix><KeyCount>{rows.count('<Key>')}</KeyCount>"
                        f"<MaxKeys>1000</MaxKeys><IsTruncated>false</IsTruncated>{rows}</ListBucketResult>"
                    ).encode()
                    standin.requests.append(("LIST", prefix))
                    self._send(200, body)
                    return
                standin.requests.append(("GET", key))
                if key not in standin.objects:
                    self._send(404, b"<Error><Code>NoSuchKey</Code></Error>")
                    return
                self._send(200, standin.objects[key][0], "application/octet-stream")

            def do_HEAD(self) -> None:
                key, _ = self._key()
                self._send(200 if key in standin.objects else 404)

            def do_DELETE(self) -> None:
                key, _ = self._key()
                standin.objects.pop(key, None)
                standin.requests.append(("DELETE", key))
                self._send(204)

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.endpoint = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def env(self, bucket: str = "private-bucket") -> dict[str, str]:
        return {
            "R2_BUCKET": bucket,
            "R2_ACCESS_KEY_ID": "test-access-key",
            "R2_SECRET_ACCESS_KEY": "test-secret-key",
            "R2_ENDPOINT": self.endpoint,
        }


@pytest.fixture()
def s3_standin() -> Iterator[S3StandIn]:
    standin = S3StandIn()
    standin.thread.start()
    try:
        yield standin
    finally:
        standin.server.shutdown()
        standin.server.server_close()
