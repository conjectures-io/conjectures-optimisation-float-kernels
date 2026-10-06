"""The endpoints: submit a signed Kernel.lean, read a submission and its report, read the board.

Laid out like the conjectures platform's competition surface (`/v1/competitions/<slug>/...`, the
signature in `X-Conjectures-*` headers), so one miner client talks to either.
"""

from __future__ import annotations

from typing import cast

from fastapi import APIRouter, File, Header, HTTPException, Request, UploadFile
from loguru import logger

import admission
import db
from db import SLOTS, SubmissionState, iso, models
from db.verifier import required_fingerprint

from . import schemas, security, sig
from .settings import MAX_FILE_BYTES, PROMOTION_BAR, Settings

router = APIRouter()
# Every competition endpoint, as the conjectures platform lays them out: one slug per slot.
PREFIX = "/v1/competitions/{competition}"


def refuse(code: int, reason: str) -> HTTPException:
    # Every rejected write is a 4xx with a one-line reason, and nothing else.
    return HTTPException(status_code=code, detail={"reason": reason})


def _store(request: Request) -> db.Store:
    # app.state is untyped; naming the store once keeps every handler typed instead of Any.
    return cast("db.Store", request.app.state.store)  # pyright: ignore[reportAny]


def _settings(request: Request) -> Settings:
    # The settings the app was built with.
    return cast("Settings", request.app.state.settings)  # pyright: ignore[reportAny]


def _limit(request: Request, subject: str, what: str) -> None:
    # Count one write against the subject's window and refuse with a 429 when it is full.
    allowed, hits = _store(request).rate.hit(subject)
    if not allowed:
        limit = _store(request).rate.limit
        logger.warning(f"[api] rate limit: {what} at {hits} writes in the window")
        raise refuse(429, f"more than {limit} writes a minute from this {what}")


def _client(request: Request) -> str:
    # The direct peer (the proxy, if any); bounds unsigned floods, the hotkey bucket bounds miners.
    return request.client.host if request.client else "unknown"


def submission_view(row: models.Submission) -> schemas.SubmissionView:
    # A submission row as the miner sees it.
    return schemas.SubmissionView(
        id=row.id,
        hotkey=row.hotkey,
        slot=row.slot,
        digest=row.digest,
        submitted_at=iso(row.submitted_at),
        state=row.state,
        exit_code=row.exit_code,
        stage=row.stage,
        reason=row.reason,
        ratio=row.ratio,
        classes=row.classes,
        seed=row.seed,
        incumbent_sha256=row.incumbent_sha256,
        verifier_fingerprint=row.verifier_fingerprint,
        verification_current=(
            row.verifier_fingerprint == required_fingerprint()
            if row.state == SubmissionState.ACCEPTED.value
            else None
        ),
    )


@router.get("/health", response_model=schemas.Health)
def health(request: Request) -> schemas.Health:
    # Liveness and queue length (null if the store is down) - it must not fail with the store.
    try:
        queued = _store(request).submissions.queue_depth()
    except Exception as exc:  # noqa: BLE001 - liveness must not depend on the store
        logger.warning(f"[api] health without queue length: {exc}")
        queued = None
    return schemas.Health(ok=True, queued=queued, promotion_bar=PROMOTION_BAR)


@router.get("/ready", response_model=schemas.Ready)
def ready(request: Request) -> schemas.Ready:
    # Readiness: 503 when the database is unreachable, so a load balancer stops routing here.
    store = _store(request)
    try:
        database = store.ping()
        queued = store.submissions.queue_depth()
    except Exception as exc:  # noqa: BLE001 - any store failure is "not ready"
        logger.warning(f"[api] not ready: {exc}")
        raise HTTPException(503, {"reason": "the store is not reachable"}) from exc
    return schemas.Ready(ok=True, queued=queued, promotion_bar=PROMOTION_BAR, database=database)


def _slot(competition: str) -> str:
    # The slot a competition slug names; any other slug is not served here.
    slot = sig.slot_of(competition)
    if slot is None or slot not in SLOTS:
        raise HTTPException(404, {"reason": f"no competition {competition!r} here"})
    return slot


