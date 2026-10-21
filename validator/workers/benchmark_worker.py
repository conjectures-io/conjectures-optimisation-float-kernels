"""One shared hardware worker implementation; run with PYTHONPATH=.:validator."""

from __future__ import annotations

import argparse
import fcntl
import json
import socket
import sys
import tempfile
import time
from pathlib import Path

from benchmark import machine_identity
from loguru import logger
from process import install_signals, supervised

import db
from db.jobs import WorkerDb


def execute(repo, job, owner, host_lock_fd=None):
    lease = job.lease_seconds
    with tempfile.TemporaryDirectory(prefix="exact-job-") as tmp:
        path = Path(tmp)
        try:
            candidate, reference = repo.sources(job.id)
            (path / "candidate").write_bytes(candidate)
            (path / "reference").write_bytes(reference)
            (path / "request.json").write_text(json.dumps(job.request))
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
                ],
                lambda: repo.heartbeat(job.id, owner, job.token),
                job.request["timing"]["job_seconds"],
                lease / 3,
                path / "process.log",
                pass_fds=() if host_lock_fd is None else (host_lock_fd,),
            )
            result = json.loads((path / "result.json").read_text())
            if "error" in result:
                repo.fail(job.id, owner, job.token, result["error"])
            else:
                repo.complete(job.id, owner, job.token, result["result"])
        except Exception as exc:
            logger.warning("exact job {} failed: {}", job.id, exc)
            repo.fail(job.id, owner, job.token, {"kind": "infrastructure", "reason": str(exc)})


def main():
    install_signals()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment", required=True)
    parser.add_argument(
        "--host-lock", type=Path, default=Path("/tmp/floatlib-exact-benchmark.lock")
    )
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    owner = f"{socket.gethostname()}:{__import__('os').getpid()}"
    store = db.connect()
    repo = WorkerDb(store.sessions)
    try:
        while True:
            # Lock before claiming so a job does not wait behind another timing process on a lease.
            with open(args.host_lock, "a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                job = repo.claim(args.environment, machine_identity(), owner)
                if job:
                    execute(repo, job, owner, lock.fileno())
            if args.once:
                return
            time.sleep(2)
    finally:
        store.close()


if __name__ == "__main__":
    main()
