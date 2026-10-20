"""The migrations and the models must describe the same database."""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy.exc import IntegrityError

from db import models
from db.status import STATE_VALUES, SubmissionState

REPO = Path(__file__).resolve().parents[1]

ALEMBIC = REPO / "deploy/migrate"


def _alembic_config(url: str) -> Config:
    config = Config(str(ALEMBIC / "alembic.ini"))
    config.set_main_option("script_location", str(ALEMBIC / "alembic"))
    config.set_main_option("sqlalchemy.url", url)
    return config


@pytest.fixture
def migrated(database_url):
    # A scratch database built from the migrations, so the models are compared against them.
    base, _ = database_url.rsplit("/", 1)
    name = f"conjectures_migrated_{uuid.uuid4().hex[:8]}"
    admin = sa.create_engine(f"{base}/postgres", isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(sa.text(f'CREATE DATABASE "{name}"'))
    url = f"{base}/{name}"
    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = url
    try:
        command.upgrade(_alembic_config(url), "head")
        yield url
    finally:
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous
        with admin.connect() as conn:
            conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{name}"'))
        admin.dispose()


def test_the_migrations_build_exactly_what_the_models_describe(migrated):
    engine = sa.create_engine(migrated)
    try:
        with engine.connect() as conn:
            context = MigrationContext.configure(
                conn, opts={"compare_type": True, "compare_server_default": True}
            )
            diff = compare_metadata(context, models.Base.metadata)
    finally:
        engine.dispose()
    assert diff == [], f"models and migrations have drifted: {diff}"


def test_the_migrations_are_reversible(migrated):
    # A revision that cannot be undone is a revision nobody dares apply.
    config = _alembic_config(migrated)
    command.downgrade(config, "base")
    engine = sa.create_engine(migrated)
    try:
        remaining = sa.inspect(engine).get_table_names()
    finally:
        engine.dispose()
    assert set(remaining) <= {"alembic_version"}


def test_exact_upgrade_preserves_legacy_rows_and_refuses_destructive_downgrade(migrated):
    config = _alembic_config(migrated)
    command.downgrade(config, "0017")
    engine = sa.create_engine(migrated)
    try:
        with engine.begin() as conn:
            sid = conn.scalar(
                sa.text(
                    "INSERT INTO submissions(hotkey,digest,slot,state) "
                    "VALUES (:hotkey,:digest,:slot,:state) RETURNING id"
                ),
                {"hotkey": "legacy-miner", "digest": "a" * 64, "slot": "full", "state": "accepted"},
            )
            conn.execute(
                sa.text(
                    "INSERT INTO submission_files(submission_id,name,content) "
                    "VALUES (:id,:name,:body)"
                ),
                {"id": sid, "name": "Kernel.lean", "body": b"legacy kernel"},
            )
            conn.execute(
                sa.text(
                    "INSERT INTO promotions(slot,submission_id,champion_sha256) "
                    "VALUES (:slot,:id,:hash)"
                ),
                {"slot": "full", "id": sid, "hash": "a" * 64},
            )
        command.upgrade(config, "head")
        with engine.begin() as conn:
            assert (
                conn.scalar(sa.text("SELECT hotkey FROM submissions WHERE id=:id"), {"id": sid})
                == "legacy-miner"
            )
            assert (
                conn.scalar(
                    sa.text("SELECT content FROM submission_files WHERE submission_id=:id"),
                    {"id": sid},
                )
                == b"legacy kernel"
            )
            assert conn.scalar(sa.text("SELECT submission_id FROM promotions")) == sid
            conn.execute(
                sa.text(
                    "INSERT INTO submissions(hotkey,digest,slot) VALUES (:hotkey,:digest,:slot)"
                ),
                {"hotkey": "exact-miner", "digest": "b" * 64, "slot": "exact"},
            )
        with pytest.raises(RuntimeError, match="archive exact submissions"):
            command.downgrade(config, "0017")
        with engine.connect() as conn:
            assert "exact_jobs" in sa.inspect(conn).get_table_names()
    finally:
        engine.dispose()


# ── The contracts the schema encodes ──────────────────────────────────────


def test_the_state_check_matches_the_python_enum(store):
    # The CHECK constraint and the enum spell one list; drift means unstorable states.
    assert STATE_VALUES == ("queued", "verifying", "accepted", "rejected", "error")
    with store.engine.connect() as conn:
        rendered = conn.execute(
            sa.text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conname = 'ck_submissions_state'"
            )
        ).scalar_one()
    for state in SubmissionState:
        assert f"'{state.value}'" in rendered


def test_an_unknown_state_is_refused_by_the_database(store):
    sid, _ = store.submissions.add("5" * 47, "a" * 64)
    with pytest.raises(IntegrityError):
        with store.engine.begin() as conn:
            conn.execute(
                sa.text("UPDATE submissions SET state = 'promoted' WHERE id = :i"), {"i": sid}
            )


def test_a_registration_cannot_be_recorded_twice_for_one_block(store):
    from conftest import register

    register(store, "5" * 47, uid=3, block=42)
    with pytest.raises(IntegrityError):
        register(store, "5" * 47, uid=3, block=42)


def test_the_same_file_from_one_hotkey_is_one_submission(store):
    first, fresh = store.submissions.add("5" * 47, "a" * 64)
    second, again = store.submissions.add("5" * 47, "a" * 64)
    assert (fresh, again) == (True, False) and first == second
