"""Registrations and the slots they buy: one registration, one accepted submission."""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Iterable
from typing import Protocol, cast

from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from axiom import get_events

from . import models
from .engine import session_scope

# Log backfill progress this often, or a first load of the subnet looks hung.
PROGRESS_EVERY = 25


class Neuron(Protocol):
    # The four neuron fields this module reads, structural so tests can pass a plain stub.

    uid: int
    hotkey: str
    coldkey: str
    block_at_registration: int


def changed_rows(
    latest: dict[int, tuple[str, str]], neurons: Iterable[Neuron]
) -> list[dict[str, object]]:
    # The rows to insert: only uids whose (hot, cold) pair moved, stamped with their own block.
    return [
        {
            "uid": n.uid,
            "ss58_hot": n.hotkey,
            "ss58_cold": n.coldkey,
            "block": n.block_at_registration,
        }
        for n in neurons
        if latest.get(n.uid) != (n.hotkey, n.coldkey)
    ]


def resolve_block_dates(
    rows: list[dict[str, object]], block_time: Callable[[int], dt.datetime], *, scope: str
) -> None:
    # Fill block_date for the changed rows only, so a quiet tick makes no archive lookups.
    total = len(rows)
    logger.info(f"[registrations] resolving {total} registration timestamp(s) [{scope}]")
    for i, row in enumerate(rows, start=1):
        row["block_date"] = block_time(cast(int, row["block"]))
        if total >= PROGRESS_EVERY and (i % PROGRESS_EVERY == 0 or i == total):
            logger.info(f"[registrations] resolved {i}/{total} registration timestamps")


class NoSlot(Exception):
    # Raised when a hotkey has no unclaimed registration left to spend.
    pass


class RegistrationsDb:
    # Recorded registrations, and the slots they buy.
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        # Holds only the session factory.
        self._sessions = sessions

    # --- the chain watcher's seam --------------------------------------------
    def publish_snapshot(self, head, metagraph, block_time: Callable[[int], dt.datetime]) -> int:
        # Append a row per uid whose key pair changed; returns how many, zero on a quiet block.
        del head  # the watcher's publish contract passes it; the rows are stamped from the neurons
        if not metagraph.neurons:
            logger.warning(
                f"[registrations] block {metagraph.block}: empty metagraph, nothing to publish"
            )
            return 0

        with session_scope(self._sessions) as session:
            latest = self._latest_keys(session)
            rows = changed_rows(latest, metagraph.neurons)
            if not rows:
                logger.info(
                    f"[registrations] block {metagraph.block}: no registration changes "
                    f"({len(metagraph.neurons)} neurons)"
                )
                return 0
            scope = "initial load" if not latest else "update"
            resolve_block_dates(rows, block_time, scope=scope)
            # Do nothing on conflict: this uid's block is already recorded and the history stands.
            session.execute(
                insert(models.Registration)
                .values(rows)
                .on_conflict_do_nothing(index_elements=["uid", "block"])
            )
            uids = sorted(cast(int, r["uid"]) for r in rows)
            logger.info(
                f"[registrations] block {metagraph.block}: recorded {len(rows)} registration "
                f"change(s) [{scope}] for uid(s) {uids}"
            )
        get_events().info(
            "registrations_recorded",
            count=len(rows),
            initial_load=not latest,
            block=metagraph.block,
            uids=uids,
        )
        return len(rows)

    @staticmethod
    def _latest_keys(session: Session) -> dict[int, tuple[str, str]]:
        # Each uid's newest recorded (hot, cold) pair, which a snapshot is diffed against.
        stmt = (
            select(
                models.Registration.uid,
                models.Registration.ss58_hot,
                models.Registration.ss58_cold,
            )
            .distinct(models.Registration.uid)
            .order_by(models.Registration.uid, models.Registration.block.desc())
        )
        return {row.uid: (row.ss58_hot, row.ss58_cold) for row in session.execute(stmt)}

    # --- entitlements ---------------------------------------------------------
    def is_registered(self, hotkey: str) -> bool:
        # Whether the subnet ever carried this hotkey; unregistered miners are refused first.
        with session_scope(self._sessions) as session:
            return (
                session.execute(
                    select(models.Registration.id)
                    .where(models.Registration.ss58_hot == hotkey)
                    .limit(1)
                ).first()
                is not None
            )

    def available_slots(self, hotkey: str) -> int:
        # Registrations this hotkey holds that no accepted submission has spent yet.
        with session_scope(self._sessions) as session:
            return _available_slots(session, hotkey)

    def claim_slot(self, session: Session, hotkey: str, submission_id: int) -> int:
        # Spend the oldest free registration in the caller's transaction (SKIP LOCKED stops races),
        # unless the submission already holds one, so re-verifying never charges a hotkey twice.
        held = session.execute(
            select(models.EntitlementClaim.registration_id).where(
                models.EntitlementClaim.submission_id == submission_id
            )
        ).scalar_one_or_none()
        if held is not None:
            return int(held)
        claimed = select(models.EntitlementClaim.registration_id)
        row = session.execute(
            select(models.Registration.id)
            .where(
                models.Registration.ss58_hot == hotkey,
                models.Registration.id.not_in(claimed),
            )
            .order_by(models.Registration.block, models.Registration.id)
            .limit(1)
            .with_for_update(skip_locked=True)
        ).first()
        if row is None:
            raise NoSlot(f"{hotkey[:8]}… has no unclaimed registration")
        session.add(models.EntitlementClaim(registration_id=row[0], submission_id=submission_id))
        session.flush()
        return int(row[0])


def _available_slots(session: Session, hotkey: str) -> int:
    # Takes a session so the worker can ask inside the transaction that is about to claim.
    claimed = select(models.EntitlementClaim.registration_id)
    return int(
        session.execute(
            select(func.count())
            .select_from(models.Registration)
            .where(
                models.Registration.ss58_hot == hotkey,
                models.Registration.id.not_in(claimed),
            )
        ).scalar_one()
    )
