"""Malware scanning for data-room uploads: a hook, off unless configured (docs/19 §3; "Data rooms, as
built").

**What runs today.** No antivirus engine runs on the one-server beta, so by default nothing is
scanned: `DATA_ROOM_SCANNER` unset or `off` gives `NoScanner`, every stored file is marked
`not_scanned`, and the data room's page says so in plain words (`SCANNING_TEXT`). What protects a
member meanwhile is the type allowlist (`data_room_files`: no programs, scripts, macros, embedded
objects or archives) and the download headers (always an attachment, never rendered by the site).

**The clamd adapter.** `DATA_ROOM_SCANNER=clamd` with `CLAMD_ADDRESS` (`host:port`, `tcp://host:port`,
or a Unix socket path, `unix:///run/clamav/clamd.ctl` or `/run/clamav/clamd.ctl`) scans every upload
before anything is stored, over clamd's `INSTREAM` command: the bytes go in chunks, each a 4-byte
network-order length then the data, ended by a zero-length chunk; clamd answers `stream: OK` or
`stream: <signature> FOUND` (clamd(8), "INSTREAM"). A flagged file is refused and never stored. When a
scanner is configured but does not answer, or answers anything else, the upload is refused with
`503`: it fails closed, it never stores an unscanned file as if it had been scanned. clamd's own
`StreamMaxLength` must be at least the file cap (`DATA_ROOM_MAX_FILE_MB`), or every large file is
refused.

**What is not scanned.** Files stored while no scanner was configured stay `not_scanned`: nothing
rescans them yet. Downloads are not rescanned. A signature engine finds known malware, not a
targeted document. What the type checks do not read either is listed in docs/19 §12.5 (PDF actions
that open a link, submit a form or open another file; spreadsheet formulas and DDE in a CSV; Word
field codes).

The signature clamd names is not stored or logged; a refusal records the reason `malware` only.
"""

from __future__ import annotations

import os
import socket
import struct
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Literal, Protocol

ENV_SCANNER: Final = "DATA_ROOM_SCANNER"
ENV_CLAMD_ADDRESS: Final = "CLAMD_ADDRESS"
ENV_SCAN_TIMEOUT: Final = "DATA_ROOM_SCAN_TIMEOUT_S"
SCANNER_MODES: Final = ("off", "clamd")
DEFAULT_TIMEOUT_SECONDS: Final = 60.0
#: clamd reads a stream in chunks; 1 MiB keeps each send small without many round trips.
CHUNK_BYTES: Final = 1024 * 1024

#: What the data room's page says, by scanner (docs/19 §3: "say plainly what is and is not scanned").
SCANNING_TEXT: Final[Mapping[str, str]] = {
    "off": (
        "Files are not scanned for viruses: no virus scanner runs on this server yet. Uploads are "
        "checked for type (no programs, scripts, macros, embedded objects, links that load from "
        "outside the file, or archives), not for malware; links inside PDFs and formulas in "
        "spreadsheets are not checked. Open files only from people you trust."
    ),
    "clamd": (
        "Each file is scanned for known viruses (ClamAV) when it is uploaded; a flagged file is "
        "refused. Files uploaded before scanning started are marked “Not scanned”."
    ),
}

Verdict = Literal["clean", "infected"]


class ScannerUnavailable(RuntimeError):
    """A configured scanner did not answer, or answered something other than a verdict."""


@dataclass(frozen=True)
class ScanResult:
    #: `None` when no scanner ran.
    verdict: Verdict | None
    scanner: str | None


class Scanner(Protocol):
    name: str

    def scan(self, data: bytes) -> ScanResult: ...


class NoScanner:
    """Nothing is scanned; the file is stored `not_scanned`."""

    name = "off"

    def scan(self, data: bytes) -> ScanResult:
        return ScanResult(verdict=None, scanner=None)


