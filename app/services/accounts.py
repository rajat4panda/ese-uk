"""Account flows: registration, sign-in with progressive lockout, password reset, email
verification, staff administration (LLD 5.1, 5.6, 16; FR-01, FR-02, FR-121, FR-125; NFR-16)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.errors import BadRequest, Forbidden, GuardFailed, Locked, NotFound, Unauthenticated
from app.models.enums import STAFF_ROLES, UserRole, UserStatus
from app.models.user import EmailVerificationToken, PasswordResetToken, User
from app.schemas.common import normalise_email
from app.services import audit, cache, notifications
from app.services.auth import (
    TokenClaims,
    check_password_policy,
    hash_link_token,
    hash_password,
    issue_token,
    new_link_token,
    unusable_password_hash,
    verify_password,
)

LOCK_SHORT_AFTER = 5  # failed attempts → 15 minutes (LLD 16)
LOCK_LONG_AFTER = 10  # failed attempts → 1 hour
LOCK_SHORT = timedelta(minutes=15)
LOCK_LONG = timedelta(hours=1)


def _now() -> datetime:
    return datetime.now(UTC)


def _email_or_400(email: str) -> str:
    try:
        normalised = normalise_email(email)
    except Exception as exc:  # PydanticCustomError
        raise BadRequest("ERR_INVALID_EMAIL", "Enter a valid email address.",
                         details=[{"field": "email", "code": "ERR_INVALID_EMAIL"}]) from exc  # fmt: skip
    assert normalised is not None
    return normalised


def _name_or_400(full_name: str) -> str:
    name = " ".join((full_name or "").split())
    if not 1 <= len(name) <= 200:
        raise BadRequest("ERR_REQUIRED", "Enter your full name (up to 200 characters).",
                         details=[{"field": "full_name", "code": "ERR_REQUIRED"}])  # fmt: skip
    return name


def find_by_email(db: Session, email: str) -> User | None:
    return db.scalars(select(User).where(func.lower(User.email) == email.lower())).first()


@dataclass
class Session_:
    token: str
    claims: TokenClaims
    user: User


def start_session(user: User) -> Session_:
    s = get_settings()
    token, claims = issue_token(user.id, str(user.role), user.full_name)
    cache.track_token(user.id, claims.jti, claims.seconds_remaining + 60)
    cache.touch_session(claims.jti, s.SESSION_INACTIVITY_MINUTES * 60)
    return Session_(token, claims, user)


# --- Registration (FR-01) ------------------------------------------------------------------
def register(db: Session, email: str, password: str, full_name: str, ip: str | None) -> User:
    email = _email_or_400(email)
    name = _name_or_400(full_name)
    check_password_policy(password, email)
    if find_by_email(db, email):
        raise BadRequest("ERR_EMAIL_EXISTS", "An account already exists for this email address. Sign in instead.",
                         details=[{"field": "email", "code": "ERR_EMAIL_EXISTS"}])  # fmt: skip
    user = User(email=email, password_hash=hash_password(password), full_name=name, role=UserRole.APPLICANT)
    db.add(user)
    db.flush()
    audit.write(db, user, "auth.register", entity_type="user", entity_id=user.id,
                detail="Applicant account created", ip=ip)  # fmt: skip
    return user


def send_verification(db: Session, user: User) -> None:
    s = get_settings()
    if not s.email_verification_required:
        return  # development / UAT: no confirmation step, so no link to send
    plaintext, digest = new_link_token()
    db.add(EmailVerificationToken(
        user_id=user.id, token_hash=digest, expires_at=_now() + timedelta(hours=s.EMAIL_VERIFICATION_HOURS)
    ))  # fmt: skip
    db.flush()
    notifications.send("auth.verify_email", user.email, {
        "name": user.full_name, "link": notifications.link(f"/verify-email?token={plaintext}"),
        "hours": s.EMAIL_VERIFICATION_HOURS,
    })  # fmt: skip


def confirm_verification(db: Session, token: str, ip: str | None) -> User:
    row = db.scalars(select(EmailVerificationToken).where(
        EmailVerificationToken.token_hash == hash_link_token(token)).with_for_update()).first()  # fmt: skip
    if row is None or row.used_at is not None:
        raise BadRequest("ERR_TOKEN_INVALID", "This link is not valid. Request a new one from your account.")
    if row.expires_at < _now():
        raise BadRequest("ERR_TOKEN_EXPIRED", "This link has expired. Request a new one from your account.")
    user = db.get(User, row.user_id)
    assert user is not None
    row.used_at = _now()
    if user.email_verified_at is None:
        user.email_verified_at = _now()
        audit.write(db, user, "auth.email_verified", entity_type="user", entity_id=user.id,
                    detail="Email address verified", ip=ip)  # fmt: skip
    return user


# --- Sign-in with progressive lockout (FR-125, NFR-16) ---------------------------------------
def login(db: Session, email: str, password: str, ip: str | None) -> Session_ | Exception:
    """Returns a session, or an exception to raise *after* the caller commits the audit trail.

    Failed attempts must be recorded even though the request fails, so this function never raises
    for credential failures; the router commits and then raises the returned error.
    """
    email = (email or "").strip().lower()  # lookup only; format rules apply at registration
    user = db.scalars(select(User).where(func.lower(User.email) == email).with_for_update()).first()
    bad = Unauthenticated("ERR_BAD_CREDENTIALS", "The email address or password is incorrect.")

    if user is None:
        verify_password(password, None)  # equalise timing
        audit.write(db, None, "auth.login_failed", actor_label="unknown account", detail="Unknown account", ip=ip)
        # Mirror the lockout response for unknown addresses, so a 423 does not reveal that an account exists.
        if cache.count_unknown_login(email, int(LOCK_SHORT.total_seconds())) >= LOCK_SHORT_AFTER:
            return Locked(message="This account is temporarily locked after repeated failed sign-in attempts. "
                                  "Try again later or reset your password.")  # fmt: skip
        return bad

    now = _now()
    locked = user.locked_until is not None and user.locked_until > now
    ok = verify_password(password, user.password_hash)

    if user.status != UserStatus.ACTIVE:
        audit.write(db, user, "auth.login_failed", entity_type="user", entity_id=user.id,
                    detail=f"Sign-in refused: account {user.status}", ip=ip)  # fmt: skip
        return bad

    if ok and not locked:
        user.failed_attempts = 0
        user.locked_until = None
        user.last_sign_in_at = now
        audit.write(db, user, "auth.login", entity_type="user", entity_id=user.id, detail="Signed in", ip=ip)
        return start_session(user)

    # Failure (wrong password, or any attempt while locked — a correct password does not bypass a lock)
    user.failed_attempts = (user.failed_attempts or 0) + 1
    n = user.failed_attempts
    audit.write(db, user, "auth.login_failed", entity_type="user", entity_id=user.id,
                detail=f"Failed sign-in attempt {n}" + (" (account locked)" if locked else ""), ip=ip)  # fmt: skip
    new_lock: timedelta | None = None
    if n == LOCK_SHORT_AFTER:
        new_lock = LOCK_SHORT
    elif n >= LOCK_LONG_AFTER and (n == LOCK_LONG_AFTER or not locked):
        new_lock = LOCK_LONG
    if new_lock is not None:
        user.locked_until = now + new_lock
        minutes = int(new_lock.total_seconds() // 60)
        audit.write(db, user, "auth.locked", entity_type="user", entity_id=user.id,
                    detail=f"Locked for {minutes} minutes after {n} failed attempts", ip=ip)  # fmt: skip
        locked = True
    if locked:
        return Locked(message="This account is temporarily locked after repeated failed sign-in attempts. "
                              "Try again later or reset your password.")  # fmt: skip
    return bad


def logout(db: Session, user: User, claims: TokenClaims, ip: str | None) -> None:
    cache.deny_token(claims.jti, claims.seconds_remaining + 60)
    audit.write(db, user, "auth.logout", entity_type="user", entity_id=user.id, detail="Signed out", ip=ip)


# --- Password reset (FR-02; LLD 3.13) ------------------------------------------------------
def request_password_reset(db: Session, email: str, ip: str | None) -> None:
    """Always succeeds from the caller's point of view — never reveals whether the account exists."""
    user = find_by_email(db, (email or "").strip().lower())
    if user is None or user.status != UserStatus.ACTIVE:
        return
    s = get_settings()
    plaintext, digest = new_link_token()
    db.add(PasswordResetToken(user_id=user.id, token_hash=digest, requested_ip=ip,
                              expires_at=_now() + timedelta(minutes=s.PASSWORD_RESET_MINUTES)))  # fmt: skip
    db.flush()
    notifications.send("auth.password_reset", user.email, {
        "name": user.full_name, "link": notifications.link(f"/reset-password?token={plaintext}"),
        "minutes": s.PASSWORD_RESET_MINUTES,
    })  # fmt: skip


