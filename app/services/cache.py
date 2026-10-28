"""Redis adapter — token deny-list, session activity, rate-limit counters (LLD 2, 16).

Redis holds no durable data. If it is flushed, users are signed out; no evidence is lost.
"""

from __future__ import annotations

import time
from functools import lru_cache

import redis

from app.config import get_settings


@lru_cache
def get_redis() -> redis.Redis:
    return redis.Redis.from_url(get_settings().REDIS_URL, decode_responses=True, socket_timeout=2)


def _r() -> redis.Redis:
    return get_redis()


# --- Token issue tracking and deny-list (LLD 16: token revocation) ------------------------
def track_token(user_id: int, jti: str, ttl_seconds: int) -> None:
    key = f"user_tokens:{user_id}"
    pipe = _r().pipeline()
    pipe.zadd(key, {jti: time.time() + ttl_seconds})
    pipe.expire(key, ttl_seconds)
    pipe.execute()


def deny_token(jti: str, ttl_seconds: int) -> None:
    if ttl_seconds > 0:
        _r().set(f"deny:{jti}", "1", ex=ttl_seconds)
    _r().delete(f"session:{jti}")


def is_denied(jti: str) -> bool:
    return bool(_r().exists(f"deny:{jti}"))


def revoke_user_tokens(user_id: int) -> int:
    """Deny every unexpired token issued to the user — password change, role change, deactivation."""
    key = f"user_tokens:{user_id}"
    now = time.time()
    r = _r()
    entries = r.zrangebyscore(key, now, "+inf", withscores=True)
    for jti, expires_at in entries:
        deny_token(jti, int(expires_at - now) + 1)
    r.delete(key)
    return len(entries)


# --- Session inactivity (NFR-06) ---------------------------------------------------------
def touch_session(jti: str, idle_seconds: int) -> None:
    _r().set(f"session:{jti}", "1", ex=idle_seconds)


def session_active(jti: str) -> bool:
    return bool(_r().exists(f"session:{jti}"))


# --- Fixed-window rate limiting (LLD 16) -------------------------------------------------
def hit_rate_limit(bucket: str, limit: int, window_seconds: int = 60) -> tuple[bool, int]:
    """Count a request; return (allowed, seconds_until_reset)."""
    window = int(time.time() // window_seconds)
    key = f"rl:{bucket}:{window}"
    r = _r()
    pipe = r.pipeline()
    pipe.incr(key)
    pipe.expire(key, window_seconds + 1)
    count, _ = pipe.execute()
    reset_in = window_seconds - int(time.time() % window_seconds)
    return count <= limit, reset_in


def count_unknown_login(email: str, window_seconds: int) -> int:
    """Failed sign-ins against an address with no account, within the lockout window."""
    import hashlib

    key = "unknown_login:" + hashlib.sha256(email.encode()).hexdigest()
    pipe = _r().pipeline()
    pipe.incr(key)
    pipe.expire(key, window_seconds, nx=True)
    count, _ = pipe.execute()
    return int(count)


def ping() -> bool:
    try:
        return bool(_r().ping())
    except redis.RedisError:
        return False
