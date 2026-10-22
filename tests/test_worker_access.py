"""Worker authorization through real database logins, not SET ROLE."""

import json
import os
import stat
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError, NoResultFound
from test_exact_competition import result
from test_tensor_workflow import accepted, setup
from tools.worker_access import provision, validate_login, write_credentials
from worker_credentials import load

import db
from db.jobs import WorkerDb


@pytest.fixture
def logins(store):
    engines = []
    names = []

    def create(kind="benchmark", environments=("local",)):
        name = "worker_test_" + uuid.uuid4().hex[:12]
        names.append(name)
        password = "a:@/ ?'\\" + uuid.uuid4().hex
        with store.engine.begin() as conn:
            provision(conn, "create", name, kind=kind, environments=environments, password=password)
        engine = create_engine(store.engine.url.set(username=name, password=password))
        engines.append(engine)
        return name, engine, WorkerDb(db.session_factory(engine))

    yield create
    for engine in engines:
        engine.dispose()
    with store.engine.begin() as conn:
        for name in names:
            conn.exec_driver_sql(f'DROP OWNED BY "{name}"')
            conn.exec_driver_sql(f'DROP ROLE "{name}"')


def test_claim_bound_to_authenticated_login(store, config, logins):
    repo, _ = setup(store, config)
    accepted(store, config)
    repo.plan(config)
    name, engine, first = logins()
    _, _, second = logins()
    _, _, wrong = logins(environments=("other",))
    with pytest.raises(DBAPIError):
        wrong.claim("local", "a" * 64, "spoof")
    assert first.claim("local", "b" * 64, "same-owner") is None
    job = first.claim("local", "a" * 64, "same-owner")
    assert job.claimant == name
    assert first.access()["environments"] == ["local"]
    assert first.sources(job.id)[0] == b"candidate"
    # Knowing another process's token is insufficient with different DB credentials.
    second.claims[job.id] = (job.owner, job.token)
    second.requests[job.id] = job.request
    with pytest.raises(NoResultFound):
        second.sources(job.id)
    assert not second.heartbeat(job.id, job.owner, job.token)
    assert not second.complete(job.id, job.owner, job.token, result(job.request))
    assert not second.fail(job.id, job.owner, job.token, {"kind": "candidate", "reason": "spoof"})
    for statement in (
        "UPDATE benchmark_jobs SET state='cancelled'",
        "UPDATE worker_logins SET enabled=true",
        "INSERT INTO worker_environments VALUES('spoof','local')",
        "DELETE FROM promotions",
        "CREATE TABLE public.worker_escape(id int)",
    ):
        with pytest.raises(DBAPIError), engine.begin() as conn:
            conn.execute(text(statement))
    with store.engine.begin() as conn:
        provision(conn, "environments", name, environments=("other",))
    assert not first.heartbeat(job.id, job.owner, job.token)
    with pytest.raises(NoResultFound):
        first.sources(job.id)
    with store.engine.begin() as conn:
        provision(conn, "environments", name, environments=("local",))
    assert first.complete(job.id, job.owner, job.token, result(job.request))
    with pytest.raises(NoResultFound):
        first.sources(job.id)


def test_rotation_revocation_and_auditor_permissions(store, logins):
    name, engine, worker = logins()
    assert worker.access()["enabled"]
    with store.engine.begin() as conn:
        provision(conn, "rotate", name, password="replacement")
    with pytest.raises(DBAPIError), create_engine(engine.url).connect():
        pass
    assert worker.access()["enabled"]  # Existing sessions survive a password change.
    with store.engine.begin() as conn:
        provision(conn, "revoke", name)
    with pytest.raises(DBAPIError):
        worker.access()
    with pytest.raises(DBAPIError):
        worker.claim("local", "a" * 64, "worker")
    _, audit_engine, auditor = logins(kind="auditor", environments=())
    assert auditor.access()["kind"] == "auditor"
    with audit_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO audit_cursors(profile_identity,promotion_id) VALUES('test',0)")
        )
        conn.execute(text("SELECT * FROM sources"))
    with pytest.raises(DBAPIError), audit_engine.begin() as conn:
        conn.execute(text("UPDATE submissions SET state='proved'"))
    with pytest.raises(DBAPIError):
        auditor.claim("local", "a" * 64, "audit")


def test_credentials_protected_and_no_overwrite(tmp_path, monkeypatch):
    path = tmp_path / "worker.json"
    values = {"DATABASE_URL": "postgresql+psycopg://test:secret@localhost/test"}
    write_credentials(path, values)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    monkeypatch.setattr(os, "environ", dict(os.environ))
    os.environ["POSTGRES_PASSWORD"] = "administrator-secret"
    os.environ["WORKER_ADMIN_DATABASE_URL"] = "administrator-dsn"
    load(path)
    assert "POSTGRES_PASSWORD" not in os.environ
    assert "WORKER_ADMIN_DATABASE_URL" not in os.environ
    with pytest.raises(FileExistsError):
        write_credentials(path, {"DATABASE_URL": "changed"})
    assert json.loads(path.read_text()) == values
    path.chmod(0o644)
    with pytest.raises(ValueError, match="0600"):
        load(path)


@pytest.mark.parametrize("name", ["pg_admin", "tensor_admin", "a;DROP ROLE x", "A", "x" * 64])
def test_refuse_unsafe_login_names(name):
    with pytest.raises(ValueError):
        validate_login(name)