def confirm_password_reset(db: Session, token: str, new_password: str, ip: str | None) -> User:
    row = db.scalars(select(PasswordResetToken).where(
        PasswordResetToken.token_hash == hash_link_token(token)).with_for_update()).first()  # fmt: skip
    if row is None or row.used_at is not None:
        # used_at prevents replay (T-27)
        raise BadRequest("ERR_TOKEN_INVALID", "This link is not valid or has already been used.")
    if row.expires_at < _now():
        raise BadRequest("ERR_TOKEN_EXPIRED", "This link has expired. Request a new one.")
    user = db.get(User, row.user_id)
    if user is None or user.status != UserStatus.ACTIVE:
        raise BadRequest("ERR_TOKEN_INVALID", "This link is not valid or has already been used.")
    check_password_policy(new_password, user.email)
    now = _now()
    for other in db.scalars(select(PasswordResetToken).where(
            PasswordResetToken.user_id == user.id, PasswordResetToken.used_at.is_(None))):  # fmt: skip
        other.used_at = now  # every outstanding reset or activation link dies with this one
    row.used_at = now
    user.password_hash = hash_password(new_password)
    user.failed_attempts = 0
    user.locked_until = None
    if user.role != UserRole.APPLICANT and user.email_verified_at is None:
        user.email_verified_at = _now()  # activation proves control of the staff mailbox
    cache.revoke_user_tokens(user.id)  # LLD 16: revoke on password change
    audit.write(db, user, "auth.password_reset", entity_type="user", entity_id=user.id,
                detail="Password set via emailed link; existing sessions revoked", ip=ip)  # fmt: skip
    return user


