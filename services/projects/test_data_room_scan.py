"""The malware-scanning hook (`services/projects/data_room_scan.py`): off unless configured, and a
clamd adapter driven here against a fake clamd that speaks `INSTREAM`. No antivirus engine runs in
the tests; the fake flags a marker, never real malware (no test signature is committed)."""

from __future__ import annotations

import socket
import socketserver
import struct
import threading
from collections.abc import Iterator

import pytest

from services.projects import data_room_scan as scan

MARKER = b"FAKE-MALWARE-MARKER-FOR-TESTS"


class FakeClamd(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, reply: bytes | None = None) -> None:
        self.received: list[bytes] = []
        self.reply = reply
        super().__init__(("127.0.0.1", 0), _Handler)


class _Handler(socketserver.BaseRequestHandler):
    server: FakeClamd

    def _read(self, n: int) -> bytes:
        data = b""
        while len(data) < n:
            part = self.request.recv(n - len(data))
            if not part:
                raise ConnectionError("closed")
            data += part
        return data

    def handle(self) -> None:
        command = b""
        while not command.endswith(b"\0"):
            command += self._read(1)
        assert command == b"zINSTREAM\0"
        body = b""
        while True:
            (length,) = struct.unpack("!I", self._read(4))
            if length == 0:
                break
            body += self._read(length)
        self.server.received.append(body)
        if self.server.reply is not None:
            self.request.sendall(self.server.reply)
        elif MARKER in body:
            self.request.sendall(b"stream: Fake.Test.Signature FOUND\0")
        else:
            self.request.sendall(b"stream: OK\0")


@pytest.fixture()
def clamd() -> Iterator[FakeClamd]:
    server = FakeClamd()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


def _address(server: FakeClamd) -> str:
    host, port = server.server_address[:2]
    return f"{host}:{port}"


def test_off_by_default_scans_nothing() -> None:
    scanner = scan.scanner_from_env({})
    assert isinstance(scanner, scan.NoScanner)
    assert scanner.scan(MARKER) == scan.ScanResult(verdict=None, scanner=None)
    assert scan.scanner_mode({}) == "off"
    assert "not scanned" in scan.SCANNING_TEXT["off"]


def test_clamd_passes_a_clean_file_and_flags_a_marked_one(clamd: FakeClamd) -> None:
    scanner = scan.scanner_from_env({"DATA_ROOM_SCANNER": "clamd", "CLAMD_ADDRESS": _address(clamd)})
    assert isinstance(scanner, scan.ClamdScanner)
    clean = b"%PDF-1.7 " + b"x" * (3 * scan.CHUNK_BYTES + 17)  # several chunks
    assert scanner.scan(clean) == scan.ScanResult(verdict="clean", scanner="clamd")
    assert clamd.received[-1] == clean
    assert scanner.scan(b"prefix " + MARKER).verdict == "infected"
    assert scan.scanner_mode({"DATA_ROOM_SCANNER": "clamd"}) == "clamd"


@pytest.mark.parametrize(
    "reply", [b"INSTREAM size limit exceeded. ERROR\0", b"stream: OK ERROR\0", b"\0", b"garbage\0"]
)
def test_any_other_answer_fails_closed(reply: bytes) -> None:
    server = FakeClamd(reply=reply)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        scanner = scan.ClamdScanner(_address(server))
        with pytest.raises(scan.ScannerUnavailable):
            scanner.scan(b"data")
    finally:
        server.shutdown()
        server.server_close()


def test_a_scanner_that_does_not_answer_fails_closed() -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    scanner = scan.ClamdScanner(f"127.0.0.1:{port}", timeout=1)
    with pytest.raises(scan.ScannerUnavailable):
        scanner.scan(b"data")


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({"DATA_ROOM_SCANNER": "sophos"}, "one of off, clamd"),
        ({"DATA_ROOM_SCANNER": "clamd"}, "CLAMD_ADDRESS"),
        ({"DATA_ROOM_SCANNER": "clamd", "CLAMD_ADDRESS": "nohost"}, "host:port"),
        ({"DATA_ROOM_SCANNER": "clamd", "CLAMD_ADDRESS": "h:1", "DATA_ROOM_SCAN_TIMEOUT_S": "x"}, "seconds"),
    ],
)
def test_malformed_settings_are_refused(env: dict[str, str], message: str) -> None:
    with pytest.raises(scan.ScannerUnavailable, match=message):
        scan.scanner_from_env(env)


@pytest.mark.parametrize(
    ("address", "family", "target"),
    [
        ("127.0.0.1:3310", socket.AF_INET, ("127.0.0.1", 3310)),
        ("tcp://clamav:3310", socket.AF_INET, ("clamav", 3310)),
        ("[::1]:3310", socket.AF_INET, ("::1", 3310)),
        ("unix:///run/clamav/clamd.ctl", socket.AF_UNIX, "/run/clamav/clamd.ctl"),
        ("/run/clamav/clamd.ctl", socket.AF_UNIX, "/run/clamav/clamd.ctl"),
    ],
)
def test_addresses(address: str, family: int, target: object) -> None:
    scanner = scan.ClamdScanner(address)
    assert (scanner.family, scanner.address) == (family, target)
