"""Password hashing, password policy, tokens and one-time links (LLD 16, 5.1, 3.13).

Services contain no FastAPI imports (LLD 2.1), so these rules are callable from the worker and
directly unit-testable.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path

import bcrypt
import jwt

from app.config import get_settings
from app.errors import BadRequest, Unauthenticated

BCRYPT_COST = 12  # NFR-05
BCRYPT_MAX_BYTES = 72
MIN_PASSWORD_LENGTH = 10  # LLD 9

# A valid hash of a random value, used to equalise timing when the account does not exist.
_DUMMY_HASH = bcrypt.hashpw(b"timing-equaliser-not-a-password", bcrypt.gensalt(BCRYPT_COST))


# --- Passwords -----------------------------------------------------------------------------
def _prepare(password: str) -> bytes:
    # LLD 16: input truncated to 72 bytes before hashing (bcrypt's own limit, made explicit).
    return password.encode("utf-8")[:BCRYPT_MAX_BYTES]


def hash_password(password: str) -> str:
    return bcrypt.hashpw(_prepare(password), bcrypt.gensalt(BCRYPT_COST)).decode("ascii")


def verify_password(password: str, password_hash: str | None) -> bool:
    if not password_hash:
        bcrypt.checkpw(_prepare(password), _DUMMY_HASH)
        return False
    try:
        return bcrypt.checkpw(_prepare(password), password_hash.encode("ascii"))
    except ValueError:
        return False


def unusable_password_hash() -> str:
    """Hash of a random secret nobody knows — for staff accounts awaiting activation."""
    return hash_password(secrets.token_urlsafe(48))


@lru_cache
def _common_passwords() -> frozenset[str]:
    path = Path(__file__).resolve().parent.parent / "data" / "common_passwords.txt"
    lines = path.read_text(encoding="utf-8").splitlines()
    return frozenset(line.strip().lower() for line in lines if line.strip() and not line.startswith("#"))


def check_password_policy(password: str, email: str | None = None) -> None:
    """Raise 400 ERR_WEAK_PASSWORD when the password fails LLD 9."""
    reasons: list[str] = []
    if len(password) < MIN_PASSWORD_LENGTH:
        reasons.append(f"Use at least {MIN_PASSWORD_LENGTH} characters.")
    if password.strip().lower() in _common_passwords():
        reasons.append("This password is too common.")
    if len(set(password)) <= 2:
        reasons.append("Use a mix of characters.")
    if email:
        local = email.split("@", 1)[0].lower()
        if password.lower() == email.lower() or (len(local) >= 4 and local in password.lower()):
            reasons.append("Do not base the password on your email address.")
    if reasons:
        raise BadRequest(
            "ERR_WEAK_PASSWORD",
            "Choose a stronger password. " + " ".join(reasons),
            details=[{"field": "password", "code": "ERR_WEAK_PASSWORD", "message": r} for r in reasons],
        )


# --- Access tokens (JWT HS256; claims sub, role, name, iat, exp, jti) -----------------------
@dataclass(frozen=True)
class TokenClaims:
    user_id: int
    role: str
    name: str
    jti: str
    issued_at: datetime
    expires_at: datetime

    @property
    def seconds_remaining(self) -> int:
        return max(0, int((self.expires_at - datetime.now(UTC)).total_seconds()))


def issue_token(user_id: int, role: str, name: str, *, now: datetime | None = None) -> tuple[str, TokenClaims]:
    s = get_settings()
    now = now or datetime.now(UTC)
    exp = now + timedelta(hours=s.SESSION_EXPIRY_HOURS)
    jti = uuid.uuid4().hex
    payload = {
        "sub": str(user_id),
        "role": role,
        "name": name,
        "iat": int(now.timestamp()),
        "exp": int(exp.timestamp()),
        "jti": jti,
    }
    token = jwt.encode(payload, s.SECRET_KEY, algorithm=s.JWT_ALGORITHM)
    return token, TokenClaims(user_id, role, name, jti, now, exp)


def decode_token(token: str) -> TokenClaims:
    s = get_settings()
    try:
        p = jwt.decode(
            token,
            s.SECRET_KEY,
            algorithms=[s.JWT_ALGORITHM],
            options={"require": ["sub", "role", "iat", "exp", "jti"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise Unauthenticated("ERR_TOKEN_EXPIRED", "Your session has expired. Please sign in again.") from exc
    except jwt.PyJWTError as exc:
        raise Unauthenticated() from exc
    try:
        return TokenClaims(
            user_id=int(p["sub"]),
            role=str(p["role"]),
            name=str(p.get("name", "")),
            jti=str(p["jti"]),
            issued_at=datetime.fromtimestamp(p["iat"], UTC),
            expires_at=datetime.fromtimestamp(p["exp"], UTC),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise Unauthenticated() from exc


# --- One-time link tokens (password reset, activation, email verification) -----------------
def new_link_token() -> tuple[str, str]:
    """Return (plaintext, sha256_hex). Only the hash is stored (LLD 3.13)."""
    plaintext = secrets.token_urlsafe(32)
    return plaintext, hash_link_token(plaintext)


def hash_link_token(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()
