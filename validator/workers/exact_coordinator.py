"""Optional coordinator: proof checks, plans, result aggregation and compact reward publication."""

from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import fcntl
import hashlib
import json
import os
import socket
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

from loguru import logger
from sqlalchemy import text

import db
import fingerprint
from db.exact import ExactDb
from exact.config import Config
from exact.process import ROOT, BudgetExceeded, install_signals, supervised

AUDIT_SOURCES = ("lean/audit/ExactAuditCore.lean", "lean/audit/ExactAudit.lean")


def audit_identity(config):
    """The audit version: its sources and the proof identity; a change re-audits every champion."""
    sources = {
        name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in AUDIT_SOURCES
    }
    return hashlib.sha256(
        json.dumps({"sources": sources, "proof": config.proof_identity}, sort_keys=True).encode()
    ).hexdigest()


@contextlib.contextmanager
def checkout(heartbeat, wait):
    """The build checkout's lock, waiting while `heartbeat` keeps the claim alive."""
    with open(ROOT / "gate.lock", "a") as lock:
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if not heartbeat():
                    raise RuntimeError("lease lost while waiting for the build workspace")
                time.sleep(wait)
        yield lock


def run_audit(repo, task, config, stop):
    """Audit one champion's source in the sandbox (`gate.py --audit`) and record the outcome."""
    with tempfile.TemporaryDirectory(prefix="exact-audit-") as tmp:
        path = Path(tmp)
        try:
            if task.content is None or hashlib.sha256(task.content).hexdigest() != task.source_hash:
                raise RuntimeError("champion source missing or hash mismatch")
            (path / "Kernel.lean").write_bytes(task.content)
            verdict_path = path / "verdict.json"
            with checkout(lambda: not stop.is_set(), 1) as lock:
                code = supervised(
                    [
                        sys.executable,
                        str(ROOT / "gate.py"),
                        str(path),
                        "--slot",
                        "exact",
                        "--audit",
                        "--verdict",
                        str(verdict_path),
                    ],
                    lambda: not stop.is_set(),
                    config.audit["seconds"],
                    config.lease_seconds / 3,
                    path / "audit.log",
                    env_extra={
                        "GATE_LOCK_HELD": "1",
                        "GATE_AUDIT_CASES": str(task.cases),
                        "GATE_AUDIT_SEED": str(task.seed),
                        "GATE_BUDGET_SECONDS": str(config.audit["seconds"]),
                    },
                    pass_fds=(lock.fileno(),),
                    cancelled=stop.is_set,
                )
            verdict = json.loads(verdict_path.read_text()) if verdict_path.exists() else {}
            report = {
                "reason": verdict.get("reason", ""),
                **verdict.get("classes", {}).get("audit", {}),
            }
            # Only the audit's own finding fails a champion; anything else is retried.
            state = (
                "clean"
                if code == 0
                else "failed"
                if code == 1 and verdict.get("stage") == "audit"
                else "error"
            )
        except Exception as exc:
            state, report = "error", {"reason": str(exc)}
        if state == "failed":
            logger.error(
                "exact champion audit FAILED for {}: {}", task.source_hash[:12], report["reason"]
            )
        if not stop.is_set():
            repo.finish_audit(task.id, state, report, config)