# --- Staff administration (FR-121; LLD 5.6) -----------------------------------------------
def create_staff(db: Session, admin: User, email: str, full_name: str, role: UserRole, ip: str | None) -> User:
    if role not in STAFF_ROLES:
        raise GuardFailed("ERR_INVALID_ROLE", "Staff accounts must hold a staff role.",
                          details=[{"field": "role", "code": "ERR_INVALID_ROLE"}])  # fmt: skip
    email = _email_or_400(email)
    name = _name_or_400(full_name)
    if find_by_email(db, email):
        raise BadRequest("ERR_EMAIL_EXISTS", "An account already exists for this email address.",
                         details=[{"field": "email", "code": "ERR_EMAIL_EXISTS"}])  # fmt: skip
    user = User(email=email, full_name=name, role=role, password_hash=unusable_password_hash())
    db.add(user)
    db.flush()
    s = get_settings()
    plaintext, digest = new_link_token()
    db.add(PasswordResetToken(user_id=user.id, token_hash=digest, requested_ip=ip,
                              expires_at=_now() + timedelta(hours=s.ACTIVATION_TOKEN_HOURS)))  # fmt: skip
    audit.write(db, admin, "admin.user_create", entity_type="user", entity_id=user.id,
                detail=f"Created {role.value} account for {email}", ip=ip)  # fmt: skip
    db.flush()
    notifications.send("staff.account_created", email, {
        "name": name, "role": role.value, "hours": s.ACTIVATION_TOKEN_HOURS,
        "link": notifications.link(f"/reset-password?token={plaintext}&activate=1"),
    })  # fmt: skip
    return user


def update_staff(
    db: Session,
    admin: User,
    user_id: int,
    role: UserRole | None,
    status: UserStatus | None,
    ip: str | None,
    *,
    full_name: str | None = None,
    email: str | None = None,
) -> User:
    # FR-122: re-asserted here as well as on the route. This function is now the way a staff
    # member's sign-in address is changed, so it does not rely on one caller getting it right.
    if admin.role != UserRole.ADMIN:
        raise Forbidden()
    user = db.get(User, user_id, with_for_update=True)
    if user is None:
        raise NotFound()
    if user.id == admin.id:
        raise GuardFailed("ERR_SELF_MODIFICATION",
                          "You cannot change your own role or status. Ask another administrator.")  # fmt: skip
    if user.role == UserRole.APPLICANT:
        raise GuardFailed("ERR_INVALID_ROLE", "Applicant accounts are not managed here.")
    changed = False
    # FR-100: correcting a name or an address. Both are audited with the old value, because the
    # audit trail names people and a silent rename would make older entries unattributable.
    if full_name is not None and full_name.strip() and full_name.strip() != user.full_name:
        old_name = user.full_name
        user.full_name = full_name.strip()
        audit.write(db, admin, "admin.user_update", entity_type="user", entity_id=user.id,
                    detail=f"{user.email}: name '{old_name}' -> '{user.full_name}'", ip=ip)  # fmt: skip
        changed = True
    if email is not None and email.lower() != user.email:
        new_email = email.lower()
        if db.scalars(select(User).where(User.email == new_email, User.id != user.id)).first():
            raise GuardFailed("ERR_EMAIL_IN_USE", "Another account already uses that email address.",
                              details=[{"field": "email", "code": "ERR_EMAIL_IN_USE"}])  # fmt: skip
        old_email = user.email
        user.email = new_email
        audit.write(db, admin, "admin.user_update", entity_type="user", entity_id=user.id,
                    detail=f"Email '{old_email}' -> '{new_email}'; active sessions revoked", ip=ip)  # fmt: skip
        changed = True
    if role is not None and role != user.role:
        if role not in STAFF_ROLES:
            raise GuardFailed("ERR_INVALID_ROLE", "Staff accounts must hold a staff role.",
                              details=[{"field": "role", "code": "ERR_INVALID_ROLE"}])  # fmt: skip
        old = user.role
        user.role = role
        audit.write(db, admin, "admin.user_role_change", entity_type="user", entity_id=user.id,
                    detail=f"{user.email}: role {old} -> {role}; active sessions revoked", ip=ip)  # fmt: skip
        changed = True
    if status is not None and status != user.status:
        old_status = user.status
        user.status = status.value
        action = "admin.user_reactivate" if status == UserStatus.ACTIVE else "admin.user_deactivate"
        audit.write(db, admin, action, entity_type="user", entity_id=user.id,
                    detail=f"{user.email}: status {old_status} -> {status.value}", ip=ip)  # fmt: skip
        changed = True
    if changed:
        db.flush()
        cache.revoke_user_tokens(user.id)  # LLD 16, T-26
    return user
