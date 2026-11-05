"""Coordinator planning, immutable evidence links and atomic promotion/publication."""

from __future__ import annotations

import datetime as dt
import hashlib
import secrets
from pathlib import Path

from comparisons import board_request, board_result, bundle_requests
from config import Config, digest, measurement_hash
from multiseed import workloads
from scoring import assess
from sqlalchemy import exists, func, or_, select

from axiom import get_events

from . import models as m
from .champions import champion, history, source_blocked
from .engine import session_scope
from .submissions import SubmissionsDb, db_now, lead, put_source


def intake_modes(config):
    # Which uploads are evaluated: dev-tagged ones only on a development deployment.
    return ("subnet", "dev") if config.dev_intake else ("subnet",)


def lock_board(session, board):
    session.execute(select(func.pg_advisory_xact_lock(func.hashtextextended("tensor:" + board, 0))))


# What a decision says about a timeout; the worker's full evidence stays with the failed job.
TIMEOUT_FIELDS = (
    "phase",
    "limit",
    "running",
    "allowance_seconds",
    "budget_seconds",
    "phase_budget_seconds",
    "configured_phase_seconds",
    "bounded_by_overall",
    "elapsed_seconds",
    "stopped_after_seconds",
    "peak_memory_bytes",
    "runs",
    "cell",
    "cells_finished",
    "cells_total",
)


def failure_decision(error, environment, seed):
    """The decision a candidate's failed job makes: out of time is `timed-out`, a measured result;
    anything else, a crash, a failed build or invalid output, is `benchmark-failed`."""
    decision = {"reason": error["reason"], "score": None}
    timeout = error.get("timeout")
    if not isinstance(timeout, dict):
        return {"outcome": "benchmark-failed"} | decision
    partial = error.get("partial") if isinstance(error.get("partial"), dict) else {}
    rounds = partial.get("rounds")
    summary = {k: timeout.get(k) for k in TIMEOUT_FIELDS} | {
        "environment": environment,
        "seed": seed,
        "rounds_completed": len(rounds) if isinstance(rounds, list) else None,
        "rounds_planned": partial.get("rounds_planned"),
    }
    return {"outcome": "timed-out"} | decision | {"timeout": summary}


def select_real_sample_plan(real_workloads, timing, *, rng=None):
    """Draw a balanced secret sample schedule shared by all environments for an evaluation.

    Each process runs batch one. When observations outnumber available examples, examples recur
    across fresh processes; within a pass they remain distinct (trivial for batch one).
    """
    chooser = rng or secrets.SystemRandom()
    plan = {}
    for workload in real_workloads:
        if workload["samples_per_run"] != 1:
            raise ValueError("real inference must use batch size one")
        count = timing["rounds"] * timing["inner"]
        pool = list(range(workload["sample_count"]))
        schedule = []
        while len(schedule) < count:
            cycle = chooser.sample(pool, len(pool))
            schedule.extend(cycle[: count - len(schedule)])
        chooser.shuffle(schedule)
        cursor = 0
        rounds = []
        for _ in range(timing["rounds"]):
            passes = []
            for _ in range(timing["inner"]):
                passes.append([schedule[cursor]])
                cursor += 1
            rounds.append(passes)
        plan[workload["id"]] = rounds
    return plan


def select_real_warmup_plan(real_workloads, timing, sample_plan, *, rng=None):
    """Select one hidden, unscored example per fresh process, distinct from its scored input."""
    chooser = rng or secrets.SystemRandom()
    result = {}
    for workload in real_workloads:
        count = workload["sample_count"]
        if count < 2:
            raise ValueError(f"{workload['id']}: warm-up needs at least two input examples")
        rounds = []
        for round_index, passes in enumerate(sample_plan[workload["id"]]):
            warmup_passes = []
            for pass_index, scored in enumerate(passes):
                choices = [index for index in range(count) if index not in scored]
                warmup_passes.append([chooser.choice(choices)])
            rounds.append(warmup_passes)
        result[workload["id"]] = rounds
    return result


