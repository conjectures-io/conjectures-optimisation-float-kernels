"""The gate worker: claim a submission, run gate.py over it, record the verdict it wrote."""

from __future__ import annotations

import fcntl
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import cast

from loguru import logger

import admission
import db
import log
from axiom import config_error, get_events, init
from db import SubmissionState, models
from db.drift import observe

from . import storage
from .settings import PROMOTION_BAR, Settings, load

VALIDATOR = Path(__file__).resolve().parent.parent
GATE = VALIDATOR.parent / "gate.py"
# The lock gate.py takes for every run: the worker holds it from before a claim to the verdict.
GATE_LOCK = VALIDATOR.parent / "gate.lock"

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
        "champion_sha256": v.get("champion_sha256") or None,
        "verifier_fingerprint": v.get("verifier_fingerprint") or None,
        "gate_seconds": v.get("seconds"),
        "report": verdict.read_text(),
        # Not a column: a reference failure pauses its slot without charging either submission.
        "incumbent_failed": v.get("incumbent_failed") is True,
        "reference_failure_phase": v.get("reference_failure_phase"),
        "reference_complete": v.get("reference_complete") is True,
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


def champion_env(store: db.Store, settings: Settings, sub: models.Submission) -> dict[str, str]:
    # Points the gate at the champion `sub` must beat, written where its pin can find it; empty
    # while the shipped champion holds, so the gate's own default applies.
    slot = sub.slot
    kernel = store.submissions.champion_kernel(slot, before=sub.id)
    if kernel is None:
        return {}
    path = settings.files / "champions" / slot / "Kernel.lean"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.is_file() or path.read_bytes() != kernel:
        path.write_bytes(kernel)
    return {"GATE_INCUMBENT" if slot == "add" else f"GATE_INCUMBENT_{slot.upper()}": str(path)}


def beats_champion(sub: models.Submission, fields: dict[str, object]) -> bool:
    # Whether an accepted verdict beat the champion it was timed against, so it is promoted.
    rounds = cast("list[list[float]]", fields.get("rounds") or [])
    evidence = admission.Evidence(
        sub.id,
        sub.hotkey,
        float(cast("float", fields.get("ratio") or 0.0)),
        sub.submitted_at,
        tuple((float(i), float(s)) for i, s in rounds),
    )
    return admission.assess(evidence, PROMOTION_BAR).outcome == "qualifies"


