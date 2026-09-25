"""Connection plumbing: resolve the database URL and hand out sessions."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker


def database_url() -> str:
    # DATABASE_URL, else the POSTGRES_* vars - resolved the same way in Alembic's env.py.
    url = os.getenv("DATABASE_URL")
    if url:
        return url
    user = os.getenv("POSTGRES_USER", "conjectures")
    password = os.getenv("POSTGRES_PASSWORD", "conjectures")
    host = os.getenv("POSTGRES_HOST", "localhost")
    port = os.getenv("POSTGRES_PORT", "5432")
    name = os.getenv("POSTGRES_DB", "conjectures")
    return f"postgresql+psycopg://{user}:{password}@{host}:{port}/{name}"


def create_db_engine(url: str | None = None, *, echo: bool = False, pool_size: int = 5) -> Engine:
    # Small pool for four processes on 50 connections; pre-ping drops ones the server killed.
    return create_engine(
        url or database_url(),
        pool_pre_ping=True,
        pool_size=pool_size,
        max_overflow=pool_size,
        pool_recycle=1800,
        echo=echo,
        future=True,
    )


def session_factory(engine: Engine) -> sessionmaker[Session]:
    # expire_on_commit=False keeps returned rows readable after the unit of work closes.
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    # Transactional scope: commit on success, roll back on error, always close.
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
