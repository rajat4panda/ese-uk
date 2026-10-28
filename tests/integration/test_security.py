"""Cross-cutting security behaviour (LLD 15, 16)."""

from __future__ import annotations

from app.config import get_settings
from tests.fixtures import API, PASSWORD


def test_security_headers(client):
    r = client.get(f"{API}/health")
    h = r.headers
    assert h["x-content-type-options"] == "nosniff"
    assert h["x-frame-options"] == "DENY"
    assert h["referrer-policy"] == "same-origin"
    assert "default-src 'self'" in h["content-security-policy"]
    assert "max-age" in h["strict-transport-security"]
    assert h["cache-control"] == "no-store"
    assert h["x-request-id"]


def test_error_envelope_carries_request_id(client):
    r = client.get(f"{API}/me", headers={"X-Request-ID": "req-123"})
    body = r.json()
    assert set(body["error"]) == {"code", "message", "details", "request_id"}
    assert body["error"]["request_id"] == "req-123"


def test_unknown_route_uses_error_model(client):
    r = client.get(f"{API}/nothing-here")
    assert r.status_code == 404 and r.json()["error"]["code"] == "ERR_NOT_FOUND"


def test_health_reports_dependencies(client):
    body = client.get(f"{API}/health").json()
    assert body == {"status": "ok", "database": True, "cache": True, "environment": "test"}


def test_auth_rate_limit(client, monkeypatch):
    """LLD 16: 10 requests per minute per IP on auth endpoints. The window is fixed, so the test
    asserts on the responses it collected rather than making an extra call that could land in the
    next window."""
    monkeypatch.setattr(get_settings(), "RATE_LIMIT_ENABLED", True)
    responses = [client.post(f"{API}/auth/login", json={"email": f"x{i}@example.com", "password": PASSWORD})
                 for i in range(11)]  # fmt: skip
    limited = [r for r in responses if r.status_code == 429]
    assert [r.status_code for r in responses[:10]] == [401] * 10
    assert len(limited) == 1
    assert limited[0].json()["error"]["code"] == "ERR_RATE_LIMITED"
    assert "retry-after" in limited[0].headers


def test_general_rate_limit_per_token(client, applicant, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "RATE_LIMIT_ENABLED", True)
    monkeypatch.setattr(s, "RATE_LIMIT_GENERAL_PER_MINUTE", 3)
    codes = [client.get(f"{API}/me", headers=applicant.headers).status_code for _ in range(4)]
    assert codes == [200, 200, 200, 429]


def test_unhandled_error_hides_detail(client, applicant, monkeypatch):
    from app.services import applications

    def boom(*a, **k):
        raise RuntimeError("secret internals")

    monkeypatch.setattr(applications, "create_or_get", boom)
    from fastapi.testclient import TestClient

    from app.main import create_app

    with TestClient(create_app(), raise_server_exceptions=False) as c:
        r = c.post(f"{API}/applications", headers=applicant.headers)
    assert r.status_code == 500
    assert r.json()["error"]["code"] == "ERR_INTERNAL"
    assert "secret" not in r.text


def test_reference_endpoints(client, applicant, reference):
    progs = client.get(f"{API}/reference/programmes", headers=applicant.headers).json()
    assert [p["id"] for p in progs] == [reference["programme_id"]]
    intakes = client.get(f"{API}/reference/intakes", headers=applicant.headers).json()
    assert [i["id"] for i in intakes] == [reference["intake_id"]]  # closed intake hidden
    agents = client.get(f"{API}/reference/agents", headers=applicant.headers).json()
    assert [a["id"] for a in agents] == [reference["agent_id"]]  # suspended agent hidden (FR-106)
    countries = client.get(f"{API}/reference/countries").json()
    assert {"code": "GB", "name": "United Kingdom"} in countries and len(countries) == 249
    assert client.get(f"{API}/reference/programmes").status_code == 401
