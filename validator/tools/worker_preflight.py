"""Check worker permissions and local execution without claiming jobs."""

import argparse
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import sandbox
from benchmark import machine_identity
from sqlalchemy import text
from worker_credentials import load

import db
import fingerprint
from db.jobs import WorkerDb


def local_checks():
    sandbox.check_limits()
    for tool in (sandbox.LAKE, "cc"):
        if not shutil.which(tool):
            raise RuntimeError(f"required build tool unavailable: {tool}")
    subprocess.run(
        [sandbox.LAKE, "env", "lean", "--version"],
        cwd=sandbox.LIB,
        check=True,
        capture_output=True,
        timeout=30,
    )
    if sandbox._SANDBOX:
        subprocess.run(
            [sandbox.BWRAP, "--ro-bind", "/", "/", "--unshare-all", "--", "/usr/bin/true"],
            check=True,
            capture_output=True,
            timeout=30,
        )
    if sandbox._BENCH_CPU:
        subprocess.run(
            ["taskset", "-c", sandbox._BENCH_CPU, "/usr/bin/true"],
            check=True,
            capture_output=True,
            timeout=10,
        )
    for directory in (sandbox.HERE / "pinned", sandbox.HERE / "results", sandbox.LIB / "work"):
        directory.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryFile(dir=directory):
            pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--credentials", type=Path)
    parser.add_argument("--kind", choices=("benchmark", "auditor"), default="benchmark")
    parser.add_argument("--environment")
    parser.add_argument("--expected-identity")
    args = parser.parse_args()
    from sandbox import require_backend

    require_backend()
    load(args.credentials)
    store = db.connect()
    try:
        access = WorkerDb(store.sessions).access()
        if access["protocol"] != 2 or not access["enabled"] or access["kind"] != args.kind:
            raise RuntimeError("worker role disabled, incompatible or not provisioned")
        environment = args.environment or os.environ.get("BENCHMARK_ENVIRONMENT")
        if args.kind == "benchmark" and environment not in access["environments"]:
            raise RuntimeError("worker is not assigned to this environment")
        with store.engine.connect() as conn:
            if args.kind == "benchmark":
                required = (
                    "claim(text,text,text,text)",
                    "heartbeat(bigint,text,text)",
                    "complete(bigint,text,text,jsonb)",
                    "fail(bigint,text,text,jsonb)",
                    "sources(bigint,text,text)",
                )
                for signature in required:
                    if not conn.scalar(
                        text("SELECT has_function_privilege(session_user,:f,'EXECUTE')"),
                        {"f": "public." + signature},
                    ):
                        raise RuntimeError("missing worker protocol permission")
            else:
                for table in (
                    "promotions",
                    "evaluations",
                    "submissions",
                    "sources",
                    "audits",
                    "audit_cursors",
                    "audit_attempts",
                ):
                    privileges = "SELECT,INSERT,UPDATE" if table.startswith("audit") else "SELECT"
                    for privilege in privileges.split(","):
                        if not conn.scalar(
                            text("SELECT has_table_privilege(session_user,:t,:p)"),
                            {"t": "public." + table, "p": privilege},
                        ):
                            raise RuntimeError("missing audit permission")
        local_checks()
        report = {"access": access, "proof_identity": fingerprint.proof()}
        if args.kind == "benchmark":
            identity = machine_identity()
            if args.expected_identity and identity != args.expected_identity:
                raise RuntimeError("machine identity differs from coordinator configuration")
            report.update(machine_identity=identity, benchmark_identity=fingerprint.benchmark())
        print(json.dumps(report, indent=2))
    finally:
        store.close()


if __name__ == "__main__":
    main()
