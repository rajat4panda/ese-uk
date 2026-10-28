"""Storage, scanning and notification adapters."""

from __future__ import annotations

import socket
import struct
import threading

import pytest

from app.services import documents, notifications
from app.services.scanning import ClamAVScanner, ScannerUnavailable, SimulatedScanner
from app.services.storage import LocalStorage, S3Storage
from tests.fixtures import EICAR, PDF_BYTES, PNG_BYTES


# --- magic bytes -------------------------------------------------------------------------------
@pytest.mark.parametrize("head,expected", [
    (PDF_BYTES, "application/pdf"), (PNG_BYTES, "image/png"), (b"\xff\xd8\xff\xe0JFIF", "image/jpeg"),
    (b"MZ\x90\x00", None), (b"GIF89a", None), (b"<svg xmlns=", None), (b"PK\x03\x04", None), (b"", None),
])  # fmt: skip
def test_sniff(head, expected):
    result = documents.sniff(head)
    assert (result[0] if result else None) == expected


@pytest.mark.parametrize("name,expected", [
    ("passport.pdf", "passport.pdf"), ("../../x.pdf", "x.pdf"), ("C:\\Users\\me\\scan.png", "scan.png"),
    ("", "upload"), (None, "upload"), ("a\x00b.pdf", "ab.pdf"), ("x" * 300 + ".pdf", "x" * 255),
])  # fmt: skip
def test_safe_filename(name, expected):
    assert documents.safe_filename(name) == expected


# --- storage -----------------------------------------------------------------------------------
def test_local_storage_round_trip_and_containment(tmp_path):
    s = LocalStorage(str(tmp_path))
    s.put("ESE-2026-000127/passport/a.pdf", PDF_BYTES, "application/pdf")
    assert s.exists("ESE-2026-000127/passport/a.pdf")
    assert b"".join(s.open("ESE-2026-000127/passport/a.pdf")) == PDF_BYTES
    s.delete("ESE-2026-000127/passport/a.pdf")
    assert not s.exists("ESE-2026-000127/passport/a.pdf")
    with pytest.raises(ValueError):
        s.put("../escape.pdf", PDF_BYTES, "application/pdf")


def test_s3_storage_uses_sse(monkeypatch):
    moto = pytest.importorskip("moto")
    import boto3

    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with moto.mock_aws():
        boto3.client("s3", region_name="eu-west-2").create_bucket(
            Bucket="ese-docs-test", CreateBucketConfiguration={"LocationConstraint": "eu-west-2"}
        )
        s = S3Storage("ese-docs-test", "eu-west-2")
        s.put("k/doc.pdf", PDF_BYTES, "application/pdf")
        head = s.client.head_object(Bucket="ese-docs-test", Key="k/doc.pdf")
        assert head["ServerSideEncryption"] == "AES256"
        assert b"".join(s.open("k/doc.pdf")) == PDF_BYTES
        assert s.exists("k/doc.pdf")
        s.delete("k/doc.pdf")
        assert not s.exists("k/doc.pdf")


# --- scanning ----------------------------------------------------------------------------------
def test_simulated_scanner():
    assert SimulatedScanner().scan(PDF_BYTES).clean
    r = SimulatedScanner().scan(b"%PDF " + EICAR)
    assert not r.clean and r.signature


def _fake_clamd(reply: bytes):
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    received = {}

    def serve():
        conn, _ = srv.accept()
        buf = b""
        while True:
            chunk = conn.recv(65536)
            if not chunk:
                break
            buf += chunk
            if buf.endswith(struct.pack("!L", 0)):
                break
        received["data"] = buf
        conn.sendall(reply)
        conn.close()
        srv.close()

    threading.Thread(target=serve, daemon=True).start()
    return srv.getsockname()[1], received


def test_clamav_protocol_clean():
    port, received = _fake_clamd(b"stream: OK\0")
    assert ClamAVScanner(f"127.0.0.1:{port}", 5).scan(PDF_BYTES).clean
    assert received["data"].startswith(b"zINSTREAM\0")


def test_clamav_protocol_found():
    port, _ = _fake_clamd(b"stream: Win.Test.EICAR_HDB-1 FOUND\0")
    result = ClamAVScanner(f"127.0.0.1:{port}", 5).scan(EICAR)
    assert not result.clean and result.signature == "Win.Test.EICAR_HDB-1"


def test_clamav_unreachable():
    with pytest.raises(ScannerUnavailable):
        ClamAVScanner("127.0.0.1:1", 1).scan(PDF_BYTES)


# --- notifications -----------------------------------------------------------------------------
def test_templates_render_without_missing_placeholders():
    payload = {"name": "N", "link": "L", "hours": 1, "minutes": 1, "role": "r", "ref": "ESE-2026-000127",
               "programme": "P", "intake": "I", "reason": "R", "offer_type": "conditional", "conditions": "C",
               "cas_number": "E4G0ABC123", "course": "BSc", "start_date": "2027-01-18", "end_date": "2030-01-18",
               "event": "deferral", "deadline": "2026-10-09", "student_id": 1, "percentage": 62.5,
               "threshold": 80, "expires": "2026-12-01", "document_id": 4, "category": "passport",
               "offer_id": 9, "days": 45, "agent": "A", "country": "GH", "review_date": "2026-10-20",
               "when": "due"}  # fmt: skip
    for key in notifications.TEMPLATES:
        subject, body = notifications.render(key, payload)
        assert "$" not in subject + body, key