def select_synthetic_warmup_plan(input_plan, *, rng=None):
    """Draw independent hidden warm-up seeds, separate from every scored synthetic seed."""
    chooser = rng or secrets.SystemRandom()
    used = {
        seed
        for rounds in input_plan.values()
        for passes in rounds
        for values in passes
        for seed in values
    }
    result = {}
    for key, rounds in input_plan.items():
        selected_rounds = []
        for passes in rounds:
            selected_passes = []
            for _ in passes:
                seed = chooser.getrandbits(32)
                while seed in used:
                    seed = chooser.getrandbits(32)
                used.add(seed)
                selected_passes.append([seed])
            selected_rounds.append(selected_passes)
        result[key] = selected_rounds
    return result


def _sample_plan_prefix(plan, timing):
    """Reshape a shared workload schedule for one board's observation count."""
    count = timing["rounds"] * timing["inner"]
    result = {}
    for workload_id, rounds in plan.items():
        flattened = [samples for round_samples in rounds for samples in round_samples]
        if len(flattened) < count:
            raise ValueError("shared sample schedule is shorter than a board plan")
        selected = flattened[:count]
        result[workload_id] = [
            selected[start : start + timing["inner"]] for start in range(0, count, timing["inner"])
        ]
    return result


def select_synthetic_input_plan(workload_seeds, synthetic_cells, timing, *, rng=None):
    """Resolve hidden, distinct generator seeds for every draw/cell/observation."""
    chooser = rng or secrets.SystemRandom()
    used = set()
    plan = {}
    for workload_seed in workload_seeds:
        for draw in workloads(workload_seed):
            for cell in synthetic_cells:
                key = f"{draw['id']}|{cell}"
                observations = []
                for _ in range(timing["rounds"]):
                    selected = []
                    while len(selected) < timing["inner"]:
                        value = chooser.getrandbits(32)
                        if value not in used:
                            used.add(value)
                            # One seed per fresh process pass, with an innermost one-item
                            # repetition list matching run_binary's input sequence shape.
                            selected.append([value])
                    observations.append(selected)
                plan[key] = observations
    return plan


def _synthetic_plan_prefix(plan, workload_seeds, cells, timing):
    """Select one board's cells/seeds and truncate its schedule to that board's timing."""
    count = timing["rounds"] * timing["inner"]
    result = {}
    for workload_seed in workload_seeds:
        for draw in workloads(workload_seed):
            for cell in cells:
                key = f"{draw['id']}|{cell}"
                flattened = [values for group in plan[key] for values in group]
                selected = flattened[:count]
                result[key] = [
                    selected[start : start + timing["inner"]]
                    for start in range(0, count, timing["inner"])
                ]
    return result


def _flatten_schedule(schedule):
    return [values for round_values in schedule for values in round_values]


def _reshape_schedule(values, timing):
    count = timing["rounds"] * timing["inner"]
    if len(values) != count:
        raise ValueError("input schedule has the wrong number of observations")
    return [values[start : start + timing["inner"]] for start in range(0, count, timing["inner"])]


