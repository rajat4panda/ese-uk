"""Authentication endpoints (LLD 5.1)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response, status
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser, auth_rate_limit, client_ip, get_current
from app.config import get_settings
from app.database import get_db
from app.errors import BadRequest
from app.schemas.auth import (
    LoginIn,
    RegisterIn,
    ResetConfirmIn,
    ResetRequestIn,
    TokenOut,
    UserOut,
    VerifyEmailIn,
    user_out,
)
from app.services import accounts

router = APIRouter(tags=["authentication"])


@router.post("/auth/register", status_code=201, response_model=TokenOut, dependencies=[Depends(auth_rate_limit)])
def register(body: RegisterIn, request: Request, db: Session = Depends(get_db)) -> TokenOut:
    """Public. Creates an applicant account only (FR-01)."""
    user = accounts.register(db, body.email, body.password, body.full_name, client_ip(request))
    accounts.send_verification(db, user)
    session = accounts.start_session(user)
    db.commit()
    return TokenOut(token=session.token, expires_at=session.claims.expires_at, user=user_out(user))


@router.post("/auth/login", response_model=TokenOut, dependencies=[Depends(auth_rate_limit)])
def login(body: LoginIn, request: Request, db: Session = Depends(get_db)) -> TokenOut:
    result = accounts.login(db, body.email, body.password, client_ip(request))
    db.commit()  # failed attempts and lockouts are evidence and must persist (NFR-16)
    if isinstance(result, Exception):
        raise result
    return TokenOut(token=result.token, expires_at=result.claims.expires_at, user=user_out(result.user))


@router.post("/auth/logout", status_code=204)
def logout(request: Request, current: CurrentUser = Depends(get_current), db: Session = Depends(get_db)) -> Response:
    accounts.logout(db, current.user, current.claims, client_ip(request))
    db.commit()
    return Response(status_code=204)


@router.post("/auth/password-reset/request", status_code=202, dependencies=[Depends(auth_rate_limit)])
def password_reset_request(body: ResetRequestIn, request: Request, db: Session = Depends(get_db)) -> dict:
    """Always 202 — never reveals whether an account exists (FR-02)."""
    accounts.request_password_reset(db, body.email, client_ip(request))
    db.commit()
    return {"message": "If an account exists for that address, we have sent a link to reset the password."}


@router.post("/auth/password-reset/confirm", dependencies=[Depends(auth_rate_limit)])
def password_reset_confirm(body: ResetConfirmIn, request: Request, db: Session = Depends(get_db)) -> dict:
    accounts.confirm_password_reset(db, body.token, body.new_password, client_ip(request))
    db.commit()
    return {"message": "Your password has been set. You can now sign in."}


@router.post("/auth/verify-email/confirm", dependencies=[Depends(auth_rate_limit)])
def verify_email_confirm(body: VerifyEmailIn, request: Request, db: Session = Depends(get_db)) -> dict:
    accounts.confirm_verification(db, body.token, client_ip(request))
    db.commit()
    return {"message": "Thank you — your email address is confirmed."}


@router.post("/auth/verify-email/resend", status_code=status.HTTP_202_ACCEPTED, dependencies=[Depends(auth_rate_limit)])
def verify_email_resend(current: CurrentUser = Depends(get_current), db: Session = Depends(get_db)) -> dict:
    if current.user.email_verified_at is not None or not get_settings().email_verification_required:
        raise BadRequest("ERR_ALREADY_VERIFIED", "Your email address is already confirmed.")
    accounts.send_verification(db, current.user)
    db.commit()
    return {"message": "We have sent a new confirmation link."}


@router.get("/me", response_model=UserOut)
def me(current: CurrentUser = Depends(get_current)) -> UserOut:
    return user_out(current.user)
