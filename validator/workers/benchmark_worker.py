"""One shared hardware worker implementation; run with PYTHONPATH=.:validator."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import socket
import sys
import tempfile
import time
from pathlib import Path

from benchmark import machine_identity, preparation_mode
from loguru import logger
from process import ROOT, BudgetExceeded, install_signals, supervised
from timing import supervisor_seconds
from worker_credentials import load

import db
from axiom import get_events, observe_worker
from db.jobs import WorkerDb


def timed_out(error):
    # A candidate that ran out of time is a measured result: too slow, not broken.
    return error.get("kind") == "candidate" and isinstance(error.get("timeout"), dict)


def report(job, error, seconds):
    # A candidate failure is the miner's result; reference, checksum and infrastructure ones are
    # ours. Telemetry must never fail the job, so optional fields are read defensively.
    events = get_events()
    kind = (error or {}).get("kind")
    emit = (
        events.info
        if error is None or timed_out(error)
        else events.warning
        if kind == "candidate"
        else events.error
    )
    emit(
        "benchmark_job_finished",
        job_id=job.id,
        environment=getattr(job, "environment", None),
        seed=(job.request or {}).get("seed"),
        candidate_hash=getattr(job, "candidate_hash", None),
        reference_hash=getattr(job, "reference_hash", None),
        outcome="completed" if error is None else "timed-out" if timed_out(error) else "failed",
        error_kind=kind,
        reason=str((error or {}).get("reason") or "")[:2000] or None,
        seconds=round(seconds, 1),
    )


def execute(repo, job, owner, host_lock_fd=None, checkout_lock_fd=None):
    lease = job.lease_seconds
    started = time.monotonic()
    fields = {"job_id": job.id, "worker_id": owner}
    events = get_events()
    events.info("benchmark_claimed", **fields)

    def fail(error):
        if repo.fail(job.id, owner, job.token, error):
            report(job, error, time.monotonic() - started)
            if timed_out(error):
                events.info(
                    "benchmark_timed_out",
                    **fields,
                    phase=error["timeout"].get("phase"),
                    limit=error["timeout"].get("limit"),
                    cell=error["timeout"].get("cell"),
                    reason=str(error.get("reason", ""))[:2000],
                    seconds=time.monotonic() - started,
                )
                return
            kind = error.get("kind")
            severity = (
                "warning" if kind == "candidate" else "critical" if kind == "checksum" else "error"
            )
            events.emit(
                severity,
                "benchmark_failed",
                **fields,
                kind=kind,
                reason=str(error.get("reason", ""))[:2000],
                seconds=time.monotonic() - started,
            )
        else:
            events.warning("benchmark_result_discarded", **fields, reason="lease lost")

    with tempfile.TemporaryDirectory(prefix="exact-job-") as tmp:
        path = Path(tmp)
        try:
            candidate, reference = repo.sources(job.id)
            (path / "candidate").write_bytes(candidate)
            (path / "reference").write_bytes(reference)
            (path / "request.json").write_text(json.dumps(job.request))
            mode = preparation_mode(job.request)
            supervised(
                [
                    sys.executable,
                    "-m",
                    "benchmark",
                    "--request",
                    str(path / "request.json"),
                    "--candidate",
                    str(path / "candidate"),
                    "--reference",
                    str(path / "reference"),
                    "--result",
                    str(path / "result.json"),
                    "--preparation-mode",
                    mode,
                ],
                lambda: repo.heartbeat(job.id, owner, job.token),
                supervisor_seconds(job.request["timing"], mode),
                lease / 3,
                path / "process.log",
                pass_fds=tuple(fd for fd in (host_lock_fd, checkout_lock_fd) if fd is not None),
                env_extra={"GATE_LOCK_HELD": "1"} if checkout_lock_fd is not None else None,
            )
            result = json.loads((path / "result.json").read_text())
            if "error" in result:
                fail(result["error"])
            else:
                if repo.complete(job.id, owner, job.token, result["result"]):
                    report(job, None, time.monotonic() - started)
                    events.info("benchmark_completed", **fields, seconds=time.monotonic() - started)
                else:
                    events.warning("benchmark_result_discarded", **fields, reason="lease lost")
        except BudgetExceeded as exc:
            logger.warning("benchmark job {} ran out of time: {}", job.id, exc)
            fail({"kind": "infrastructure", "reason": f"supervisor-timeout: {exc}"})
        except Exception as exc:
            logger.warning("exact job {} failed: {}", job.id, exc)
            fail({"kind": "infrastructure", "reason": str(exc)})


@observe_worker("competition-benchmark-worker")
def main():
    install_signals()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment")
    parser.add_argument("--credentials", type=Path)
    parser.add_argument(
        "--host-lock", type=Path, default=Path("/tmp/floatlib-exact-benchmark.lock")
    )
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    from sandbox import require_backend

    require_backend()
    load(args.credentials)
    args.environment = args.environment or os.environ.get("BENCHMARK_ENVIRONMENT")
    if not args.environment:
        message = "--environment or BENCHMARK_ENVIRONMENT is required"
        get_events().error("service_misconfigured", error=message)
        parser.error(message)
    get_events().bind(benchmark_environment=args.environment)
    owner = f"{socket.gethostname()}:{__import__('os').getpid()}"
    store = db.connect()
    repo = WorkerDb(store.sessions)
    try:
        while True:
            # Lock before claiming so a job does not wait behind another timing process on a lease.
            with open(args.host_lock, "a") as lock, open(ROOT / "gate.lock", "a") as build_lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                fcntl.flock(build_lock, fcntl.LOCK_EX)
                job = repo.claim(args.environment, machine_identity(), owner)
                if job:
                    execute(repo, job, owner, lock.fileno(), build_lock.fileno())
            if args.once:
                return
            time.sleep(2)
    finally:
        store.close()


if __name__ == "__main__":
    main()