def reuse_submission_schedules(assignment, previous_plans, timing, real_workloads=()):
    """Keep scheduled inputs stable when a submission is replanned or gains an environment."""
    previous_real = {}
    previous_synthetic = {}
    for plan in previous_plans:
        plan = getattr(plan, "plan", plan)
        for workload_id, schedule in (plan.get("real_sample_plan") or {}).items():
            values = _flatten_schedule(schedule)
            if len(values) > len(previous_real.get(workload_id, ())):
                previous_real[workload_id] = values
        for key, schedule in (plan.get("synthetic_input_plan") or {}).items():
            values = _flatten_schedule(schedule)
            if len(values) > len(previous_synthetic.get(key, ())):
                previous_synthetic[key] = values

    count = timing["rounds"] * timing["inner"]
    real = assignment.get("real_sample_plan") or {}
    for workload_id, generated in real.items():
        old = previous_real.get(workload_id, [])
        values = (old + _flatten_schedule(generated)[len(old) :])[:count]
        real[workload_id] = _reshape_schedule(values, timing)
    assignment["real_sample_plan"] = real or None

    synthetic = assignment.get("synthetic_input_plan") or {}
    old_seeds = {
        group[0]
        for values in previous_synthetic.values()
        for group in values
        if isinstance(group, list) and group
    }
    used = set()
    for key, generated in synthetic.items():
        old = previous_synthetic.get(key, [])
        values = list(old[:count])
        used.update(group[0] for group in values)
        for group in _flatten_schedule(generated):
            if len(values) >= count:
                break
            seed = group[0]
            if seed not in used and seed not in old_seeds:
                values.append(group)
                used.add(seed)
        while len(values) < count:
            seed = secrets.randbits(32)
            if seed not in old_seeds and seed not in used:
                values.append([seed])
                used.add(seed)
        synthetic[key] = _reshape_schedule(values, timing)
    assignment["synthetic_input_plan"] = synthetic or None
    assignment["real_warmup_plan"] = (
        select_real_warmup_plan(real_workloads, timing, real) if real else None
    )
    assignment["synthetic_warmup_plan"] = (
        select_synthetic_warmup_plan(synthetic) if synthetic else None
    )


def submission_assignment(
    real_workloads,
    timing,
    *,
    fresh_seed_count=0,
    previous_seed_values=(),
    workload_seeds=(),
    synthetic_cells=(),
    rng=None,
):
    """Resolve secret workload inputs once for all board evaluations of one submission."""
    chooser = rng or secrets.SystemRandom()
    seed_values = list(previous_seed_values) if fresh_seed_count else []
    if fresh_seed_count and len(seed_values) != fresh_seed_count:
        seed_values = []
        while len(seed_values) < fresh_seed_count:
            value = chooser.getrandbits(32)
            if value not in seed_values:
                seed_values.append(value)
    sample_plan = select_real_sample_plan(real_workloads, timing, rng=chooser)
    selected_workload_seeds = seed_values if fresh_seed_count else list(workload_seeds)
    input_plan = select_synthetic_input_plan(
        selected_workload_seeds, synthetic_cells, timing, rng=chooser
    )
    real_warmup_plan = select_real_warmup_plan(real_workloads, timing, sample_plan, rng=chooser)
    synthetic_warmup_plan = select_synthetic_warmup_plan(input_plan, rng=chooser)
    return {
        "seed_values": seed_values,
        "real_sample_plan": sample_plan or None,
        "synthetic_input_plan": input_plan or None,
        "real_warmup_plan": real_warmup_plan or None,
        "synthetic_warmup_plan": synthetic_warmup_plan or None,
    }


def job_query(eid):
    return select(m.BenchmarkJob).join(m.EvaluationJob).where(m.EvaluationJob.evaluation_id == eid)


def cancel(session, evaluation):
    evaluation.state = "superseded"
    # Only completed evidence is shared, but keep the active-consumer check explicit.
    session.flush()
    jobs = list(session.scalars(job_query(evaluation.id)))
    for job in jobs:
        consumers = session.scalar(
            select(m.Evaluation.id)
            .join(m.EvaluationJob)
            .where(m.EvaluationJob.job_id == job.id, m.Evaluation.state == "pending")
            .limit(1)
        )
        if consumers is None and job.state in ("queued", "running", "failed"):
            job.state = "cancelled"
            job.claimant = job.owner = job.token = job.lease_until = None


