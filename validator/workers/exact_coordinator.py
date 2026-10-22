"""Optional coordinator: proof checks, plans, result aggregation and compact reward publication."""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import socket
import subprocess
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
from exact.process import ROOT, BudgetExceeded, checkout, install_signals, supervised
from exact.provenance import specification_identity


def source_environment():
    # Audit provenance is useful even in packaged deployments without Git metadata.
    try:
        commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True, stderr=subprocess.DEVNULL
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        commit = None
    return {
        "commit": commit,
        "specification_identity": specification_identity(),
        "toolchain": (ROOT / "lean/lean-toolchain").read_text().strip(),
        "manifest_sha256": hashlib.sha256(
            (ROOT / "lean/lake-manifest.json").read_bytes()
        ).hexdigest(),
        "verifier_settings": {
            key: os.environ.get(key, default) for key, default in fingerprint.KNOBS.items()
        },
    }


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
            if code == 0:
                # Immutable references for a remote auditor to select the matching release.
                verdict["source_environment"] = source_environment()
                if fingerprint.current() != config.proof_identity:
                    code = 2
                    verdict["reason"] = (
                        "verification environment changed while recording provenance"
                    )
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
                    if args.once:
                        break
                    if future is not None and future.done():
                        future.result()
                        future = None
                    if future is None:
                        sub = repo.claim_proof(owner, config.proof_identity, config.lease_seconds)
                        if sub:
                            future = pool.submit(verify, repo, sub, config, stop)
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
