"""Request dependencies: authentication, role checks, client IP, rate limiting (LLD 6, 16).

Role checks are enforced by a dependency on every router and re-asserted in the service layer,
so an unauthorised request is rejected regardless of entry point (FR-122).
"""

from __future__ import annotations

import ipaddress
from collections.abc import Callable
from dataclasses import dataclass

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.errors import Forbidden, RateLimited, Unauthenticated
from app.models.enums import UserRole, UserStatus
from app.models.user import User
from app.services import cache
from app.services.auth import TokenClaims, decode_token

bearer = HTTPBearer(auto_error=False)


def client_ip(request: Request) -> str | None:
    """Client address for the audit trail. Behind the load balancer, run uvicorn with
    --proxy-headers --forwarded-allow-ips=<LB range> so this is the real client, not the proxy."""
    host = request.client.host if request.client else None
    try:
        return str(ipaddress.ip_address(host)) if host else None
    except ValueError:
        return None


@dataclass
class CurrentUser:
    user: User
    claims: TokenClaims

    @property
    def role(self) -> UserRole:
        return self.user.role


def get_current(
    request: Request,
    creds: HTTPAuthorizationCredentials | None = Depends(bearer),
    db: Session = Depends(get_db),
) -> CurrentUser:
    if creds is None or creds.scheme.lower() != "bearer":
        raise Unauthenticated()
    claims = decode_token(creds.credentials)
    if cache.is_denied(claims.jti):
        raise Unauthenticated("ERR_TOKEN_EXPIRED", "Your session has ended. Please sign in again.")
    if not cache.session_active(claims.jti):
        # NFR-06 inactivity timeout
        raise Unauthenticated("ERR_TOKEN_EXPIRED", "You were signed out after a period of inactivity.")
    user = db.get(User, claims.user_id)
    if user is None or user.status != UserStatus.ACTIVE or str(user.role) != claims.role:
        raise Unauthenticated("ERR_TOKEN_EXPIRED", "Your session is no longer valid. Please sign in again.")
    s = get_settings()
    cache.touch_session(claims.jti, s.SESSION_INACTIVITY_MINUTES * 60)
    request.state.user_id = user.id
    _rate_limit(f"user:{user.id}", s.RATE_LIMIT_GENERAL_PER_MINUTE)
    return CurrentUser(user, claims)


def require_roles(*roles: UserRole) -> Callable[..., CurrentUser]:
    allowed = frozenset(roles)

    def dependency(current: CurrentUser = Depends(get_current)) -> CurrentUser:
        if current.user.role not in allowed:
            raise Forbidden()
        return current

    return dependency


def _rate_limit(bucket: str, limit: int) -> None:
    if not get_settings().RATE_LIMIT_ENABLED:
        return
    allowed, reset_in = cache.hit_rate_limit(bucket, limit)
    if not allowed:
        raise RateLimited(headers={"Retry-After": str(reset_in)})


def auth_rate_limit(request: Request) -> None:
    """LLD 16: 10 requests per minute per IP on authentication endpoints."""
    _rate_limit(f"auth:{client_ip(request) or 'unknown'}", get_settings().RATE_LIMIT_AUTH_PER_MINUTE)
