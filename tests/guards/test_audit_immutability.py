"""T-15: audit_entries is append-only for the application role (LLD 3.10, NFR-12)."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError

from app.database import get_engine
from tests.fixtures import API


@pytest.mark.priority("T-15")
@pytest.mark.parametrize("statement", [
    "UPDATE audit_entries SET detail = 'tampered'",
    "DELETE FROM audit_entries",
    "TRUNCATE audit_entries",
])  # fmt: skip
def test_t15_app_role_cannot_alter_audit(client, applicant, statement):
    """T-15: attempt to UPDATE an audit_entries row as app_role -> permission denied at the database."""
    with get_engine().connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM audit_entries")).scalar_one() >= 1
        with pytest.raises(ProgrammingError) as exc:
            conn.execute(text(statement))
        assert "permission denied" in str(exc.value)


def test_app_role_can_insert_and_read_audit(client, applicant):
    with get_engine().connect() as conn:
        user = conn.execute(text("SELECT current_user")).scalar_one()
        assert user == "ese_app"
        roles = conn.execute(text("SELECT pg_has_role(current_user, 'app_role', 'member')")).scalar_one()
        assert roles is True


def test_app_role_has_no_delete_anywhere(client, applicant, draft):
    with get_engine().connect() as conn:
        for table in ("users", "applications", "documents", "password_reset_tokens"):
            with pytest.raises(ProgrammingError):
                conn.execute(text(f"DELETE FROM {table}"))  # noqa: S608
            conn.rollback()


def test_audit_timestamp_is_server_side(client, applicant):
    r = client.get(f"{API}/me", headers=applicant.headers)
    assert r.status_code == 200
    with get_engine().connect() as conn:
        drift = conn.execute(text("SELECT max(abs(extract(epoch FROM at - now()))) FROM audit_entries")).scalar_one()
    assert float(drift) < 60
