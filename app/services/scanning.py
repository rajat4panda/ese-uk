"""Malware scanning adapter (FR-22, LLD 11.1 step 5).

ClamAV (clamd INSTREAM over TCP) in every deployed environment. The simulated scanner is for
local development and tests only: it flags the industry-standard EICAR test string, which lets
the quarantine path be exercised without real malware. Configuration refuses it in production.
"""

from __future__ import annotations

import socket
import struct
from abc import ABC, abstractmethod
from functools import lru_cache

from app.config import get_settings

EICAR_SIGNATURE = b"EICAR-STANDARD-ANTIVIRUS-TEST-FILE"


class ScannerUnavailable(RuntimeError):
    pass


class ScanResult:
    def __init__(self, clean: bool, signature: str | None = None) -> None:
        self.clean = clean
        self.signature = signature


class Scanner(ABC):
    @abstractmethod
    def scan(self, data: bytes) -> ScanResult: ...


class SimulatedScanner(Scanner):
    def scan(self, data: bytes) -> ScanResult:
        if EICAR_SIGNATURE in data:
            return ScanResult(False, "Eicar-Test-Signature")
        return ScanResult(True)


class ClamAVScanner(Scanner):
    CHUNK = 64 * 1024

    def __init__(self, endpoint: str, timeout: float) -> None:
        host, _, port = endpoint.partition(":")
        self.host, self.port, self.timeout = host, int(port or 3310), timeout

    def scan(self, data: bytes) -> ScanResult:
        try:
            with socket.create_connection((self.host, self.port), timeout=self.timeout) as sock:
                sock.sendall(b"zINSTREAM\0")
                for i in range(0, len(data), self.CHUNK):
                    chunk = data[i : i + self.CHUNK]
                    sock.sendall(struct.pack("!L", len(chunk)) + chunk)
                sock.sendall(struct.pack("!L", 0))
                reply = b""
                while not reply.endswith(b"\0"):
                    part = sock.recv(4096)
                    if not part:
                        break
                    reply += part
        except OSError as exc:
            raise ScannerUnavailable(str(exc)) from exc
        text = reply.rstrip(b"\0").decode("utf-8", "replace")
        if text.endswith("OK"):
            return ScanResult(True)
        if text.endswith("FOUND"):
            return ScanResult(False, text.split(":", 1)[-1].replace("FOUND", "").strip())
        raise ScannerUnavailable(f"Unexpected clamd reply: {text}")


@lru_cache
def get_scanner() -> Scanner:
    s = get_settings()
    if s.ANTIVIRUS_BACKEND == "clamav":
        if not s.ANTIVIRUS_ENDPOINT:
            raise ScannerUnavailable("ANTIVIRUS_ENDPOINT is not configured")
        return ClamAVScanner(s.ANTIVIRUS_ENDPOINT, s.ANTIVIRUS_TIMEOUT_SECONDS)
    return SimulatedScanner()
