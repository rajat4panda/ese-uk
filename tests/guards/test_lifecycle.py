"""State machine executor (LLD 7.1, 7.2): guards, roles, atomicity, status write protection."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from app.database import get_sessionmaker
from app.errors import Conflict, Forbidden, GuardFailed, NotFound
from app.models.application import Application, StatusWriteOutsideExecutor
from app.models.enums import ApplicationStatus
from app.models.user import User
from app.services import lifecycle
from tests.fixtures import API, complete_data, register


def _load(app_id: int, user_id: int):
    db = get_sessionmaker()()
    return db, db.get(Application, app_id), db.get(User, user_id)


def test_status_cannot_be_written_outside_executor(client, applicant, draft):
    db, app, _ = _load(draft["id"], applicant.id)
    with pytest.raises(StatusWriteOutsideExecutor):
        app.status = ApplicationStatus.SUBMITTED
    db.rollback()
    db.close()


def test_new_application_default_draft_allowed(client, applicant):
    app = Application(ref="ESE-2026-999999", applicant_id=applicant.id, status=ApplicationStatus.DRAFT)
    assert app.status == ApplicationStatus.DRAFT
    with pytest.raises(StatusWriteOutsideExecutor):
        Application(ref="ESE-2026-999998", applicant_id=applicant.id, status=ApplicationStatus.CAS_ASSIGNED)


def test_unknown_transition_is_invalid_state(client, applicant, draft):
    db, app, user = _load(draft["id"], applicant.id)
    with pytest.raises(Conflict) as exc:
        lifecycle.transition(db, app, "assign_cas", user)
    assert exc.value.code == "ERR_INVALID_STATE"
    db.close()


def test_role_checked_before_guards(client, applicant, draft, staff):
    s = staff("senior")
    db, app, _ = _load(draft["id"], applicant.id)
    senior = db.get(User, s.id)
    with pytest.raises(Forbidden):
        lifecycle.transition(db, app, "submit", senior)
    db.close()


def test_guard_order_ownership_first(client, applicant, draft):
    """An incomplete application owned by someone else is 'not found', not 'incomplete'."""
    other = register(client, "other@example.com")
    db, app, _ = _load(draft["id"], applicant.id)
    with pytest.raises(NotFound):
        lifecycle.transition(db, app, "submit", db.get(User, other.id))
    db.close()


def test_guard_failure_writes_nothing(client, applicant, draft, owner_engine):
    db, app, user = _load(draft["id"], applicant.id)
    with pytest.raises(GuardFailed):
        lifecycle.transition(db, app, "submit", user)
    db.commit()
    db.close()
    with owner_engine.connect() as conn:
        status = conn.execute(text("SELECT status FROM applications WHERE id=:i"), {"i": draft["id"]}).scalar_one()
        n = conn.execute(text("SELECT count(*) FROM audit_entries WHERE action='application.submit'")).scalar_one()
    assert status == "draft" and n == 0


def test_status_and_audit_share_a_transaction(client, applicant, draft, reference, owner_engine):
    """If the surrounding transaction rolls back, neither the new status nor its audit entry persist."""
    client.put(f"{API}/applications/{draft['id']}", json={"data": complete_data(reference)}, headers=applicant.headers)
    db, app, user = _load(draft["id"], applicant.id)
    lifecycle.transition(db, app, "submit", user)
    assert app.status == ApplicationStatus.SUBMITTED
    db.rollback()
    db.close()
    with owner_engine.connect() as conn:
        status = conn.execute(text("SELECT status FROM applications WHERE id=:i"), {"i": draft["id"]}).scalar_one()
        n = conn.execute(text("SELECT count(*) FROM audit_entries WHERE action='application.submit'")).scalar_one()
    assert status == "draft" and n == 0


def test_every_declared_transition_has_roles_and_audit_action():
    from app.services.audit import ACTIONS

    for (_, action), t in lifecycle.TRANSITIONS.items():
        assert t.roles, action
        assert t.audit_action in ACTIONS, action


def test_locked_load_sees_concurrent_submission(client, applicant, draft, reference):
    """A save that loaded the draft before a concurrent submit must not act on the stale status."""
    from app.errors import Conflict as _Conflict
    from app.services import applications as svc

    client.put(f"{API}/applications/{draft['id']}", json={"data": complete_data(reference)}, headers=applicant.headers)
    db, stale, user = _load(draft["id"], applicant.id)
    assert stale.status == ApplicationStatus.DRAFT
    assert client.post(f"{API}/applications/{draft['id']}/submit", headers=applicant.headers).status_code == 200
    fresh = svc.get_for_actor(db, draft["id"], user, lock=True)
    assert fresh.status == ApplicationStatus.SUBMITTED and "submitted_at" in fresh.data
    with pytest.raises(_Conflict):
        svc.save_draft(db, fresh, user, {"nationality": "GB"}, if_match=None, ip=None)
    db.rollback()
    db.close()
