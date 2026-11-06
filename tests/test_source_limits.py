"""The fixed source ceiling is shared; public upload policy belongs to the API."""

import hashlib
import os
import subprocess
import sys

import pytest
import sandbox
from alembic import command
from source_limits import MAX_SOURCE_BYTES
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from test_db_schema import migration_config
from test_miner_sig import REPO, load

import fingerprint
from db.submissions import put_source


def test_cli_and_gate_share_the_fixed_ceiling(tmp_path, monkeypatch):
    submit = load("miner_source_ceiling", REPO / "miner/submit.py")
    assert submit.MAX_FILE_BYTES == sandbox._MAX_BYTES == MAX_SOURCE_BYTES == 4 * 1024 * 1024
    # Lowering public admission must not invalidate an existing, larger champion.
    monkeypatch.setenv("TORCHLEAN_MAX_SOURCE_BYTES", "1024")
    content = b"-" * MAX_SOURCE_BYTES
    src = tmp_path / "Kernel.lean"
    src.write_bytes(content)
    assert submit.kernel_of(tmp_path) == content
    assert sandbox.intake(src, sandbox.Verdict(submission="large")) is None


def test_api_policy_does_not_change_execution_identities(monkeypatch):
    assert "validator/source_limits.py" in fingerprint.PROOF_FILES
    assert "validator/source_limits.py" in fingerprint.BENCHMARK_FILES
    proof, benchmark = fingerprint.proof(), fingerprint.benchmark()
    monkeypatch.setenv("TORCHLEAN_MAX_SOURCE_BYTES", "2097152")
    assert (fingerprint.proof(), fingerprint.benchmark()) == (proof, benchmark)


@pytest.mark.parametrize(
    "kind",
    ["long_word", "unclosed_nested_comment", "many_local_declarations", "unterminated_string"],
)
def test_policy_scan_is_bounded_for_large_sources(tmp_path, kind):
    src = tmp_path / "Kernel.lean"
    if kind == "long_word":
        content = b"-- " + b"x" * (MAX_SOURCE_BYTES - 3)
    elif kind == "unclosed_nested_comment":
        content = b"/-" * (MAX_SOURCE_BYTES // 2)
    elif kind == "unterminated_string":
        content = b'"' + b'\\"' * ((MAX_SOURCE_BYTES - 1) // 2)
    else:
        declaration = b'local notation "value" => Nat\n'
        content = declaration * (MAX_SOURCE_BYTES // len(declaration))
    src.write_bytes(content)
    # Run in a child so a regression cannot hang the entire test session.
    subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from pathlib import Path; from sandbox import policy, Verdict; "
            "assert policy(Path(sys.argv[1]), Verdict(submission='large')) is None",
            str(src),
        ],
        cwd=REPO,
        env={**os.environ, "PYTHONPATH": str(REPO / "validator")},
        timeout=15,
        check=True,
        capture_output=True,
    )


@pytest.mark.parametrize(
    "name", ["foosimprocbar", "foo_elab", "foo_syntax_bar", "αsimprocβ", "«foo_elab»"]
)
def test_linear_identifier_scan_still_rejects_embedded_forbidden_names(name):
    assert sandbox.BANNED.search(name)


@pytest.mark.parametrize("size", [1024 * 1024, MAX_SOURCE_BYTES])
def test_store_accepts_large_sources_at_supported_boundaries(store, size):
    content = b"-" * size
    with store.sessions.begin() as session:
        digest = put_source(session, content)
    with store.engine.connect() as c:
        assert (
            c.scalar(
                text("SELECT octet_length(content) FROM sources WHERE sha256=:h"), {"h": digest}
            )
            == size
        )


def test_both_python_and_database_reject_above_the_ceiling(store):
    content = b"-" * (MAX_SOURCE_BYTES + 1)
    with store.sessions.begin() as session:
        with pytest.raises(ValueError, match=str(MAX_SOURCE_BYTES)):
            put_source(session, content)
    with pytest.raises(IntegrityError, match="ck_sources_size"):
        with store.engine.begin() as c:
            c.execute(
                text("INSERT INTO sources(sha256,content) VALUES(:h,:v)"),
                {"h": hashlib.sha256(content).hexdigest(), "v": content},
            )


def test_source_size_downgrade_refuses_data_loss(store, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", store.engine.url.render_as_string(hide_password=False))
    cfg = migration_config()
    content = b"-" * (1024 * 1024)
    with store.sessions.begin() as session:
        digest = put_source(session, content)
    try:
        with pytest.raises(DBAPIError, match="Cannot downgrade: sources larger than 256000"):
            command.downgrade(cfg, "tensor_0003")
        with store.engine.connect() as c:
            assert c.scalar(text("SELECT version_num FROM alembic_version")) == "tensor_0007"
            assert (
                c.scalar(text("SELECT content FROM sources WHERE sha256=:h"), {"h": digest})
                == content
            )
        with store.engine.begin() as c:
            c.execute(text("DELETE FROM sources WHERE sha256=:h"), {"h": digest})
        command.downgrade(cfg, "tensor_0003")
        with pytest.raises(IntegrityError, match="ck_sources_size"):
            with store.engine.begin() as c:
                c.execute(
                    text("INSERT INTO sources(sha256,content) VALUES(:h,:v)"),
                    {"h": digest, "v": content},
                )
    finally:
        command.upgrade(cfg, "head")
