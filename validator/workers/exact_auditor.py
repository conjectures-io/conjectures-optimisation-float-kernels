"""Independent, report-only champion auditor. Run on a separate host and pinned checkout."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import socket
import sys
import threading
import time
from pathlib import Path

from loguru import logger
from process import ROOT, checkout, install_signals, supervised
from provenance import specification_identity

import db
import fingerprint
from db.audits import AuditDb

AUDIT_SOURCES = (
    "lean/audit/ExactAuditCore.lean",
    "lean/audit/ExactAudit.lean",
    "validator/workers/exact_auditor.py",
    "validator/db/audits.py",
    "validator/process.py",
    "validator/provenance.py",
    "validator/audit.py",
    "validator/sandbox.py",
)


def audit_profile(cases):
    if type(cases) is not int or not 1 <= cases <= 1000:
        raise ValueError("audit cases must be an integer from 1 to 1000")
    return {
        "protocol": "independent-audit-v1",
        "cases": cases,
        "sources": {
            name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in AUDIT_SOURCES
        },
    }


def classify(code, verdict, task, execution_identity=None):
    """Only complete, matching evidence can pass or report a mismatch."""
    expected = execution_identity or task.request["proof_identity"]
    data = verdict.get("classes", {}).get("audit", {})
    valid = (
        verdict.get("mode") == "audit"
        and verdict.get("stage") == "audit"
        and verdict.get("verifier_fingerprint") == expected
        and verdict.get("verifier_fingerprint_end") == expected
        and type(data.get("cases")) is int
        and data["cases"] > 0
        and type(data.get("mismatches")) is int
        and 0 <= data["mismatches"] <= data["cases"]
        and data.get("seed") == task.seed
    )
    if valid and code == 0 and verdict.get("passed") is True and data["mismatches"] == 0:
        return "clean"
    if valid and code == 1 and verdict.get("passed") is False and data["mismatches"] > 0:
        return "failed"
    return "error"


def run_audit(repo, task, settings, stop):
    directory = settings.output / f"{task.id}-{task.token}"
    directory.mkdir(parents=True, exist_ok=False)
    report = {
        "host": platform.uname()._asdict(),
        "owner": task.owner,
        "request": task.request,
        "source_hash": task.source_hash,
        "seed": task.seed,
        "cases_per_kind": task.cases,
        "artifacts": str(directory.resolve()),
    }
    started = time.monotonic()
    execution_identity = fingerprint.current()
    expected_spec = task.request.get("specification_identity")
    report["audit_verifier_identity"] = execution_identity

    def heartbeat():
        return (
            not stop.is_set()
            and fingerprint.current() == execution_identity
            and (
                specification_identity() == expected_spec
                if expected_spec
                else execution_identity == task.request["proof_identity"]
            )
            and audit_profile(task.cases) == task.request["profile"]
            and repo.heartbeat(task, settings.lease)
        )

    try:
        if task.content is None or hashlib.sha256(task.content).hexdigest() != task.source_hash:
            raise RuntimeError("promoted source missing or hash mismatch")
        if task.request["profile"] != audit_profile(task.cases):
            raise RuntimeError("audit implementation or coverage differs from the task")
        if not heartbeat():
            raise RuntimeError("lease lost or pinned verification environment differs from task")
        (directory / "Kernel.lean").write_bytes(task.content)
        (directory / "request.json").write_text(json.dumps(task.request, indent=2))
        report["toolchain"] = (ROOT / "lean/lean-toolchain").read_text().strip()
        report["manifest_sha256"] = hashlib.sha256(
            (ROOT / "lean/lake-manifest.json").read_bytes()
        ).hexdigest()
        verdict_path = directory / "verdict.json"
        with checkout(heartbeat, min(settings.lease / 3, 1), settings.seconds) as lock:
            remaining = settings.seconds - (time.monotonic() - started)
            if remaining <= 0:
                raise TimeoutError("audit time budget exhausted waiting for checkout")
            code = supervised(
                [
                    sys.executable,
                    "-m",
                    "audit",
                    str(directory.resolve()),
                    "--verdict",
                    str(verdict_path.resolve()),
                ],
                heartbeat,
                remaining,
                settings.lease / 3,
                directory / "audit.log",
                env_extra={
                    "GATE_LOCK_HELD": "1",
                    "GATE_AUDIT_CASES": str(task.cases),
                    "GATE_AUDIT_SEED": str(task.seed),
                    "GATE_BUDGET_SECONDS": str(remaining),
                },
                pass_fds=(lock.fileno(),),
                cancelled=stop.is_set,
            )
        verdict = json.loads(verdict_path.read_text()) if verdict_path.exists() else {}
        report["verdict"] = verdict
        report["reason"] = verdict.get("reason", "missing or incompatible audit evidence")
        state = classify(code, verdict, task, execution_identity)
        if not heartbeat():
            raise RuntimeError("lease or pinned audit environment changed during execution")
    except Exception as exc:
        state = "error"
        report["reason"] = str(exc)
    report["seconds"] = time.monotonic() - started
    report["state"] = state
    (directory / "report.json").write_text(json.dumps(report, indent=2))
    if not stop.is_set() and repo.finish(task, state, report, settings.retry):
        if state == "clean":
            logger.info("champion audit clean: {}", task.source_hash)
        else:
            logger.error("champion audit {}: {}: {}", state, task.source_hash, report["reason"])


def main():
    from worker_credentials import load

    install_signals()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--credentials", type=Path)
    parser.add_argument("--cases", type=int, default=50)
    parser.add_argument("--seconds", type=float, default=900)
    parser.add_argument("--lease", type=float, default=120)
    parser.add_argument("--retry", type=float, default=300)
    parser.add_argument("--poll", type=float, default=10)
    parser.add_argument("--alert-seconds", type=float, default=900)
    parser.add_argument("--output", type=Path, default=ROOT / "results/champion-audits")
    parser.add_argument(
        "--once", action="store_true", help="discover one batch and run at most one task"
    )
    parser.add_argument(
        "--discover-only", action="store_true", help="discover one batch without building"
    )
    args = parser.parse_args()
    load(args.credentials)
    try:
        profile = audit_profile(args.cases)
    except ValueError as exc:
        parser.error(str(exc))
    for value in (args.seconds, args.lease, args.retry, args.poll, args.alert_seconds):
        if not math.isfinite(value) or value <= 0:
            parser.error("budgets and intervals must be finite and positive")
    identity = fingerprint.current()
    spec_identity = specification_identity()
    store = db.connect()
    repo = AuditDb(store.sessions)
    stop = threading.Event()
    owner = f"{socket.gethostname()}:{os.getpid()}"
    try:
        while True:
            if audit_profile(args.cases) != profile or fingerprint.current() != identity:
                raise RuntimeError("pinned checkout changed; restart the audit worker")
            repo.discover(profile)
            if args.discover_only:
                break
            if task := repo.claim(profile, identity, owner, args.lease, spec_identity):
                run_audit(repo, task, args, stop)
            for alert in repo.alerts(profile, args.alert_seconds):
                logger.warning(
                    "champion audit waiting (possibly another release required): {}", alert
                )
            if args.once:
                break
            time.sleep(args.poll)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        store.close()


if __name__ == "__main__":
    main()