@router.post(f"{PREFIX}/submissions", response_model=schemas.SubmitAccepted)
async def submit(
    request: Request,
    competition: str,
    hotkey: str = Header(..., alias="X-Conjectures-Hotkey"),
    signature: str = Header(..., alias="X-Conjectures-Signature"),
    timestamp: int = Header(..., alias="X-Conjectures-Timestamp"),
    kernel: UploadFile = File(..., alias="Kernel.lean"),
) -> schemas.SubmitAccepted:
    # Accept a signed Kernel.lean, cheapest checks first; resubmitting a file returns its id.
    # Signed fields travel as headers, as on the platform: a header-only request is not one a
    # browser can send cross-site, so no page can submit with a miner's leaked signature.
    slot = _slot(competition)
    store, settings = _store(request), _settings(request)
    _limit(request, f"ip:{_client(request)}", "address")
    try:
        security.check_hotkey(hotkey)
        security.check_signature(signature)
    except security.Invalid as exc:
        raise refuse(400, str(exc)) from exc

    _limit(request, f"hotkey:{hotkey}", "hotkey")

    try:
        security.check_fresh(timestamp, settings.signature_window_seconds)
    except security.Invalid as exc:
        raise refuse(401, str(exc)) from exc

    lean = await kernel.read(MAX_FILE_BYTES + 1)
    if len(lean) > MAX_FILE_BYTES:
        raise refuse(413, f"Kernel.lean must be at most {MAX_FILE_BYTES} bytes")
    if not lean:
        raise refuse(400, "Kernel.lean must be non-empty")
    try:
        lean.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise refuse(400, "Kernel.lean must be UTF-8 text") from exc

    digest = sig.digest_of(lean)
    message = sig.submit_message(
        competition=competition, digest=digest, hotkey=hotkey, timestamp=timestamp
    )
    if not sig.verify(hotkey, message, signature):
        raise refuse(401, "signature does not verify against the hotkey for this file and slot")

    # A slot is required here but spent only on acceptance, so a rejection costs nothing.
    if not store.registrations.is_registered(hotkey):
        raise refuse(402, "this hotkey is not registered on the subnet")
    allowed, slots, pending = store.submissions.may_queue(hotkey)
    if not allowed:
        raise refuse(
            402,
            f"{slots} unclaimed registration(s) and {pending} already in the queue: "
            "one registration buys one accepted submission, so register again to submit again",
        )

    sub_id, fresh = store.submissions.add(hotkey, digest, slot, kernel=lean)
    row = store.submissions.get(sub_id)
    assert row is not None, "add() either inserted this row or found the existing one"
    logger.info(
        f"[api] submit hotkey={hotkey[:8]}… slot={slot} {digest[:16]} -> {sub_id} {row.state} "
        f"({'new' if fresh else 'repeat'}, {slots} slot(s))"
    )
    return schemas.SubmitAccepted(
        competition=competition,
        submission=sub_id,
        slot=slot,
        state=row.state,
        digest=digest,
        created=fresh,
        slots_remaining=slots,
    )


def _row(request: Request, competition: str, sub_id: int) -> models.Submission:
    # One submission of this competition slug's slot, or a 404.
    slot = _slot(competition)
    row = _store(request).submissions.get(sub_id)
    if row is None or row.slot != slot:
        raise HTTPException(404, {"reason": "no such submission"})
    return row


@router.get(f"{PREFIX}/submissions/{{sub_id}}", response_model=schemas.SubmissionView)
def submission(request: Request, competition: str, sub_id: int) -> schemas.SubmissionView:
    # One submission's state and verdict; public, so a miner can read why it was refused.
    return submission_view(_row(request, competition, sub_id))


@router.get(f"{PREFIX}/submissions/{{sub_id}}/report", response_model=schemas.Report)
def report(request: Request, competition: str, sub_id: int) -> schemas.Report:
    # The gate's full verdict JSON, its own endpoint since no listing should carry it.
    return schemas.Report(report=_row(request, competition, sub_id).report)


@router.get(f"{PREFIX}/leaderboard", response_model=schemas.Leaderboard)
def leaderboard(request: Request, competition: str) -> schemas.Leaderboard:
    # One slot's board: every hotkey's best accepted ratio vs its incumbent, fastest first.
    slot = _slot(competition)
    store = _store(request)
    rows = store.submissions.leaderboard(slot)
    decisions = store.scoring.admission(slot, PROMOTION_BAR)
    outcome = {d.submission_id: d.outcome for d in decisions}
    return schemas.Leaderboard(
        slot=slot,
        incumbent_sha256=store.submissions.current_incumbent(slot),
        verifier_fingerprint=required_fingerprint(),
        promotion_bar=PROMOTION_BAR,
        leader=admission.leader(decisions),
        ranking=[
            schemas.Ranking(
                rank=i + 1,
                submission=row.id,
                hotkey=row.hotkey,
                # leaderboard() selects only accepted rows with a ratio.
                ratio=row.ratio or 0.0,
                qualifies=(row.ratio or 1.0) <= PROMOTION_BAR,
                admission=outcome.get(row.id),
                submitted_at=iso(row.submitted_at),
            )
            for i, row in enumerate(rows)
        ],
    )
