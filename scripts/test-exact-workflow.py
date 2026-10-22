#!/usr/bin/env python3
"""Rehearse proof -> leased jobs -> board decisions -> champion audit -> rewards, in a scratch DB.

Requires CREATE DATABASE on --admin-url, the Lean build environment and working sandbox limits.
Does not use the API or submit chain weights. Short timings exercise plumbing, not admission noise.
"""

from __future__ import annotations

import argparse
import hashlib
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

import db  # noqa: E402
import reward  # noqa: E402
from chain.types import MetagraphView  # noqa: E402
from db import models  # noqa: E402
from exact.process import environment, terminate  # noqa: E402
from workers.weight_setter import WeightSetterConfig, plan_for  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--admin-url", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--candidate", type=Path, default=ROOT / "kernels/exact/incumbent/Kernel.lean"
    )
    parser.add_argument("--minutes", type=float, default=15)
    args = parser.parse_args()
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    config = out / "config.toml"
    subprocess.run(
        [sys.executable, str(ROOT / "scripts/exact-config.py"), "--quick", "--output", str(config)],
        env=environment(),
        check=True,
    )
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
        sid, _ = store.submissions.add(
            "local-exact-smoke-miner", hashlib.sha256(candidate).hexdigest(), "exact", candidate
        )
        with (out / "coordinator.log").open("w") as log:
            coordinator = subprocess.Popen(
                [sys.executable, "-m", "workers.exact_coordinator", "--config", str(config)],
                cwd=ROOT,
                env=env,
                stdout=log,
                stderr=log,
                start_new_session=True,
            )
        processes.append(coordinator)
        while time.monotonic() - started < args.minutes * 60:
            if coordinator.poll() is not None:
                raise RuntimeError("coordinator exited; inspect coordinator.log")
            with db.session_scope(store.sessions) as s:
                submission = s.get(models.Submission, sid)
                done = s.scalar(
                    sa.select(sa.func.count())
                    .select_from(models.ExactEvaluation)
                    .where(models.ExactEvaluation.state == "completed")
                )
                if submission.state == "rejected":
                    raise RuntimeError(f"proof rejected: {submission.reason}")
                # A promotion is finished once the coordinator has audited the new champion.
                promoted = s.scalar(sa.select(sa.func.count()).select_from(models.ExactPromotion))
                audits = list(s.scalars(sa.select(models.ExactAudit.state)))
            if done == 6 and (
                not promoted or (audits and all(a not in ("queued", "running") for a in audits))
            ):
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
                s.scalars(sa.select(models.ExactEvaluation).order_by(models.ExactEvaluation.id))
            )
            jobs = list(s.scalars(sa.select(models.ExactJob).order_by(models.ExactJob.id)))
            submission = s.get(models.Submission, sid)
            audits = [
                {"source_hash": a.source_hash, "state": a.state, "report": a.report}
                for a in s.scalars(sa.select(models.ExactAudit).order_by(models.ExactAudit.id))
            ]
            data = {
                "proof": submission.exact_verification,
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
        setter = WeightSetterConfig(
            scoring_source="exact", netuid=2, treasury_override=0, competition_share_override=0.2
        )
        plan, scored, snapshot = plan_for(
            store,
            MetagraphView((0, 1), {"local-exact-smoke-miner": 1}),
            setter,
            reward.ScoringConfig(),
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
                        sa.text("SELECT id,state,stage,reason,exact_verification FROM submissions")
                    ).mappings()
                ]
                failure["jobs"] = [
                    dict(row)
                    for row in conn.execute(
                        sa.text("SELECT id,state,request,result,error FROM exact_jobs")
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
