"""Engine, session factory and request-scoped session dependency (LLD 2.1).

The application connects as a member of app_role. That role has no UPDATE or DELETE
on audit_entries, so tamper-evidence rests on database permission (LLD 3.10, NFR-12).
Routers own the transaction boundary: services add and flush, the router commits.
"""

from collections.abc import Iterator
from functools import lru_cache

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings


@lru_cache
def get_engine() -> Engine:
    settings = get_settings()
    return create_engine(settings.DATABASE_URL, pool_pre_ping=True, pool_size=10, max_overflow=10, future=True)


@lru_cache
def get_sessionmaker() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(), autoflush=True, expire_on_commit=False, future=True)


def get_db() -> Iterator[Session]:
    session = get_sessionmaker()()
    try:
        yield session
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
