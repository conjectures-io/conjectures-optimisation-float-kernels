"""The gate worker: claim a submission, run gate.py over it, record the verdict it wrote."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import cast

from loguru import logger

import db
import log
from axiom import config_error, get_events, init
from db import SubmissionState, models
from db.drift import observe

from . import storage
from .settings import Settings, load

VALIDATOR = Path(__file__).resolve().parent.parent
GATE = VALIDATOR.parent / "gate.py"

# The whole gate, every stage; a typical run takes under a minute.
TOTAL_TIMEOUT = float(os.environ.get("VERIFY_TOTAL_TIMEOUT", "1800"))

STATE_OF_EXIT = {0: SubmissionState.ACCEPTED, 1: SubmissionState.REJECTED}
IDLE_SECONDS = 2.0
# The longest pause after consecutive validator errors, so an outage does not spin the gate.
MAX_BACKOFF_SECONDS = 300.0
# How often to sweep stale claims and expired rate-limit windows, in idle turns.
SWEEP_EVERY = 30


def recorded(verdict: Path) -> dict[str, object]:
    # The columns a verdict fills in; empty when the gate wrote none, which is a validator error.
    if not verdict.is_file():
        return {}
    v = cast("dict[str, object]", json.loads(verdict.read_text()))
    ratio = v.get("ratio")
    return {
        "stage": v.get("stage"),
        "reason": v.get("reason") or None,
        "ratio": float(ratio) if isinstance(ratio, (int, float)) else None,
        "classes": v.get("classes") or None,
        "rounds": v.get("rounds") or None,
        "seed": v.get("seed") or None,
        "incumbent_sha256": v.get("incumbent_sha256") or None,
        "verifier_fingerprint": v.get("verifier_fingerprint") or None,
        "gate_seconds": v.get("seconds"),
        "report": verdict.read_text(),
        # Not a column: whether the rejection is charged, read off by score_one.
        "exhausted": v.get("exhausted") is True,
    }


def materialize(store: db.Store, settings: Settings, sub_id: int, tmp: Path) -> Path:
    # The directory the gate reads: Kernel.lean from the store, written under `tmp`; a submission
    # queued before the store kept files still has its copy under SERVICE_FILES.
    kernel = store.submissions.kernel(sub_id)
    if kernel is None:
        return settings.submission_dir(sub_id)
    directory = tmp / str(sub_id)
    storage.write_submission(directory, kernel)
    return directory


def run_gate(
    submission_dir: Path, verdict: Path, slot: str = "add"
) -> subprocess.CompletedProcess[str]:
    # Run gate.py over one submission directory in its slot, capped; a timeout rejects.
    cmd = [sys.executable, str(GATE), str(submission_dir), "--verdict", str(verdict)]
    cmd += ["--slot", slot]
    with subprocess.Popen(
        cmd,
        cwd=GATE.parent,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=dict(os.environ),
        start_new_session=True,
    ) as process:
        try:
            stdout, stderr = process.communicate(timeout=TOTAL_TIMEOUT)
            return subprocess.CompletedProcess(cmd, process.returncode, stdout, stderr)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                stdout, stderr = process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                stdout, stderr = process.communicate()
            verdict.write_text(
                json.dumps(
                    {
                        "stage": "timeout",
                        "reason": f"the gate did not finish within {TOTAL_TIMEOUT:.0f}s",
                        "passed": False,
                    }
                )
            )
            return subprocess.CompletedProcess(cmd, 1, stdout, stderr)


def score_one(store: db.Store, settings: Settings, sub: models.Submission) -> str:
    # Gate one claimed submission: no verdict requeues it uncharged, and a verdict with an undefined
    # exit (a stage crashed on it) counts toward the error cap.
    logger.info(f"[worker] verifying submission {sub.id} slot={sub.slot} ({sub.hotkey[:8]}…)")
    events = get_events()
    who = {"submission_id": sub.id, "hotkey": sub.hotkey, "slot": sub.slot}
    with tempfile.TemporaryDirectory(prefix=f"gate-{sub.id}-") as tmp:
        verdict = Path(tmp) / "verdict.json"
        result = run_gate(materialize(store, settings, sub.id, Path(tmp)), verdict, sub.slot)
        fields = recorded(verdict)
    charge = bool(fields.pop("exhausted", False))
    state = STATE_OF_EXIT.get(result.returncode)

    if state is None or not fields:
        logger.warning(
            f"[worker] validator error on submission {sub.id} (exit {result.returncode}):\n"
            f"{result.stdout[-1500:]}\n{result.stderr[-1500:]}"
        )
        events.error(
            "gate_validator_error",
            submission_id=sub.id,
            exit_code=result.returncode,
            error=result.stderr[-1500:] or result.stdout[-1500:],
        )
        if not fields:
            store.submissions.requeue(sub.id, expected_attempt=sub.verification_attempt)
            events.warning(
                "submission_requeued",
                submission_id=sub.id,
                reason=f"validator error (exit {result.returncode}), no verdict",
            )
        else:
            state_now = store.submissions.gate_crashed(
                sub.id,
                max_errors=settings.max_gate_errors,
                expected_attempt=sub.verification_attempt,
                exit_code=result.returncode,
                **fields,
            )
            events.error(
                "gate_verdict",
                **who,
                state=state_now,
                stage=fields.get("stage"),
                reason=fields.get("reason"),
                exit_code=result.returncode,
            )
        return SubmissionState.ERROR.value

    final = store.submissions.finish(
        sub.id,
        state,
        charge=charge,
        expected_attempt=sub.verification_attempt,
        exit_code=result.returncode,
        **fields,
    )
    provenance = {
        "verifier_fingerprint": fields.get("verifier_fingerprint"),
        "incumbent_sha256": fields.get("incumbent_sha256"),
    }
    observe(store.sessions, sub.slot, provenance)
    end = json.loads(str(fields["report"])).get("verifier_fingerprint_end")
    if end and end != provenance["verifier_fingerprint"]:
        # Forward a mid-run transition even on the first observation in this database.
        observe(store.sessions, sub.slot, {**provenance, "verifier_fingerprint": end})
    events.emit(
        "info" if final == SubmissionState.ACCEPTED.value else "warning",
        "gate_verdict",
        **who,
        state=final,
        stage=fields.get("stage"),
        reason=fields.get("reason"),
        ratio=fields.get("ratio"),
        exit_code=result.returncode,
        duration_seconds=fields.get("gate_seconds"),
    )
    logger.info(
        f"[worker] submission {sub.id} {final} stage={fields['stage']} ratio={fields['ratio']}"
    )
    return final


def drain(store: db.Store, settings: Settings) -> int:
    # Verify every claimable submission in arrival order; stop on a validator error.
    return _drain(store, settings)[0]


def _drain(store: db.Store, settings: Settings) -> tuple[int, bool]:
    # `drain`, also saying whether it stopped on a validator error.
    done = 0
    while (sub := store.submissions.claim_next(settings.worker_id)) is not None:
        get_events().info(
            "submission_claimed",
            submission_id=sub.id,
            hotkey=sub.hotkey,
            slot=sub.slot,
            worker_id=settings.worker_id,
        )
        if score_one(store, settings, sub) == SubmissionState.ERROR.value:
            return done, True
        done += 1
    return done, False


def sweep(store: db.Store, settings: Settings) -> None:
    # Between drains: reclaim abandoned claims and prune dead rate-limit windows.
    if requeued := store.submissions.requeue_stale(settings.stale_claim_seconds):
        logger.info(f"[worker] requeued {requeued} submission(s) abandoned mid-gate")
        get_events().warning("submission_requeued", count=requeued, reason="stale claim")
    store.rate.prune()


def backoff(errors: int) -> float:
    # The pause after `errors` consecutive validator errors, doubling from idle up to a cap, with
    # the exponent capped too so a days-long outage cannot overflow the float.
    return min(IDLE_SECONDS * 2 ** min(errors, 16), MAX_BACKOFF_SECONDS)


def run_forever(store: db.Store, settings: Settings) -> None:
    # Sweep, drain, sleep when idle (longer after validator errors), never die on one bad turn.
    logger.info(f"[worker] {settings.worker_id} draining the queue")
    if interrupted := store.submissions.requeue_interrupted(settings.worker_id):
        logger.info(f"[worker] requeued {interrupted} submission(s) this worker left mid-gate")
        get_events().warning(
            "submission_requeued", count=interrupted, reason="interrupted by a worker restart"
        )
    turn = errors = 0
    while True:
        try:
            if turn % SWEEP_EVERY == 0:
                sweep(store, settings)
            _, errored = _drain(store, settings)
            errors = errors + 1 if errored else 0
            turn += 1
        except Exception:  # noqa: BLE001 - a database or subprocess blip must not end the loop
            logger.exception("[worker] loop error")
        time.sleep(backoff(errors))


def main() -> None:
    # Run the gate worker until interrupted, closing the store on the way out.
    log.setup()
    events = init("competition-gate-worker")
    try:
        settings = load()
    except Exception as exc:
        events.error("service_misconfigured", error=config_error(exc))
        raise
    store = db.connect()
    events.info(
        "service_started",
        worker_id=settings.worker_id,
        total_timeout_seconds=TOTAL_TIMEOUT,
        stale_claim_seconds=settings.stale_claim_seconds,
    )
    reason = "crashed"
    try:
        run_forever(store, settings)
    except KeyboardInterrupt:
        reason = "interrupted"
        logger.info("[worker] interrupted")
    finally:
        events.info("service_stopped", reason=reason)
        store.close()


if __name__ == "__main__":
    main()
