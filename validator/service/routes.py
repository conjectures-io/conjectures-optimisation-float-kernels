"""The endpoints: submit a signed Kernel.lean, read a submission, read the board."""

from __future__ import annotations

from typing import cast

import db
from db import iso, models
from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from loguru import logger

from . import schemas, security, sig, storage
from .settings import MAX_FILE_BYTES, PROMOTION_BAR, Settings

router = APIRouter()


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


@router.post("/submit", response_model=schemas.SubmitAccepted)
async def submit(
    request: Request,
    hotkey: str = Form(...),
    signature: str = Form(...),
    timestamp: int = Form(...),
    kernel: UploadFile = File(..., alias="Kernel.lean"),
    slot: str = Form("add"),
) -> schemas.SubmitAccepted:
    # Accept a signed Kernel.lean, cheapest checks first; resubmitting a file returns its id.
    store, settings = _store(request), _settings(request)
    _limit(request, f"ip:{_client(request)}", "address")
    try:
        security.check_slot(slot)
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

    digest = sig.digest_of(lean)
    if not sig.verify(hotkey, sig.submit_message(digest, hotkey, timestamp, slot), signature):
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

    sub_id, fresh = store.submissions.add(hotkey, digest, slot)
    if fresh:
        storage.write_submission(settings.submission_dir(sub_id), lean)
    row = store.submissions.get(sub_id)
    assert row is not None, "add() either inserted this row or found the existing one"
    logger.info(
        f"[api] submit hotkey={hotkey[:8]}… slot={slot} {digest[:16]} -> {sub_id} {row.state} "
        f"({'new' if fresh else 'repeat'}, {slots} slot(s))"
    )
    return schemas.SubmitAccepted(
        submission=sub_id, slot=slot, state=row.state, digest=digest, slots_remaining=slots
    )


@router.get("/submissions/{sub_id}", response_model=schemas.SubmissionView)
def submission(request: Request, sub_id: int) -> schemas.SubmissionView:
    # One submission's state and verdict; public, so a miner can read why it was refused.
    row = _store(request).submissions.get(sub_id)
    if row is None:
        raise HTTPException(404, {"reason": "no such submission"})
    return submission_view(row)


@router.get("/leaderboard", response_model=schemas.Leaderboard)
def leaderboard(request: Request, slot: str = "add") -> schemas.Leaderboard:
    # One slot's board: every hotkey's best accepted ratio vs its incumbent, fastest first.
    try:
        security.check_slot(slot)
    except security.Invalid as exc:
        raise refuse(400, str(exc)) from exc
    store = _store(request)
    rows = store.submissions.leaderboard(slot)
    return schemas.Leaderboard(
        slot=slot,
        incumbent_sha256=store.submissions.current_incumbent(slot),
        promotion_bar=PROMOTION_BAR,
        ranking=[
            schemas.Ranking(
                rank=i + 1,
                submission=row.id,
                hotkey=row.hotkey,
                # leaderboard() selects only accepted rows with a ratio.
                ratio=row.ratio or 0.0,
                qualifies=(row.ratio or 1.0) <= PROMOTION_BAR,
                submitted_at=iso(row.submitted_at),
            )
            for i, row in enumerate(rows)
        ],
    )
