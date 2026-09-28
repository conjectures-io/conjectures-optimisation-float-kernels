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

import db
from db import SubmissionState, models
from loguru import logger

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
        "seed": v.get("seed") or None,
        "incumbent_sha256": v.get("incumbent_sha256") or None,
        "gate_seconds": v.get("seconds"),
    }


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
    # Gate one claimed submission. No verdict (the gate failed before reaching it) requeues it
    # uncharged; a verdict with an undefined exit (a stage crashed on it) counts toward the cap.
    logger.info(f"[worker] verifying submission {sub.id} slot={sub.slot} ({sub.hotkey[:8]}…)")
    with tempfile.TemporaryDirectory(prefix=f"gate-{sub.id}-") as tmp:
        verdict = Path(tmp) / "verdict.json"
        result = run_gate(settings.submission_dir(sub.id), verdict, sub.slot)
        fields = recorded(verdict)
    state = STATE_OF_EXIT.get(result.returncode)

    if state is None or not fields:
        logger.warning(
            f"[worker] validator error on submission {sub.id} (exit {result.returncode}):\n"
            f"{result.stdout[-1500:]}\n{result.stderr[-1500:]}"
        )
        if not fields:
            store.submissions.requeue(sub.id, expected_attempt=sub.verification_attempt)
        else:
            store.submissions.gate_crashed(
                sub.id,
                max_errors=settings.max_gate_errors,
                expected_attempt=sub.verification_attempt,
                exit_code=result.returncode,
                **fields,
            )
        return SubmissionState.ERROR.value

    final = store.submissions.finish(
        sub.id,
        state,
        expected_attempt=sub.verification_attempt,
        exit_code=result.returncode,
        **fields,
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
        if score_one(store, settings, sub) == SubmissionState.ERROR.value:
            return done, True
        done += 1
    return done, False


def sweep(store: db.Store, settings: Settings) -> None:
    # Between drains: reclaim abandoned claims and prune dead rate-limit windows.
    if requeued := store.submissions.requeue_stale(settings.stale_claim_seconds):
        logger.info(f"[worker] requeued {requeued} submission(s) abandoned mid-gate")
    store.rate.prune()


def backoff(errors: int) -> float:
    # The pause after `errors` consecutive validator errors: doubling from idle, capped.
    # The exponent is capped too: a days-long outage must not overflow the float.
    return min(IDLE_SECONDS * 2 ** min(errors, 16), MAX_BACKOFF_SECONDS)


def run_forever(store: db.Store, settings: Settings) -> None:
    # Sweep, drain, sleep when idle (longer after validator errors), never die on one bad turn.
    logger.info(f"[worker] {settings.worker_id} draining the queue")
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
    settings = load()
    store = db.connect()
    try:
        run_forever(store, settings)
    except KeyboardInterrupt:
        logger.info("[worker] interrupted")
    finally:
        store.close()


if __name__ == "__main__":
    main()
