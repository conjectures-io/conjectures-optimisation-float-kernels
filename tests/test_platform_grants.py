"""The platform API's role gets exactly what it uses, on the schema the migrations build.

Once conjectures-validator serves FloatLib, it reads and queues through this database as
`platform_api` (deploy/platform/platform_api.sql, applied by deploy/platform/grant.sh). A grant
missing from that file is a 500 on the platform's side the first time a read touches the table;
one too many lets the platform change what the gate or the weight setter own. So this applies the
file to a freshly migrated database and checks every privilege on every table and sequence
against the contract below (deploy/platform/CONTRACT.md).
"""

from __future__ import annotations

import os
import re
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from db import models

REPO = Path(__file__).resolve().parents[1]
GRANTS = REPO / "deploy/platform/platform_api.sql"
ALEMBIC = REPO / "deploy/migrate"
ROLE = "platform_api"

# What the platform does with each table; every other table and privilege stays denied.
EXPECTED: dict[str, set[str]] = {
    "submissions": {"SELECT", "INSERT"},
    "submission_files": {"SELECT", "INSERT"},
    "rate_limit_windows": {"SELECT", "INSERT", "UPDATE"},
    "registrations": {"SELECT"},
    "entitlement_claims": {"SELECT"},
    "weight_sets": {"SELECT"},
    "score_snapshots": {"SELECT"},
}
EXPECTED_SEQUENCES = {"submissions_id_seq"}
TABLE_PRIVILEGES = ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")


def test_every_table_the_grants_name_is_one_the_models_define() -> None:
    # Table grants only (`GRANT ... ON <tables> TO`), never on a schema, a sequence or `ALL ... IN`.
    clauses = re.findall(
        r"\bGRANT\s+[A-Z, ]+\s+ON\s+(?!SCHEMA\b|SEQUENCE\b|ALL\b)([a-z_,\s]+?)\s+TO\s+" + ROLE,
        GRANTS.read_text(),
    )
    named = {table.strip() for clause in clauses for table in clause.split(",")}
    assert named == set(EXPECTED)
    assert named <= set(models.Base.metadata.tables)


@pytest.fixture
def migrated(database_url: str) -> Iterator[sa.Engine]:
    # A scratch database built by the revisions, reusing a cluster-wide `platform_api` role if one
    # exists and dropping one it created after its database.
    base, _ = database_url.rsplit("/", 1)
    name = f"conjectures_grants_{uuid.uuid4().hex[:8]}"
    admin = sa.create_engine(f"{base}/postgres", isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(sa.text(f'CREATE DATABASE "{name}"'))
        created_role = not conn.execute(
            sa.text("SELECT EXISTS (SELECT FROM pg_roles WHERE rolname = :r)"), {"r": ROLE}
        ).scalar_one()
        if created_role:
            conn.execute(sa.text(f"CREATE ROLE {ROLE} NOLOGIN"))
    url = f"{base}/{name}"
    config = Config(str(ALEMBIC / "alembic.ini"))
    config.set_main_option("script_location", str(ALEMBIC / "alembic"))
    config.set_main_option("sqlalchemy.url", url)
    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = url
    engine = sa.create_engine(url)
    try:
        command.upgrade(config, "head")
        yield engine
    finally:
        engine.dispose()
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous
        with admin.connect() as conn:
            conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{name}"'))
            if created_role:
                conn.execute(sa.text(f"DROP ROLE IF EXISTS {ROLE}"))
        admin.dispose()


def apply_grants(engine: sa.Engine) -> None:
    # The whole file in one simple-protocol call, as psql sends it: it holds a DO block,
    # so it cannot be split on semicolons.
    raw = engine.raw_connection()
    try:
        raw.cursor().execute(GRANTS.read_text())
        raw.commit()
    finally:
        raw.close()


def granted(engine: sa.Engine) -> tuple[dict[str, set[str]], set[str]]:
    with engine.connect() as conn:
        tables = (
            conn.execute(sa.text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'"))
            .scalars()
            .all()
        )
        sequences = (
            conn.execute(sa.text("SELECT sequence_name FROM information_schema.sequences"))
            .scalars()
            .all()
        )
        table_grants = {
            table: {
                privilege
                for privilege in TABLE_PRIVILEGES
                if conn.execute(
                    sa.text("SELECT has_table_privilege(:r, :t, :p)"),
                    {"r": ROLE, "t": f"public.{table}", "p": privilege},
                ).scalar_one()
            }
            for table in tables
        }
        sequence_grants = {
            sequence
            for sequence in sequences
            if conn.execute(
                sa.text("SELECT has_sequence_privilege(:r, :s, 'USAGE')"),
                {"r": ROLE, "s": f"public.{sequence}"},
            ).scalar_one()
        }
    return {t: p for t, p in table_grants.items() if p}, sequence_grants


def test_the_role_gets_exactly_what_the_platform_uses(migrated: sa.Engine) -> None:
    apply_grants(migrated)
    tables, sequences = granted(migrated)
    assert tables == EXPECTED
    assert sequences == EXPECTED_SEQUENCES
    with migrated.connect() as conn:
        assert conn.execute(
            sa.text("SELECT has_database_privilege(:r, current_database(), 'CONNECT')"),
            {"r": ROLE},
        ).scalar_one()


def test_rerunning_resets_grants_made_by_hand(migrated: sa.Engine) -> None:
    apply_grants(migrated)
    with migrated.begin() as conn:
        conn.execute(sa.text(f"GRANT UPDATE, DELETE ON submissions TO {ROLE}"))
        conn.execute(sa.text(f"GRANT SELECT ON registrations, weight_sets TO {ROLE}"))
        conn.execute(sa.text(f"GRANT UPDATE ON weight_sets TO {ROLE}"))
    apply_grants(migrated)
    tables, _ = granted(migrated)
    assert tables == EXPECTED