def _parse_address(raw: str) -> tuple[int, str | tuple[str, int]]:
    text = raw.strip()
    if text.startswith("unix://"):
        return socket.AF_UNIX, text[len("unix://") :]
    if text.startswith("unix:"):
        return socket.AF_UNIX, text[len("unix:") :]
    if text.startswith("/"):
        return socket.AF_UNIX, text
    if text.startswith("tcp://"):
        text = text[len("tcp://") :]
    host, sep, port = text.rpartition(":")
    if not sep or not host or not port.isdigit():
        raise ScannerUnavailable(f"{ENV_CLAMD_ADDRESS} must be host:port or a socket path")
    return socket.AF_INET, (host.strip("[]"), int(port))


class ClamdScanner:
    """clamd's `INSTREAM` (module docstring)."""

    name = "clamd"

    def __init__(self, address: str, *, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> None:
        self.family, self.address = _parse_address(address)
        self.timeout = timeout

    def _connect(self) -> socket.socket:
        if self.family == socket.AF_UNIX:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(self.timeout)
            sock.connect(self.address)
            return sock
        if not isinstance(self.address, tuple):
            raise ScannerUnavailable("clamd's TCP address is malformed")
        return socket.create_connection(self.address, timeout=self.timeout)

    def scan(self, data: bytes) -> ScanResult:
        try:
            with self._connect() as sock:
                sock.sendall(b"zINSTREAM\0")
                view = memoryview(data)
                for start in range(0, len(data), CHUNK_BYTES):
                    chunk = view[start : start + CHUNK_BYTES]
                    sock.sendall(struct.pack("!I", len(chunk)))
                    sock.sendall(chunk)
                sock.sendall(struct.pack("!I", 0))
                reply = b""
                while not reply.endswith(b"\0") and len(reply) < 4096:
                    part = sock.recv(4096)
                    if not part:
                        break
                    reply += part
        except OSError as exc:
            raise ScannerUnavailable(f"clamd did not answer ({type(exc).__name__})") from None
        text = reply.rstrip(b"\0").strip()
        if text.endswith(b" OK") and b"ERROR" not in text:
            return ScanResult(verdict="clean", scanner=self.name)
        if text.endswith(b" FOUND"):
            return ScanResult(verdict="infected", scanner=self.name)
        raise ScannerUnavailable("clamd answered without a verdict")


def scanner_from_env(env: Mapping[str, str] | None = None) -> Scanner:
    """`NoScanner` unless `DATA_ROOM_SCANNER=clamd`; a malformed setting is refused, never ignored."""
    source = os.environ if env is None else env
    mode = (source.get(ENV_SCANNER) or "off").strip().lower() or "off"
    if mode not in SCANNER_MODES:
        raise ScannerUnavailable(f"{ENV_SCANNER} must be one of {', '.join(SCANNER_MODES)}")
    if mode == "off":
        return NoScanner()
    address = (source.get(ENV_CLAMD_ADDRESS) or "").strip()
    if not address:
        raise ScannerUnavailable(f"{ENV_SCANNER}=clamd needs {ENV_CLAMD_ADDRESS}")
    raw_timeout = (source.get(ENV_SCAN_TIMEOUT) or "").strip()
    try:
        timeout = float(raw_timeout) if raw_timeout else DEFAULT_TIMEOUT_SECONDS
    except ValueError:
        raise ScannerUnavailable(f"{ENV_SCAN_TIMEOUT} must be a number of seconds") from None
    return ClamdScanner(address, timeout=max(1.0, timeout))


def scanner_mode(env: Mapping[str, str] | None = None) -> str:
    """`off` or `clamd`, for the page's sentence. A malformed setting reads `off` here; uploads then
    fail closed in `scanner_from_env`, so nothing is stored either way."""
    source = os.environ if env is None else env
    mode = (source.get(ENV_SCANNER) or "off").strip().lower() or "off"
    return mode if mode in SCANNER_MODES else "off"