class EvaluationsDb:
    def __init__(self, sessions):
        self.sessions = sessions
        self.leader_token = None
        self.submissions = SubmissionsDb(sessions)

    def set_leader(self, token):
        with session_scope(self.sessions) as s:
            row = s.get(m.CoordinatorState, 1, with_for_update=True)
            if row is None:
                s.add(m.CoordinatorState(id=1, leader_token=token))
            else:
                row.leader_token = token
        self.leader_token = self.submissions.leader_token = token

    def _lead(self, session):
        return lead(session, self.leader_token)

    def reconcile(self, config: Config):
        with session_scope(self.sessions) as s:
            state = self._lead(s)
            # Same lock as intake: no partially received upload can cross the boundary.
            s.execute(select(func.pg_advisory_xact_lock(func.hashtextextended("tensor:intake", 0))))
            old_boards = set(state.config_evidence["boards"]) if state.config_evidence else set()
            boundary = s.scalar(select(func.max(m.Submission.id))) or 0
            for board in config.boards:
                progress = s.get(m.BoardProgress, board)
                if progress is None:
                    s.add(m.BoardProgress(board=board, activation_boundary=boundary))
                elif board not in old_boards:
                    progress.activation_boundary = boundary
                source = Path(config.boards[board]["reference"]["path"]).read_bytes()
                if (
                    hashlib.sha256(source).hexdigest()
                    != config.boards[board]["reference"]["sha256"]
                ):
                    raise ValueError("initial reference hash mismatch")
                put_source(s, source)
            for e in s.scalars(
                select(m.Evaluation).where(m.Evaluation.state == "pending").with_for_update()
            ):
                if e.board not in config.boards or e.config_hash != config.identity(e.board):
                    cancel(s, e)
            state.config_hash, state.config_evidence = digest(config.public()), config.public()
            s.flush()
            self._publish(s, config)

    def plan(self, config: Config):
        created = []
        planned = []
        board_plans = {board: config.plan(board) for board in config.boards}
        max_timing = max(
            (plan["timing"] for plan in board_plans.values()),
            key=lambda timing: timing["rounds"] * timing["inner"],
        )
        all_real_workloads = {
            workload["id"]: workload
            for plan in board_plans.values()
            for workload in plan["real_workloads"]
        }
        submission_assignments = {}
        request_groups = {}
        planned_by_evaluation = {}
        with session_scope(self.sessions) as s:
            self._lead(s)
            for board in config.boards:
                lock_board(s, board)
                progress = s.get(m.BoardProgress, board)
                if progress is None:
                    raise RuntimeError("reconcile configuration before planning")
                pending = s.scalar(
                    select(m.Evaluation).where(
                        m.Evaluation.board == board, m.Evaluation.state == "pending"
                    )
                )
                ref = champion(s, board)
                if pending:
                    blocked = s.scalar(
                        select(source_blocked(m.Submission.source_sha256)).where(
                            m.Submission.id == pending.submission_id
                        )
                    )
                    if not blocked and pending.reference_promotion_id == (ref.id if ref else None):
                        continue
                    cancel(s, pending)
                    s.flush()
                done = exists(
                    select(m.Evaluation.id).where(
                        m.Evaluation.submission_id == m.Submission.id,
                        m.Evaluation.board == board,
                        m.Evaluation.state == "completed",
                    )
                )
                # Rejected uploads do not block; queued/running proofs retain intake FIFO.
                sub = s.scalar(
                    select(m.Submission)
                    .where(
                        m.Submission.id > progress.activation_boundary,
                        m.Submission.state != "rejected",
                        m.Submission.intake_mode.in_(intake_modes(config)),
                        ~source_blocked(m.Submission.source_sha256),
                        ~done,
                    )
                    .order_by(m.Submission.submitted_at, m.Submission.id)
                    .limit(1)
                )
                if sub is None or sub.state != "proved":
                    continue
                # Existing paid state is preserved if proof policy changes; stale proofs are
                # explicitly audited/requeued by an operator, never silently reverified here.
                if sub.verification_identity != config.proof_identity:
                    continue
                refhash = s.get(m.Evaluation, ref.evaluation_id).submission_id if ref else None
                refhash = (
                    s.get(m.Submission, refhash).source_sha256
                    if ref
                    else config.boards[board]["reference"]["sha256"]
                )
                previous = s.scalar(
                    select(m.Evaluation)
                    .where(m.Evaluation.submission_id == sub.id, m.Evaluation.board == board)
                    .order_by(m.Evaluation.generation.desc())
                    .limit(1)
                )
                generation = previous.generation + 1 if previous else 1
                plan = board_plans[board]
                assignment = submission_assignments.get(sub.id)
                if assignment is None:
                    old_plans = list(
                        s.scalars(
                            select(m.Evaluation)
                            .where(m.Evaluation.submission_id == sub.id)
                            .order_by(m.Evaluation.generation.desc(), m.Evaluation.id.desc())
                        )
                    )
                    old_seed_values = next(
                        (
                            row.plan.get("seed_values", [])
                            for row in old_plans
                            if row.plan.get("seed_values")
                        ),
                        [],
                    )
                    assignment = submission_assignments[sub.id] = submission_assignment(
                        list(all_real_workloads.values()),
                        max_timing,
                        fresh_seed_count=(config.seed_count if config.seed_mode == "fresh" else 0),
                        previous_seed_values=old_seed_values,
                        workload_seeds=(config.seeds if config.seed_mode == "fixed" else ()),
                        synthetic_cells=config.synthetic_cells,
                    )
                    reuse_submission_schedules(
                        assignment, old_plans, max_timing, list(all_real_workloads.values())
                    )
                if config.seed_mode == "fresh":
                    plan["seed_values"] = assignment["seed_values"]
                    for st in plan["strata"]:
                        st["seed"] = assignment["seed_values"][st["seed"]]
                if plan["real_workloads"]:
                    plan["real_sample_plan"] = _sample_plan_prefix(
                        {
                            workload["id"]: assignment["real_sample_plan"][workload["id"]]
                            for workload in plan["real_workloads"]
                        },
                        plan["timing"],
                    )
                    plan["real_warmup_plan"] = _sample_plan_prefix(
                        {
                            workload["id"]: assignment["real_warmup_plan"][workload["id"]]
                            for workload in plan["real_workloads"]
                        },
                        plan["timing"],
                    )
                else:
                    plan["real_sample_plan"] = None
                    plan["real_warmup_plan"] = None
                if "synthetic" in plan["suites"]:
                    plan["synthetic_input_plan"] = _synthetic_plan_prefix(
                        assignment["synthetic_input_plan"],
                        [st["seed"] for st in plan["strata"]],
                        plan["cells"],
                        plan["timing"],
                    )
                    plan["synthetic_warmup_plan"] = _synthetic_plan_prefix(
                        assignment["synthetic_warmup_plan"],
                        [st["seed"] for st in plan["strata"]],
                        plan["cells"],
                        plan["timing"],
                    )
                else:
                    plan["synthetic_input_plan"] = None
                    plan["synthetic_warmup_plan"] = None
                e = m.Evaluation(
                    submission_id=sub.id,
                    board=board,
                    generation=generation,
                    config_hash=config.identity(board),
                    reference_promotion_id=ref.id if ref else None,
                    reference_hash=refhash,
                    plan=plan,
                )
                s.add(e)
                s.flush()
                created.append(e.id)
                for st in plan["strata"]:
                    request = {k: st[k] for k in ("environment", "identity", "seed")} | {
                        "candidate_hash": sub.source_sha256,
                        "reference_hash": refhash,
                        "baseline_hash": plan["baseline_hash"],
                        "cells": sorted(plan["cells"]),
                        "timing": st.get("timing", plan["timing"]),
                        "benchmark_identity": plan["benchmark_identity"],
                        "workloads": workloads(st["seed"]) if "synthetic" in plan["suites"] else [],
                        "suites": plan["suites"],
                        "real_workloads": plan["real_workloads"],
                        "real_sample_plan": plan["real_sample_plan"],
                        "synthetic_input_plan": plan["synthetic_input_plan"],
                        "real_warmup_plan": plan["real_warmup_plan"],
                        "synthetic_warmup_plan": plan["synthetic_warmup_plan"],
                        "warmup_policy": plan["warmup_policy"],
                    }
                    key = (sub.id, st["environment"], st["seed"])
                    request_groups.setdefault(key, []).append(
                        {"evaluation_id": e.id, "board": board, "request": request}
                    )
                event = {
                    "evaluation_id": e.id,
                    "submission_id": sub.id,
                    "hotkey": sub.hotkey,
                    "board": board,
                    "generation": generation,
                    "reference_promotion_id": e.reference_promotion_id,
                    "jobs": len(plan["strata"]),
                    "reused_jobs": 0,
                }
                planned_by_evaluation[e.id] = event
                planned.append(event)

            for group in request_groups.values():
                requests = {entry["board"]: entry["request"] for entry in group}
                bundled = None
                if len(group) > 1 and len(requests) == len(group):
                    try:
                        bundled = bundle_requests(requests)
                    except (KeyError, TypeError, ValueError):
                        # Different board profiles can retain the ordinary independent job path.
                        bundled = None
                job_request_sets = (
                    [(bundled, group)]
                    if bundled is not None
                    else [(entry["request"], [entry]) for entry in group]
                )
                for request, consumers in job_request_sets:
                    request_hash = (
                        digest(request) if "comparisons" in request else measurement_hash(request)
                    )
                    old = s.scalar(
                        select(m.BenchmarkJob)
                        .where(
                            m.BenchmarkJob.request_hash == request_hash,
                            m.BenchmarkJob.state == "completed",
                            m.BenchmarkJob.result.is_not(None),
                        )
                        .order_by(m.BenchmarkJob.id.desc())
                        .limit(1)
                    )
                    reused = old is not None
                    if old is None:
                        old = m.BenchmarkJob(
                            request_hash=request_hash,
                            request=request,
                            candidate_hash=consumers[0]["request"]["candidate_hash"],
                            reference_hash=request["reference_hash"],
                            environment=request["environment"],
                            lease_seconds=config.lease_seconds,
                            retry_seconds=config.retry_seconds,
                        )
                        s.add(old)
                        s.flush()
                    for consumer in consumers:
                        s.add(
                            m.EvaluationJob(
                                evaluation_id=consumer["evaluation_id"],
                                job_id=old.id,
                                environment=request["environment"],
                                seed=request["seed"],
                            )
                        )
                        if reused:
                            planned_by_evaluation[consumer["evaluation_id"]]["reused_jobs"] += 1
        # After the commit, so an event never describes a plan that rolled back.
        for fields in planned:
            get_events().info("evaluation_planned", **fields)
        return created

    def ready(self):
        with session_scope(self.sessions) as s:
            out = []
            for e in s.scalars(
                select(m.Evaluation)
                .where(m.Evaluation.state == "pending")
                .order_by(m.Evaluation.id)
            ):
                jobs = list(s.scalars(job_query(e.id).order_by(m.BenchmarkJob.id)))
                if jobs and all(j.state == "completed" for j in jobs):
                    pairs = []
                    for job in jobs:
                        if "comparisons" in job.request:
                            pairs.append(
                                (
                                    board_request(job.request, e.board),
                                    board_result(job.request, job.result, e.board),
                                )
                            )
                        else:
                            pairs.append((job.request, job.result))
                    out.append((e, pairs))
            return out

    def finalize(self, eid, config, decision):
        with session_scope(self.sessions) as s:
            self._lead(s)
            e = s.get(m.Evaluation, eid)
            if e is None:
                return False
            lock_board(s, e.board)
            s.refresh(e, with_for_update=True)
            if e.state != "pending":
                return False
            current = champion(s, e.board)
            sub = s.get(m.Submission, e.submission_id)
            if (
                e.board not in config.boards
                or e.config_hash != config.identity(e.board)
                or e.reference_promotion_id != (current.id if current else None)
                or s.scalar(select(source_blocked(sub.source_sha256)))
                or sub.state != "proved"
                or sub.verification_identity != config.proof_identity
                or sub.intake_mode not in intake_modes(config)
            ):
                cancel(s, e)
                return False
            e.state, e.decision, e.completed_at = "completed", decision, db_now(s)
            for job in s.scalars(job_query(e.id)):
                if job.state in ("queued", "running"):
                    job.state = "cancelled"
                    job.claimant = job.owner = job.token = job.lease_until = None
            promotion = None
            if decision["outcome"] == "qualifies":
                promotion = m.Promotion(
                    board=e.board,
                    evaluation_id=e.id,
                    source_environment=(sub.proof_report or {}).get("source_environment", {}),
                )
                s.add(promotion)
            progress = s.get(m.BoardProgress, e.board)
            progress.last_submission_id, progress.last_submitted_at = sub.id, sub.submitted_at
            s.flush()
            self._publish(s, config)
            fields = {
                "evaluation_id": e.id,
                "submission_id": sub.id,
                "hotkey": sub.hotkey,
                "board": e.board,
                "generation": e.generation,
                "reference_promotion_id": e.reference_promotion_id,
            }
            promotion_id = promotion.id if promotion else None
        # After the commit: a decision or a promotion that rolled back was never made.
        events = get_events()
        (events.warning if decision["outcome"] == "benchmark-failed" else events.info)(
            "evaluation_decided",
            **fields,
            outcome=decision["outcome"],
            score=decision.get("score"),
            gain_lower=decision.get("gain_lower"),
            guard_passed=decision.get("guard_passed"),
            reason=decision.get("reason"),
        )
        if promotion_id is not None:
            events.info(
                "champion_promoted",
                **fields,
                promotion_id=promotion_id,
                score=decision.get("score"),
            )
        return True

    def aggregate(self, config):
        count = 0
        with session_scope(self.sessions) as s:
            failed = list(
                s.execute(
                    select(
                        m.Evaluation.id,
                        m.BenchmarkJob.error,
                        m.EvaluationJob.environment,
                        m.EvaluationJob.seed,
                    )
                    .join(m.EvaluationJob, m.EvaluationJob.evaluation_id == m.Evaluation.id)
                    .join(m.BenchmarkJob, m.BenchmarkJob.id == m.EvaluationJob.job_id)
                    .where(
                        m.Evaluation.state == "pending",
                        m.BenchmarkJob.state == "failed",
                        m.BenchmarkJob.error["kind"].astext == "candidate",
                    )
                    .order_by(m.BenchmarkJob.id)
                )
            )
        for eid, error, environment, seed in failed:
            count += self.finalize(eid, config, failure_decision(error, environment, seed))
        # Bootstrap holds no database connection.
        for e, pairs in self.ready():
            try:
                decision = assess(e.plan, pairs)
            except (ValueError, KeyError, TypeError) as exc:
                quarantined = False
                with session_scope(self.sessions) as s:
                    self._lead(s)
                    row = s.get(m.Evaluation, e.id, with_for_update=True)
                    if row.state == "pending":
                        quarantined = True
                        cancel(s, row)
                        for job in s.scalars(job_query(e.id)):
                            # Retain malformed payloads for diagnosis, but never reuse them.
                            if job.state == "completed":
                                job.state = "failed"
                                job.error = {"kind": "infrastructure", "reason": str(exc)[:2000]}
                                for consumer in s.scalars(
                                    select(m.Evaluation)
                                    .join(m.EvaluationJob)
                                    .where(
                                        m.EvaluationJob.job_id == job.id,
                                        m.Evaluation.state == "pending",
                                    )
                                ):
                                    cancel(s, consumer)
                if quarantined:
                    get_events().error(
                        "evaluation_quarantined",
                        evaluation_id=e.id,
                        submission_id=e.submission_id,
                        board=e.board,
                        generation=e.generation,
                        error=f"{type(exc).__name__}: {exc}"[:2000],
                    )
                continue
            count += self.finalize(e.id, config, decision)
        return count

    def disqualify(self, promotion_id, reason, config):
        if not reason.strip():
            raise ValueError("disqualification requires a reason")
        with session_scope(self.sessions) as s:
            self._lead(s)
            row = s.get(m.Promotion, promotion_id, with_for_update=True)
            if row is None:
                raise ValueError("unknown promotion")
            lock_board(s, row.board)
            row.disqualified_at, row.disqualification_reason = db_now(s), reason
            for pending in s.scalars(
                select(m.Evaluation).where(
                    m.Evaluation.board == row.board, m.Evaluation.state == "pending"
                )
            ):
                cancel(s, pending)
            s.flush()
            self._publish(s, config)
            board = row.board
        get_events().warning(
            "promotion_disqualified", promotion_id=promotion_id, board=board, reason=reason
        )

    def set_disqualification(self, source_hash, active, reason, actor, config):
        """Operator decision serialized with all coordinator writes, across every board."""
        from config import sha

        sha(source_hash)
        if not reason.strip() or not actor.strip():
            raise ValueError("operator and reason are required")
        with session_scope(self.sessions) as s:
            state = s.get(m.CoordinatorState, 1, with_for_update=True)
            if state is None or state.config_hash != digest(config.public()):
                raise ValueError("configuration must match the active coordinator")
            if s.get(m.Source, source_hash) is None:
                raise ValueError("unknown source")
            row = s.get(m.SourceDisqualification, source_hash)
            record = {
                "active": active,
                "reason": reason,
                "actor": actor,
                "at": db_now(s).isoformat(),
            }
            if row is None:
                row = m.SourceDisqualification(source_hash=source_hash, history=[])
                s.add(row)
            row.active, row.reason, row.actor = active, reason, actor
            row.history = row.history + [record]
            row.updated_at = db_now(s)
            s.flush()
            if active:
                for evaluation in s.scalars(
                    select(m.Evaluation)
                    .join(m.Submission)
                    .where(
                        m.Evaluation.state == "pending",
                        m.Submission.source_sha256 == source_hash,
                    )
                ):
                    cancel(s, evaluation)
            s.flush()
            self._publish(s, config)
        get_events().warning(
            "source_disqualification_set",
            source_hash=source_hash,
            active=active,
            reason=reason,
            actor=actor,
        )

    def publish(self, config):
        with session_scope(self.sessions) as s:
            self._lead(s)
            self._publish(s, config)

    def _publish(self, s, config):
        boards = {}
        for board in config.boards:
            paused = bool(
                s.scalar(
                    select(m.BenchmarkJob.id)
                    .join(m.EvaluationJob)
                    .join(m.Evaluation)
                    .where(
                        m.Evaluation.board == board,
                        m.Evaluation.state == "pending",
                        m.BenchmarkJob.state.in_(("queued", "running")),
                        m.BenchmarkJob.error["kind"].astext.in_(("reference", "checksum")),
                    )
                    .limit(1)
                )
            )
            boards[board] = {
                "config_hash": config.identity(board),
                "initial_reference": config.boards[board]["reference"]["sha256"],
                "paused": paused,
                "kings": [
                    {"promotion_id": p.id, "submission_id": sid, "hotkey": hotkey}
                    for p, sid, hotkey in history(s, board, limit=2)
                ],
            }
        publication = {"version": "tensor-top2-v1", "rewards": config.rewards, "boards": boards}
        row = s.get(m.CoordinatorState, 1)
        if row.publication != publication:
            row.publication, row.publication_hash = publication, digest(publication)

    def alerts(self, age):
        with session_scope(self.sessions) as s:
            now = db_now(s)
            rows = s.scalars(
                select(m.BenchmarkJob).where(
                    m.BenchmarkJob.state.in_(("queued", "running")),
                    or_(
                        m.BenchmarkJob.created_at < now - dt.timedelta(seconds=age),
                        m.BenchmarkJob.lease_until < now,
                        m.BenchmarkJob.attempts >= 3,
                    ),
                )
            )
            return [
                {
                    "job": j.id,
                    "environment": j.environment,
                    "state": j.state,
                    "attempts": j.attempts,
                    "error": j.error,
                }
                for j in rows
            ]
