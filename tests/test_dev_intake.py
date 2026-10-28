"""Development intake is explicit and cannot accidentally look like a chain registration."""

import hashlib

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from conftest import register


def source(conn):
    content = b"import ContractExact\n"
    sha = hashlib.sha256(content).hexdigest()
    conn.execute(
        text("INSERT INTO sources(sha256,content) VALUES (:sha,:content)"),
        dict(sha=sha, content=content),
    )
    return sha


def test_registered_intake_defaults_to_subnet(store):
    rid = register(store, "alice")
    with store.engine.begin() as c:
        sha = source(c)
        mode = c.scalar(
            text(
                "INSERT INTO submissions(hotkey,registration_id,source_sha256) "
                "VALUES ('alice',:rid,:sha) RETURNING intake_mode"
            ),
            dict(rid=rid, sha=sha),
        )
        assert mode == "subnet"


def test_tagged_dev_intake_needs_no_registration(store):
    with store.engine.begin() as c:
        sha = source(c)
        c.execute(
            text(
                "INSERT INTO submissions(hotkey,source_sha256,intake_mode) "
                "VALUES ('alice',:sha,'dev')"
            ),
            dict(sha=sha),
        )
        assert c.scalar(text("SELECT registration_id FROM submissions")) is None
        assert c.scalar(text("SELECT count(*) FROM registrations")) == 0


@pytest.mark.parametrize("mode", ["subnet", "invalid"])
def test_registrationless_upload_requires_dev_tag(store, mode):
    with pytest.raises(IntegrityError):
        with store.engine.begin() as c:
            sha = source(c)
            c.execute(
                text(
                    "INSERT INTO submissions(hotkey,source_sha256,intake_mode) "
                    "VALUES ('alice',:sha,:mode)"
                ),
                dict(sha=sha, mode=mode),
            )


def test_dev_upload_cannot_claim_real_registration(store):
    rid = register(store, "alice")
    with pytest.raises(IntegrityError):
        with store.engine.begin() as c:
            sha = source(c)
            c.execute(
                text(
                    "INSERT INTO submissions(hotkey,registration_id,source_sha256,intake_mode) "
                    "VALUES ('alice',:rid,:sha,'dev')"
                ),
                dict(sha=sha, rid=rid),
            )


def test_dev_intake_migration_roundtrip(store, database_url, monkeypatch):
    from pathlib import Path

    from alembic import command
    from alembic.config import Config
    from sqlalchemy import inspect

    monkeypatch.setenv("DATABASE_URL", database_url)
    root = Path(__file__).resolve().parents[1]
    cfg = Config(str(root / "deploy/migrate/alembic.ini"))
    cfg.set_main_option("script_location", str(root / "deploy/migrate/alembic"))
    with store.engine.begin() as c:
        sha = source(c)
        sid = c.scalar(
            text(
                "INSERT INTO submissions(hotkey,source_sha256,intake_mode) "
                "VALUES ('dev',:sha,'dev') RETURNING id"
            ),
            dict(sha=sha),
        )
    with pytest.raises(RuntimeError, match="Remove development"):
        command.downgrade(cfg, "tensor_0001")
    with store.engine.begin() as c:
        c.execute(text("DELETE FROM submissions WHERE id=:id"), dict(id=sid))
        c.exec_driver_sql("CREATE ROLE platform_api")
        c.exec_driver_sql("GRANT INSERT(hotkey) ON submissions TO platform_api")
    try:
        command.downgrade(cfg, "tensor_0001")
        assert "intake_mode" not in {
            x["name"] for x in inspect(store.engine).get_columns("submissions")
        }
        rid = register(store, "alice")
        with store.engine.begin() as c:
            c.execute(
                text(
                    "INSERT INTO submissions(hotkey,registration_id,source_sha256) "
                    "VALUES ('alice',:rid,:sha)"
                ),
                dict(rid=rid, sha=sha),
            )
        command.upgrade(cfg, "head")
        with store.engine.connect() as c:
            assert c.scalar(text("SELECT intake_mode FROM submissions")) == "subnet"
            assert c.scalar(
                text(
                    "SELECT has_column_privilege('platform_api','submissions',"
                    "'intake_mode','INSERT')"
                )
            )
    finally:
        command.upgrade(cfg, "head")
        with store.engine.begin() as c:
            c.exec_driver_sql("DROP OWNED BY platform_api; DROP ROLE platform_api")


def test_coordinator_can_verify_and_schedule_dev_upload(store, config):
    import dataclasses

    from db.evaluations import EvaluationsDb

    # A development deployment opts in; production never plans dev uploads.
    config = dataclasses.replace(config, dev_intake=True)

    repo = EvaluationsDb(store.sessions)
    repo.set_leader("test-dev-intake")
    repo.reconcile(config)
    with store.engine.begin() as c:
        sha = source(c)
        sid = c.scalar(
            text(
                "INSERT INTO submissions(hotkey,source_sha256,intake_mode) "
                "VALUES ('dev',:sha,'dev') RETURNING id"
            ),
            dict(sha=sha),
        )
    claimed = repo.submissions.claim_proof("test", config.proof_identity, 120)
    assert claimed.id == sid and claimed.registration_id is None
    assert repo.submissions.finish_proof(
        sid,
        claimed.token,
        config.proof_identity,
        {"exit_code": 0, "stage": "axioms", "passed": True},
        1,
    )
    assert len(repo.plan(config)) == len(config.boards)
    with store.engine.connect() as c:
        assert c.scalar(text("SELECT count(*) FROM benchmark_jobs WHERE state='queued'")) == len(
            config.boards
        )