def run_gate(
    submission_dir: Path, verdict: Path, slot: str = "add", env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    # Run gate.py over one submission directory in its slot, capped; a timeout rejects.
    cmd = [sys.executable, str(GATE), str(submission_dir), "--verdict", str(verdict)]
    cmd += ["--slot", slot]
    progress_path = verdict.with_suffix(".progress.json")
    with subprocess.Popen(
        cmd,
        cwd=GATE.parent,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={
            **os.environ,
            **(env or {}),
            "GATE_LOCK_HELD": "1",
            "GATE_PROGRESS_PATH": str(progress_path),
        },
        start_new_session=True,
    ) as process:
        try:
            stdout, stderr = process.communicate(timeout=TOTAL_TIMEOUT)
            if process.returncode != 0 and not verdict.exists() and progress_path.exists():
                progress = json.loads(progress_path.read_text())
                if str(progress.get("phase", "")).startswith("reference-"):
                    verdict.write_text(
                        json.dumps(
                            {
                                "incumbent_failed": True,
                                "reference_failure_phase": progress["phase"],
                                "champion_sha256": progress.get("champion_sha256"),
                                "stage": progress["phase"],
                                "reason": (
                                    "reference process ended without a verdict "
                                    f"(exit {process.returncode})"
                                ),
                                "passed": False,
                            }
                        )
                    )
            return subprocess.CompletedProcess(cmd, process.returncode, stdout, stderr)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                stdout, stderr = process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                stdout, stderr = process.communicate()
            progress = json.loads(progress_path.read_text()) if progress_path.exists() else {}
            reference_failed = str(progress.get("phase", "")).startswith("reference-")
            verdict.write_text(
                json.dumps(
                    {
                        "incumbent_failed": reference_failed,
                        "reference_failure_phase": progress.get("phase")
                        if reference_failed
                        else None,
                        "champion_sha256": progress.get("champion_sha256"),
                        "reference_complete": progress.get("reference_complete", False),
                        "stage": "timeout",
                        "reason": f"the gate did not finish within {TOTAL_TIMEOUT:.0f}s",
                        "passed": False,
                    }
                )
            )
            return subprocess.CompletedProcess(cmd, 2 if reference_failed else 1, stdout, stderr)


def score_one(store: db.Store, settings: Settings, sub: models.Submission) -> str:
    # Gate one claimed submission: a run with no verdict or an undefined exit counts toward the
    # error cap, uncharged. Reference failures instead pause only their slot and never demote.
    logger.info(f"[worker] verifying submission {sub.id} slot={sub.slot} ({sub.hotkey[:8]}…)")
    events = get_events()
    who = {"submission_id": sub.id, "hotkey": sub.hotkey, "slot": sub.slot}
    pause = store.pauses.all().get(sub.slot)
    reference = store.submissions.champion(sub.slot, before=sub.id)
    with tempfile.TemporaryDirectory(prefix=f"gate-{sub.id}-") as tmp:
        verdict = Path(tmp) / "verdict.json"
        result = run_gate(
            materialize(store, settings, sub.id, Path(tmp)),
            verdict,
            sub.slot,
            champion_env(store, settings, sub),
        )
        fields = recorded(verdict)
    incumbent_failed = bool(fields.pop("incumbent_failed", False))
    failure_phase = fields.pop("reference_failure_phase", None)
    reference_complete = fields.pop("reference_complete", False)
    if incumbent_failed:
        phase = failure_phase or "reference-benchmark"
        reason = str(fields.get("reason") or "reference failed")
        if store.pauses.fail(
            sub,
            reference_id=reference.id if reference else None,
            reference_sha256=fields.get("champion_sha256"),
            phase=phase,
            reason=reason,
            retry_seconds=settings.champion_retry_seconds,
        ):
            # One error event per failed attempt, so an alert fires on entry and repeats while down.
            events.error(
                "slot_paused",
                **who,
                phase=phase,
                reason=reason[:2000],
                reference_id=reference.id if reference else None,
                champion_sha256=fields.get("champion_sha256"),
                first=pause is None,
                paused_since=pause.paused_at.isoformat() if pause else None,
                retry_seconds=settings.champion_retry_seconds,
            )
        return "paused"
    if reference_complete:
        if store.pauses.recover(sub, pause.revision if pause else None) and pause is not None:
            events.info(
                "slot_recovered",
                **who,
                phase=pause.phase,
                paused_since=pause.paused_at.isoformat(),
            )
    else:
        store.pauses.defer(sub, pause.revision if pause else None, settings.champion_retry_seconds)
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
            reason=fields.get("reason") or f"no verdict (exit {result.returncode})",
            exit_code=result.returncode,
        )
        return SubmissionState.ERROR.value

    promote = state is SubmissionState.ACCEPTED and beats_champion(sub, fields)
    final = store.submissions.finish(
        sub.id,
        state,
        promote=promote,
        expected_attempt=sub.verification_attempt,
        exit_code=result.returncode,
        **fields,
    )
    provenance = {
        "verifier_fingerprint": fields.get("verifier_fingerprint"),
        "incumbent_sha256": fields.get("incumbent_sha256"),
        "champion_sha256": fields.get("champion_sha256"),
    }
    if any(provenance.values()):
        # A verdict written by the worker itself (a timeout) carries no provenance to observe.
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


@contextmanager
def gate_slot() -> Iterator[bool]:
    # The gate's lock, taken without waiting: False while an operator run or another worker has it.
    with open(GATE_LOCK, "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _drain(store: db.Store, settings: Settings) -> tuple[int, bool]:
    # `drain`, also saying whether it stopped on a validator error; a busy gate claims nothing, so
    # waiting for it never counts against a submission's time limit.
    done = 0
    prefer_healthy = True
    while True:
        with gate_slot() as free:
            if not free:
                logger.debug("[worker] the gate is busy (an operator run or another worker)")
                return done, False
            pauses = store.pauses.all()
            sub = store.submissions.claim_next(
                settings.worker_id,
                prefer_healthy=prefer_healthy,
                retry_seconds=settings.champion_retry_seconds,
            )
            if sub is None:
                return done, False
            get_events().info(
                "submission_claimed",
                submission_id=sub.id,
                hotkey=sub.hotkey,
                slot=sub.slot,
                worker_id=settings.worker_id,
            )
            prefer_healthy = sub.slot in pauses
            outcome = score_one(store, settings, sub)
            if outcome == "paused":
                prefer_healthy = True
                continue
            if outcome == SubmissionState.ERROR.value:
                return done, True
        done += 1


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
