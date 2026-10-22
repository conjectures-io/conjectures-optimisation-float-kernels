#!/usr/bin/env python3
"""Rehearse proof -> leased jobs -> board decisions -> rewards, in a scratch DB.

Requires CREATE DATABASE on --admin-url, the Lean build environment and working sandbox limits.
Does not use the API or submit chain weights. Short timings exercise plumbing, not admission noise.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "validator")]

import sqlalchemy as sa  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config as AlembicConfig  # noqa: E402
from config import Config  # noqa: E402
from process import environment, terminate  # noqa: E402

import db  # noqa: E402
from chain.types import MetagraphView  # noqa: E402
from db import models  # noqa: E402
from workers.weight_setter import WeightSetterConfig, plan_for  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--admin-url", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--candidate", type=Path, default=ROOT / "kernels/exact/incumbent/Kernel.lean"
    )
    parser.add_argument("--minutes", type=float, default=15)
    parser.add_argument("--reference", type=Path)
    parser.add_argument(
        "--board", choices=("overall", "mlp", "resnet", "mobilenet", "normalized_mlp", "mixer")
    )
    args = parser.parse_args()
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    # Freeze the code behind identities and the orchestration used for this run.
    import fingerprint

    frozen = {}
    tracked = sorted(
        set(fingerprint.PINNED)
        | {
            "validator/config.py",
            "validator/scoring.py",
            "validator/rewards.py",
            "validator/db/models.py",
            "validator/db/evaluations.py",
            "validator/db/submissions.py",
            "validator/db/jobs.py",
            "validator/workers/coordinator.py",
            "validator/workers/benchmark_worker.py",
            "validator/workers/weight_setter.py",
            "deploy/migrate/alembic/versions/0001_initial.py",
            "deploy/migrate/alembic/versions/0001_job_protocol.sql",
            "scripts/test-exact-workflow.py",
            "scripts/exact-config.py",
        }
    )
    import hashlib

    for name in tracked:
        data = (ROOT / name).read_bytes()
        target = out / "sources" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        frozen[name] = hashlib.sha256(data).hexdigest()
    (out / "source-manifest.json").write_text(json.dumps(frozen, indent=2))
    config = out / "config.toml"
    extra = (["--reference", str(args.reference)] if args.reference else []) + (
        ["--board", args.board] if args.board else []
    )
    subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/exact-config.py"),
            "--quick",
            "--output",
            str(config),
            *extra,
        ],
        env=environment(),
        check=True,
    )
    expected = len(Config.load(config).boards)
    candidate = args.candidate.read_bytes() + b"\n-- Local workflow smoke case.\n"
    (out / "Kernel.lean").write_bytes(candidate)
    name = "exact_rehearsal_" + uuid.uuid4().hex[:12]
    admin = sa.create_engine(args.admin_url, isolation_level="AUTOCOMMIT")
    url = sa.engine.make_url(args.admin_url).set(database=name)
    url_text = url.render_as_string(hide_password=False)
    env = environment() | {"DATABASE_URL": url_text}
    processes = []
    original_work = (
        (ROOT / "lean/work/Submission.lean").read_bytes()
        if (ROOT / "lean/work/Submission.lean").exists()
        else None
    )
    started = time.monotonic()
    store = None
    with admin.connect() as conn:
        conn.exec_driver_sql(f"CREATE DATABASE {name}")
    try:
        os.environ["DATABASE_URL"] = url_text
        cfg = AlembicConfig(str(ROOT / "deploy/migrate/alembic.ini"))
        cfg.set_main_option("script_location", str(ROOT / "deploy/migrate/alembic"))
        command.upgrade(cfg, "head")
        store = db.connect(url_text)
        with (out / "coordinator.log").open("w") as log:
            coordinator = subprocess.Popen(
                [sys.executable, "-m", "workers.coordinator", "--config", str(config)],
                cwd=ROOT,
                env=env,
                stdout=log,
                stderr=log,
                start_new_session=True,
            )
        processes.append(coordinator)
        # Activate boards first; uploads arriving before activation are intentionally excluded.
        deadline = time.monotonic() + 30
        while True:
            with db.session_scope(store.sessions) as s:
                ready = s.scalar(sa.select(sa.func.count()).select_from(models.BoardProgress))
            if ready == expected:
                break
            if coordinator.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError("coordinator activation failed")
            time.sleep(0.2)
        with db.session_scope(store.sessions) as s:
            import datetime as dt

            s.add(
                models.Registration(
                    uid=1,
                    ss58_hot="local-exact-smoke-miner",
                    ss58_cold="local",
                    block=1,
                    block_date=dt.datetime.now(dt.timezone.utc),
                )
            )
        sid = store.submissions.add("local-exact-smoke-miner", candidate)

        while time.monotonic() - started < args.minutes * 60:
            if coordinator.poll() is not None:
                raise RuntimeError("coordinator exited; inspect coordinator.log")
            with db.session_scope(store.sessions) as s:
                submission = s.get(models.Submission, sid)
                done = s.scalar(
                    sa.select(sa.func.count())
                    .select_from(models.Evaluation)
                    .where(models.Evaluation.state == "completed")
                )
                if submission.state == "rejected":
                    raise RuntimeError(f"proof rejected: {submission.reason}")
            if done == expected:
                break
            with (out / "worker.log").open("a") as log:
                worker = subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "workers.benchmark_worker",
                        "--environment",
                        "local",
                        "--once",
                    ],
                    cwd=ROOT,
                    env=env,
                    stdout=log,
                    stderr=log,
                    start_new_session=True,
                )
            processes.append(worker)
            worker.wait(timeout=max(1, args.minutes * 60 - (time.monotonic() - started)))
            if worker.returncode:
                raise RuntimeError("benchmark worker exited; inspect worker.log")
            time.sleep(1)
        else:
            raise TimeoutError("rehearsal deadline reached")
        with db.session_scope(store.sessions) as s:
            evaluations = list(
                s.scalars(sa.select(models.Evaluation).order_by(models.Evaluation.id))
            )
            jobs = list(s.scalars(sa.select(models.BenchmarkJob).order_by(models.BenchmarkJob.id)))
            submission = s.get(models.Submission, sid)
            audits = [
                {"source_hash": a.source_hash, "state": a.state, "report": a.report}
                for a in s.scalars(sa.select(models.Audit).order_by(models.Audit.id))
            ]
            data = {
                "proof": submission.proof_report,
                "evaluations": [
                    {"id": e.id, "board": e.board, "plan": e.plan, "decision": e.decision}
                    for e in evaluations
                ],
                "jobs": [
                    {
                        "id": j.id,
                        "state": j.state,
                        "request": j.request,
                        "result": j.result,
                        "error": j.error,
                    }
                    for j in jobs
                ],
                "audits": audits,
            }
        setter = WeightSetterConfig(netuid=2, treasury_override=0, competition_share_override=0.2)
        plan, scored, snapshot = plan_for(
            store,
            MetagraphView((0, 1), {"local-exact-smoke-miner": 1}),
            setter,
        )
        data.update(
            seconds=time.monotonic() - started,
            reward_snapshot=snapshot,
            uids=plan.uids,
            weights=plan.weights,
        )
        (out / "results.json").write_text(json.dumps(data, indent=2))
        print(
            f"Proof accepted; {len(evaluations)} board decisions; {len(jobs)} jobs; "
            f"audits {[a['state'] for a in audits]}; vector {plan.weights}; "
            f"{data['seconds']:.1f}s"
        )
    except Exception as exc:
        failure = {"reason": str(exc), "seconds": time.monotonic() - started}
        if store:
            with store.engine.connect() as conn:
                failure["submissions"] = [
                    dict(row)
                    for row in conn.execute(
                        sa.text("SELECT id,state,stage,reason,proof_report FROM submissions")
                    ).mappings()
                ]
                failure["jobs"] = [
                    dict(row)
                    for row in conn.execute(
                        sa.text("SELECT id,state,request,result,error FROM benchmark_jobs")
                    ).mappings()
                ]
        (out / "failure.json").write_text(json.dumps(failure, indent=2, default=str))
        raise
    finally:
        for proc in reversed(processes):
            terminate(proc)
        if store:
            store.close()
        if original_work is not None:
            (ROOT / "lean/work/Submission.lean").write_bytes(original_work)
        else:
            (ROOT / "lean/work/Submission.lean").unlink(missing_ok=True)
        with admin.connect() as conn:
            conn.execute(
                sa.text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname=:name AND pid<>pg_backend_pid()"
                ),
                {"name": name},
            )
            conn.exec_driver_sql(f"DROP DATABASE {name}")
        admin.dispose()


if __name__ == "__main__":
    main()