def verify(repo, sub, config, stop):
    with tempfile.TemporaryDirectory(prefix="exact-proof-") as tmp:
        path = Path(tmp)
        try:
            with db.session_scope(repo.sessions) as session:
                source = session.get(db.models.SubmissionFile, (sub.id, "Kernel.lean"))
                content = source.content if source else None
            if content is None or hashlib.sha256(content).hexdigest() != sub.digest:
                raise RuntimeError("submission source missing or hash mismatch")
            (path / "Kernel.lean").write_bytes(content)
            verdict_path = path / "verdict.json"

            def heartbeat():
                return not stop.is_set() and repo.heartbeat_proof(
                    sub.id, sub.verification_attempt, config.lease_seconds
                )

            with checkout(heartbeat, min(config.lease_seconds / 3, 1)) as checkout_lock:
                code = supervised(
                    [
                        sys.executable,
                        str(ROOT / "gate.py"),
                        str(path),
                        "--slot",
                        "exact",
                        "--proof-only",
                        "--verdict",
                        str(verdict_path),
                    ],
                    lambda: (
                        not stop.is_set()
                        and repo.heartbeat_proof(
                            sub.id, sub.verification_attempt, config.lease_seconds
                        )
                    ),
                    config.verification_seconds,
                    config.lease_seconds / 3,
                    path / "proof.log",
                    env_extra={
                        "GATE_LOCK_HELD": "1",
                        "GATE_BUDGET_SECONDS": str(config.verification_seconds),
                    },
                    pass_fds=(checkout_lock.fileno(),),
                    cancelled=stop.is_set,
                )
            verdict = (
                json.loads(verdict_path.read_text())
                if verdict_path.exists()
                else {"reason": "proof process produced no verdict"}
            )
            if code == 0 and (
                not verdict.get("passed")
                or verdict.get("verifier_fingerprint") != config.proof_identity
                or verdict.get("verifier_fingerprint_end") != config.proof_identity
            ):
                code = 2
                verdict["reason"] = "proof identity changed or successful proof evidence missing"
            verdict["exit_code"] = code if code in (0, 1) else 2
        except BudgetExceeded as exc:
            # A proof check past its budget rejects the entry, as the gate worker always did;
            # requeueing it would block every board behind it.
            verdict = {"exit_code": 1, "stage": "timeout", "reason": f"proof check {exc}"}
        except Exception as exc:
            verdict = {"exit_code": 2, "stage": "infrastructure", "reason": str(exc)}
        if not stop.is_set():
            repo.finish_proof(
                sub.id,
                sub.verification_attempt,
                config.proof_identity,
                verdict,
                config.retry_seconds,
            )


def main():
    install_signals()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=os.environ.get("EXACT_COMPETITION_CONFIG"))
    parser.add_argument(
        "--once",
        action="store_true",
        help="reconcile, plan and aggregate once; no proof subprocess",
    )
    args = parser.parse_args()
    if args.config is None:
        parser.error("set EXACT_COMPETITION_CONFIG or --config")
    config = Config.load(args.config)
    if (
        config.proof_identity != fingerprint.current()
        or config.benchmark_identity != fingerprint.current()
    ):
        parser.error("configuration identities must match this pinned checkout")
    for board in config.boards.values():
        if (
            hashlib.sha256(Path(board["reference"]["path"]).read_bytes()).hexdigest()
            != board["reference"]["sha256"]
        ):
            parser.error("initial reference hash mismatch")
    store = db.connect()
    repo = ExactDb(store.sessions)
    stop = threading.Event()
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = None
    owner = f"{socket.gethostname()}:{os.getpid()}"
    identity = audit_identity(config)
    try:
        with store.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as leader:
            if not leader.scalar(
                text("SELECT pg_try_advisory_lock(hashtextextended('exact:coordinator',0))")
            ):
                raise RuntimeError("another exact coordinator owns leadership")
            try:
                repo.set_leader(uuid.uuid4().hex)
                repo.reconcile(config)
                next_alert = 0.0
                while True:
                    leader.execute(text("SELECT 1"))
                    if fingerprint.current() != config.proof_identity:
                        raise RuntimeError(
                            "pinned checkout changed; restart with reviewed configuration"
                        )
                    repo.plan(config)
                    repo.aggregate(config)
                    repo.publish(config)
                    repo.ensure_audits(config, identity)
                    if args.once:
                        break
                    if future is not None and future.done():
                        future.result()
                        future = None
                    if future is None:
                        sub = repo.claim_proof(owner, config.proof_identity, config.lease_seconds)
                        if sub:
                            future = pool.submit(verify, repo, sub, config, stop)
                        elif task := repo.claim_audit():
                            # Proof checks come first; a champion audit runs when none waits.
                            future = pool.submit(run_audit, repo, task, config, stop)
                    if time.monotonic() >= next_alert:
                        for alert in repo.alerts(config.alert_seconds):
                            logger.warning("exact benchmark waiting: {}", alert)
                        next_alert = time.monotonic() + min(config.alert_seconds, 60)
                    time.sleep(2)
            finally:
                stop.set()
                if not leader.invalidated:
                    leader.execute(
                        text("SELECT pg_advisory_unlock(hashtextextended('exact:coordinator',0))")
                    )
    finally:
        stop.set()
        pool.shutdown(wait=True, cancel_futures=True)
        store.close()


if __name__ == "__main__":
    main()
