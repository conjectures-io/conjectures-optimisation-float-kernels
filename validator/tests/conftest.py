"""A scratch Postgres database per test session and its fixtures; db tests skip without a server."""

from __future__ import annotations

import os
import sys
import uuid
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TypedDict

import pytest
import sqlalchemy as sa

VALIDATOR = Path(__file__).resolve().parent.parent
REPO = VALIDATOR.parent
if str(VALIDATOR) not in sys.path:
    sys.path.insert(0, str(VALIDATOR))

import db as store_pkg  # noqa: E402
from db import models  # noqa: E402

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
        models.Base.metadata.create_all(engine)
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
    s = store_pkg.connect(database_url, rate_limit=10, rate_window_seconds=60)
    with s.engine.begin() as conn:
        names = [t.name for t in reversed(models.Base.metadata.sorted_tables)]
        tables = ", ".join(f'"{n}"' for n in names)
        conn.execute(sa.text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    try:
        yield s
    finally:
        s.close()


def register(store, hotkey: str, *, uid: int = 0, block: int = 1000) -> int:
    # Insert one registration row (one submission slot), as the chain watcher would. Returns its id.
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


# ── The service under test ────────────────────────────────────────────────

# Stands in for gate.py (same args, verdict JSON, exit codes); markers pick the outcome.
STUB_GATE = """
import json, os, sys
from pathlib import Path
kernel = (Path(sys.argv[1]) / "Kernel.lean").read_text()
verdict = Path(sys.argv[sys.argv.index("--verdict") + 1])
if "MISCONFIGURED" in kernel:
    print("pin failed"); sys.exit(2)
base = {"submission": Path(sys.argv[1]).name, "mode": "gate", "seed": 7,
        "incumbent_sha256": "a" * 64, "seconds": 1.5, "classes": {},
        "verifier_fingerprint": "e" * 64 if "STALE" in kernel
        else os.environ.get("GATE_REQUIRED_FINGERPRINT", "f" * 64)}
if "CRASH" in kernel:
    verdict.write_text(json.dumps({**base, "stage": "policy", "passed": False,
                                   "reason": "gate error: boom", "ratio": None}))
    sys.exit(2)
if "REJECT" in kernel:
    verdict.write_text(json.dumps({**base, "stage": "statement", "passed": False,
                                   "reason": "the obligation does not typecheck", "ratio": None}))
    sys.exit(1)
ratio = float(kernel.split("RATIO=")[1].split()[0]) if "RATIO=" in kernel else 0.9
classes = {"near-one": {"incumbent_ns": 30.0, "submission_ns": 30.0 * ratio, "ratio": ratio}}
verdict.write_text(json.dumps({**base, "stage": "score", "passed": True, "reason": "",
                               "ratio": ratio, "classes": classes}))
sys.exit(0)
"""


@pytest.fixture
def settings(tmp_path, monkeypatch):
    # Files under the test's own directory; everything else at its default.
    from service.settings import Settings

    return Settings(files=tmp_path / "submissions", worker_id="test-worker")


@pytest.fixture
def stub_gate(tmp_path, monkeypatch):
    # Point the worker at the stub instead of the real, toolchain-backed gate.py.
    from service import worker as worker_mod

    path = tmp_path / "gate.py"
    path.write_text(STUB_GATE)
    monkeypatch.setattr(worker_mod, "GATE", path)
    return path


@pytest.fixture
def client(store, settings, stub_gate):
    # The API against the test store, with the stub gate behind its worker.
    from fastapi.testclient import TestClient
    from service.api import create_app

    with TestClient(app=create_app(settings, store)) as c:
        yield c


@pytest.fixture
def drain(store, settings, stub_gate):
    # Run the gate worker over the queue once, as the standalone process would.
    from service import worker as worker_mod

    def run() -> int:
        return worker_mod.drain(store, settings)

    return run


def kernel_file(tmp: Path, name: str, marker: str = "") -> Path:
    # A copy of the incumbent's Kernel.lean, with a marker line the stub gate reads.
    d = tmp / name
    d.mkdir(parents=True)
    text = (REPO / "submissions" / "incumbent" / "Kernel.lean").read_text()
    (d / "Kernel.lean").write_text(text + (f"\n-- {marker}\n" if marker else ""))
    return d


def post_submission(client, kp, d: Path, *, signer=None, timestamp: int | None = None, data=None):
    # Sign and post a submission; security tests bend `signer`, `timestamp` and `data`.
    from service import sig

    lean = (d / "Kernel.lean").read_bytes()
    stamp = int(datetime.now(timezone.utc).timestamp()) if timestamp is None else timestamp
    digest = sig.digest_of(data if data is not None else lean)
    return client.post(
        "/submit",
        data={
            "hotkey": kp.ss58_address,
            "signature": sig.sign(signer or kp, sig.submit_message(digest, kp.ss58_address, stamp)),
            "timestamp": stamp,
        },
        files={"Kernel.lean": ("Kernel.lean", lean)},
    )
