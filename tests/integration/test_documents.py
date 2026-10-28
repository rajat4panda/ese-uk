"""Document upload pipeline and retrieval (LLD 11; FR-16 to FR-22)."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from app.services.storage import get_storage
from tests.fixtures import API, EICAR, PDF_BYTES, PNG_BYTES, audit_actions, complete_data, register


def upload(client, actor, app_id, content, filename="passport.pdf", category="passport", **extra):
    return client.post(
        f"{API}/applications/{app_id}/documents",
        headers=actor.headers,
        data={"category": category, **extra},
        files={"file": (filename, content, "application/pdf")},
    )


def test_pdf_upload_stored_and_audited(client, applicant, draft, owner_engine):
    r = upload(client, applicant, draft["id"], PDF_BYTES, expiry_date="2031-01-01")
    assert r.status_code == 201, r.text
    doc = r.json()
    assert doc["content_type"] == "application/pdf" and doc["scan_status"] == "clean"
    assert doc["expiry_date"] == "2031-01-01" and doc["filename"] == "passport.pdf"
    with owner_engine.connect() as conn:
        key, uploaded_by = conn.execute(text("SELECT storage_key, uploaded_by FROM documents")).one()
    assert key.startswith(f"{draft['ref']}/passport/") and key.endswith(".pdf")
    assert uploaded_by == applicant.id
    assert get_storage().exists(key)
    assert "storage_key" not in doc  # opaque key never exposed (LLD 3.3)
    assert audit_actions(owner_engine, draft["id"])[-1] == "document.upload"


def test_content_type_detected_from_bytes_not_name(client, applicant, draft):
    r = upload(client, applicant, draft["id"], PNG_BYTES, filename="scan.pdf")
    assert r.status_code == 201
    assert r.json()["content_type"] == "image/png"


@pytest.mark.priority("T-03")
def test_t03_exe_renamed_to_pdf_rejected(client, applicant, draft, owner_engine):
    """T-03: upload .exe renamed to .pdf -> 400 ERR_FILE_TYPE (magic bytes rejected)."""
    exe = b"MZ\x90\x00\x03\x00\x00\x00\x04\x00\x00\x00\xff\xff" + b"\x00" * 100
    r = upload(client, applicant, draft["id"], exe, filename="passport.pdf")
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "ERR_FILE_TYPE"
    with owner_engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM documents")).scalar_one() == 0


def test_oversize_rejected(client, applicant, draft):
    big = b"%PDF-1.4\n" + b"0" * (5_242_880 + 1)
    r = upload(client, applicant, draft["id"], big)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "ERR_FILE_SIZE"


def test_exactly_at_limit_accepted(client, applicant, draft):
    data = b"%PDF-1.4\n" + b"0" * (5_242_880 - 9)
    assert len(data) == 5_242_880
    assert upload(client, applicant, draft["id"], data).status_code == 201


def test_malware_quarantined_not_stored(client, applicant, draft, owner_engine):
    r = upload(client, applicant, draft["id"], b"%PDF-1.4\n" + EICAR)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "ERR_SCAN_FAILED"
    with owner_engine.connect() as conn:
        status, key = conn.execute(text("SELECT scan_status, storage_key FROM documents")).one()
    assert status == "quarantined"
    assert not get_storage().exists(key)
    assert audit_actions(owner_engine, draft["id"])[-2:] == ["document.upload", "document.quarantine"]


def test_scanner_unavailable_fails_closed(client, applicant, draft, owner_engine, monkeypatch):
    from app.services import documents
    from app.services.scanning import ScannerUnavailable

    class Down:
        def scan(self, data):
            raise ScannerUnavailable("connection refused")

    monkeypatch.setattr(documents, "get_scanner", lambda: Down())
    r = upload(client, applicant, draft["id"], PDF_BYTES)
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "ERR_SCAN_UNAVAILABLE"
    with owner_engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM documents")).scalar_one() == 0


def test_applicant_cannot_upload_staff_categories(client, applicant, draft):
    r = upload(client, applicant, draft["id"], PDF_BYTES, category="report_evidence")
    assert r.status_code == 403


def test_unknown_category_rejected(client, applicant, draft):
    r = upload(client, applicant, draft["id"], PDF_BYTES, category="selfie")
    assert r.status_code == 422


def test_upload_locked_after_submission(client, applicant, draft, reference, staff):
    client.put(f"{API}/applications/{draft['id']}", json={"data": complete_data(reference)}, headers=applicant.headers)
    client.post(f"{API}/applications/{draft['id']}/submit", headers=applicant.headers)
    r = upload(client, applicant, draft["id"], PDF_BYTES)
    assert r.status_code == 409 and r.json()["error"]["code"] == "ERR_NOT_EDITABLE"
    # Staff may still add documents to a submitted application
    assert upload(client, staff("compliance"), draft["id"], PDF_BYTES, category="other").status_code == 201


def test_other_applicant_cannot_upload_or_download(client, applicant, draft):
    doc = upload(client, applicant, draft["id"], PDF_BYTES).json()
    other = register(client, "other@example.com")
    assert upload(client, other, draft["id"], PDF_BYTES).status_code == 404
    assert client.get(f"{API}/documents/{doc['id']}/download", headers=other.headers).status_code == 404


def test_download_by_owner_and_staff_is_audited(client, applicant, draft, staff, owner_engine):
    doc = upload(client, applicant, draft["id"], PDF_BYTES, filename="Pässport scan.pdf").json()
    r = client.get(f"{API}/documents/{doc['id']}/download", headers=applicant.headers)
    assert r.status_code == 200
    assert r.content == PDF_BYTES
    assert r.headers["content-type"] == "application/pdf"
    assert "attachment" in r.headers["content-disposition"]
    assert "filename*=UTF-8''P%C3%A4ssport%20scan.pdf" in r.headers["content-disposition"]
    s = staff("admissions")
    assert client.get(f"{API}/documents/{doc['id']}/download", headers=s.headers).status_code == 200
    assert audit_actions(owner_engine, draft["id"]).count("document.view") == 2


def test_finance_cannot_download(client, applicant, draft, staff):
    doc = upload(client, applicant, draft["id"], PDF_BYTES).json()
    assert client.get(f"{API}/documents/{doc['id']}/download", headers=staff("finance").headers).status_code == 403


def test_quarantined_document_never_downloadable(client, applicant, draft, staff, owner_engine):
    upload(client, applicant, draft["id"], b"%PDF-1.4\n" + EICAR)
    with owner_engine.connect() as conn:
        doc_id = conn.execute(text("SELECT id FROM documents")).scalar_one()
    r = client.get(f"{API}/documents/{doc_id}/download", headers=staff("senior").headers)
    assert r.status_code == 409


def test_outstanding_documents(client, applicant, draft):
    app = client.get(f"{API}/applications/{draft['id']}", headers=applicant.headers).json()
    assert app["outstanding_documents"] == ["passport", "qualification", "financial"]
    client.put(f"{API}/applications/{draft['id']}", json={"data": {"english_route": "slt_test"}},
               headers=applicant.headers)
    upload(client, applicant, draft["id"], PDF_BYTES, category="passport")
    upload(client, applicant, draft["id"], PDF_BYTES, category="financial")
    app = client.get(f"{API}/applications/{draft['id']}", headers=applicant.headers).json()
    assert app["outstanding_documents"] == ["qualification", "english"]
    assert [d["category"] for d in app["documents"]] == ["passport", "financial"]


def test_filename_path_components_stripped(client, applicant, draft):
    r = upload(client, applicant, draft["id"], PDF_BYTES, filename="../../etc/passwd.pdf")
    assert r.json()["filename"] == "passwd.pdf"


def test_body_limit_refused_before_parsing(client, applicant, draft):
    r = client.post(
        f"{API}/applications/{draft['id']}/documents",
        headers={**applicant.headers, "Content-Length": str(20_000_000), "Content-Type": "multipart/form-data; boundary=x"},
        content=b"--x--",
    )
    assert r.status_code == 400 and r.json()["error"]["code"] == "ERR_FILE_SIZE"


def test_chunked_upload_without_content_length_is_cut_off(client, applicant, draft):
    boundary = "xyzboundary"

    def body():
        yield (f"--{boundary}\r\nContent-Disposition: form-data; name=\"category\"\r\n\r\npassport\r\n"
               f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"big.pdf\"\r\n"
               "Content-Type: application/pdf\r\n\r\n").encode()
        yield b"%PDF-1.4\n"
        for _ in range(7):
            yield b"0" * 1_000_000
        yield f"\r\n--{boundary}--\r\n".encode()

    r = client.post(f"{API}/applications/{draft['id']}/documents", content=body(),
                    headers={**applicant.headers, "Content-Type": f"multipart/form-data; boundary={boundary}"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "ERR_FILE_SIZE"
