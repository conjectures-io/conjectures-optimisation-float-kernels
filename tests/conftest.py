"""A scratch Postgres database per test session and its fixtures; db tests skip without a server."""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TypedDict

import pytest
import sqlalchemy as sa

import db as store_pkg
from db import models

REPO = Path(__file__).resolve().parents[1]

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
# The verifier fingerprint the suite runs under, and one from an older verifier.
FINGERPRINT = "f" * 64
OLD_FINGERPRINT = "e" * 64


@pytest.fixture(autouse=True)
def current_verifier(monkeypatch: pytest.MonkeyPatch) -> str:
    # Pin the current verifier, so scoring does not depend on this checkout's gate.py hash.
    monkeypatch.setenv("GATE_REQUIRED_FINGERPRINT", FINGERPRINT)
    return FINGERPRINT


def _admin_url() -> str:
    # The server's maintenance database, used only to CREATE/DROP the scratch one.
    user = os.getenv("POSTGRES_USER", "conjectures")
    password = os.getenv("POSTGRES_PASSWORD", "conjectures")
    host = os.getenv("POSTGRES_HOST", "localhost")
    port = os.getenv("POSTGRES_PORT", "5432")
    return f"postgresql+psycopg://{user}:{password}@{host}:{port}/postgres"


def _scratch_url(name: str) -> str:
    return _admin_url().rsplit("/", 1)[0] + f"/{name}"


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    # Create a scratch database for the session, or skip the db suite when no server answers.
    name = f"conjectures_test_{uuid.uuid4().hex[:10]}"
    try:
        admin = sa.create_engine(_admin_url(), isolation_level="AUTOCOMMIT")
        with admin.connect() as conn:
            conn.execute(sa.text(f'CREATE DATABASE "{name}"'))
    except Exception as exc:  # noqa: BLE001 - no server, wrong password, no permission
        pytest.skip(
            f"no Postgres for the store tests ({exc.__class__.__name__}); "
            "try `docker compose up -d db`"
        )
    url = _scratch_url(name)
    try:
        engine = sa.create_engine(url)
        from alembic import command
        from alembic.config import Config

        cfg = Config(str(REPO / "deploy/migrate/alembic.ini"))
        cfg.set_main_option("script_location", str(REPO / "deploy/migrate/alembic"))
        old_url = os.environ.get("DATABASE_URL")
        os.environ["DATABASE_URL"] = url
        try:
            command.upgrade(cfg, "head")
        finally:
            if old_url is None:
                os.environ.pop("DATABASE_URL", None)
            else:
                os.environ["DATABASE_URL"] = old_url
        engine.dispose()
        yield url
    finally:
        with admin.connect() as conn:
            # Terminate leftover connections first, or the DROP blocks on them.
            conn.execute(
                sa.text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :n AND pid <> pg_backend_pid()"
                ),
                {"n": name},
            )
            conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{name}"'))
        admin.dispose()


class Clock(TypedDict):
    # A settable clock: `t` is what db.clock.now returns, `tick` moves it.

    t: datetime
    tick: Callable[..., None]


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    # A settable clock, so submissions can be ordered without sleeping.
    state: Clock = {"t": T0, "tick": lambda *_: None}
    monkeypatch.setattr(store_pkg.clock, "now", lambda: state["t"])

    def tick(seconds: float = 1.0) -> None:
        state["t"] = state["t"] + timedelta(seconds=seconds)

    state["tick"] = tick
    return state


@pytest.fixture
def store(database_url, clock):
    # A clean store: every table truncated, so tests cannot leak into each other.
    s = store_pkg.connect(database_url)
    with s.engine.begin() as conn:
        names = [t.name for t in reversed(models.Base.metadata.sorted_tables)]
        tables = ", ".join(f'"{n}"' for n in names)
        conn.execute(sa.text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
        conn.execute(sa.text("INSERT INTO coordinator_state(id) VALUES (1)"))
    try:
        yield s
    finally:
        s.close()


def register(store, hotkey: str, *, uid: int = 0, block: int = 1000) -> int:
    # Insert one registration row (one submission slot), as the chain watcher would, and return its
    # id.
    with store_pkg.session_scope(store.sessions) as session:
        row = models.Registration(
            uid=uid,
            ss58_hot=hotkey,
            ss58_cold=f"cold-{hotkey[:8]}",
            block=block,
            block_date=T0 - timedelta(days=1),
        )
        session.add(row)
        session.flush()
        return int(row.id)


pytest_plugins = ["test_exact_competition"]