def test_provisioning_cli_create_rotate_revoke(store, tmp_path):
    name = "worker_cli_" + uuid.uuid4().hex[:12]
    root = Path(__file__).resolve().parents[1]
    env = {
        **os.environ,
        "WORKER_ADMIN_DATABASE_URL": store.engine.url.render_as_string(hide_password=False),
        "PYTHONPATH": str(root / "validator"),
    }

    def run(*args):
        return subprocess.run(
            [sys.executable, "-m", "tools.worker_access", *args],
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
        )

    path = tmp_path / "first.json"
    second = tmp_path / "second.json"
    try:
        created = run(
            "create",
            "--login",
            name,
            "--kind",
            "benchmark",
            "--environment",
            "local",
            "--output",
            str(path),
        )
        assert created.returncode == 0, created.stdout
        assert "password" not in created.stdout.lower()
        credentials = json.loads(path.read_text())
        engine = create_engine(credentials["DATABASE_URL"])
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT session_user")) == name
            rotated = run("rotate", "--login", name, "--output", str(second))
            assert rotated.returncode == 0, rotated.stdout
            with pytest.raises(DBAPIError):
                connection.execute(text("SELECT 1"))
        engine.dispose()
        replacement = create_engine(json.loads(second.read_text())["DATABASE_URL"])
        with replacement.connect() as connection:
            assert connection.scalar(text("SELECT session_user")) == name
            assert run("revoke", "--login", name).returncode == 0
            with pytest.raises(DBAPIError):
                connection.execute(text("SELECT 1"))
        replacement.dispose()
        assert (
            run(
                "create",
                "--login",
                name,
                "--kind",
                "benchmark",
                "--environment",
                "local",
                "--output",
                str(path),
            ).returncode
            != 0
        )
        listing = run("list")
        assert listing.returncode == 0 and name in listing.stdout
        assert credentials["DATABASE_URL"] not in listing.stdout
    finally:
        with store.engine.begin() as conn:
            conn.exec_driver_sql(f'DROP OWNED BY "{name}"')
            conn.exec_driver_sql(f'DROP ROLE IF EXISTS "{name}"')


def test_preflight_reads_only_and_checks_environment(store, config, logins, monkeypatch, capsys):
    from tools import worker_preflight

    repo, _ = setup(store, config)
    accepted(store, config)
    repo.plan(config)
    _, engine, _ = logins()
    monkeypatch.setattr(worker_preflight.db, "connect", lambda: db.Store(engine))
    monkeypatch.setattr(worker_preflight, "local_checks", lambda: None)
    monkeypatch.setattr(worker_preflight, "machine_identity", lambda: "a" * 64)
    monkeypatch.setattr(
        sys, "argv", ["preflight", "--environment", "local", "--expected-identity", "a" * 64]
    )
    worker_preflight.main()
    assert json.loads(capsys.readouterr().out)["access"]["protocol"] == 2
    with store.engine.connect() as conn:
        assert conn.scalar(text("SELECT sum(attempts) FROM benchmark_jobs")) == 0
    monkeypatch.setattr(sys, "argv", ["preflight", "--environment", "other"])
    with pytest.raises(RuntimeError, match="assigned"):
        worker_preflight.main()
    monkeypatch.setattr(
        sys, "argv", ["preflight", "--environment", "local", "--expected-identity", "b" * 64]
    )
    with pytest.raises(RuntimeError, match="identity"):
        worker_preflight.main()


def test_expired_claim_reassigned_to_another_login(store, config, logins):
    repo, _ = setup(store, config)
    accepted(store, config)
    repo.plan(config)
    _, _, first = logins()
    second_name, _, second = logins()
    job = first.claim("local", "a" * 64, "first")
    with store.engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE benchmark_jobs SET lease_until=clock_timestamp()-interval '1 second' "
                "WHERE id=:id"
            ),
            {"id": job.id},
        )
    # Any already-ready jobs retain priority over the expired one.
    while ready := second.claim("local", "a" * 64, "second"):
        assert ready.id != job.id
        assert second.complete(ready.id, ready.owner, ready.token, result(ready.request))
    with store.engine.begin() as conn:
        conn.execute(
            text("UPDATE benchmark_jobs SET due_at=clock_timestamp() WHERE id=:id"), {"id": job.id}
        )
    reclaimed = second.claim("local", "a" * 64, "second")
    assert reclaimed.id == job.id and reclaimed.claimant == second_name
    assert not first.heartbeat(job.id, job.owner, job.token)
    with pytest.raises(NoResultFound):
        first.sources(job.id)
    assert second.sources(reclaimed.id)[0] == b"candidate"
    assert second.complete(
        reclaimed.id, reclaimed.owner, reclaimed.token, result(reclaimed.request)
    )


def test_pm2_restricted_workers_do_not_inherit_admin_credentials(tmp_path):
    import shutil

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    root = Path(__file__).resolve().parents[1]
    deploy = tmp_path / "deploy"
    deploy.mkdir()
    shutil.copy2(root / "deploy/pm2.config.js", deploy / "pm2.config.js")
    (tmp_path / ".env").write_text(
        "POSTGRES_PASSWORD=admin-secret\nDATABASE_URL=admin-url\n"
        "TENSOR_BENCHMARK_CREDENTIALS=/secure/bench.json\n"
        "TENSOR_AUDITOR_ENABLED=1\nTENSOR_AUDITOR_CREDENTIALS=/secure/audit.json\n"
    )
    script = """
const apps = require(process.argv[1]).apps;
for (const app of apps.filter(x => x.name.includes('benchmark') || x.name.includes('auditor'))) {
  if (app.env.POSTGRES_PASSWORD || app.env.DATABASE_URL || app.env.WORKER_ADMIN_DATABASE_URL)
    process.exit(1);
}
"""
    env = dict(os.environ)
    env["WORKER_ADMIN_DATABASE_URL"] = "provisioner-secret"
    checked = subprocess.run([node, "-e", script, str(deploy / "pm2.config.js")], env=env)
    assert checked.returncode == 0
