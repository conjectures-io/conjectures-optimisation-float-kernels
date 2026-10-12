"""What the scorer reads (each hotkey's best ratio), and what it writes back (the audit trail)."""

from __future__ import annotations

import dataclasses as dc
import datetime as dt
import hashlib
import json
from typing import cast

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session, sessionmaker

import admission

from . import models
from .engine import session_scope
from .submissions import SubmissionsDb


@dc.dataclass(frozen=True, slots=True)
class ScoredSubmission:
    # One accepted submission, reduced to what scoring uses; `ratio` is lower-is-faster.
    submission_id: int
    hotkey: str
    ratio: float
    submitted_at: dt.datetime


@dc.dataclass(frozen=True, slots=True)
class Board:
    # One slot as the weight setter scores it: each hotkey's best accepted submission, the
    # statistical admission decisions, and the champion (the one submission paid).
    slot: str
    best: list[ScoredSubmission]
    decisions: list[admission.Decision]
    leader: ScoredSubmission | None


def evidence_of(row: models.Submission) -> admission.Evidence:
    # One accepted submission as admission reads it: its ratio and its paired rounds.
    return admission.Evidence(
        row.id,
        row.hotkey,
        float(row.ratio or 0.0),
        row.submitted_at,
        tuple((float(i), float(s)) for i, s in (row.rounds or [])),
    )


class ScoringDb:
    # What the weight setter scores: each slot's board of best accepted ratios.
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        # Reads through the leaderboard query, so what a miner sees ranked is what gets scored.
        self._sessions = sessions
        self._submissions = SubmissionsDb(sessions)
        # Per slot, the last admission decisions and the evidence and policy they were made from.
        self._decided: dict[str, tuple[object, list[admission.Decision]]] = {}

    def best_per_hotkey(self, slot: str = "add") -> list[ScoredSubmission]:
        # Each hotkey's best accepted ratio in `slot` against its current incumbent, fastest first.
        return [
            ScoredSubmission(r.id, r.hotkey, float(r.ratio or 0.0), r.submitted_at)
            for r in self._submissions.leaderboard(slot)
        ]

    def admission(self, slot: str, bar: float) -> list[admission.Decision]:
        # Where each submission on the board stands against the champion, the champion itself
        # "leader", reused until a verdict, a promotion, the bar or the policy changes.
        champion = self._submissions.champion(slot)
        key = (
            bar,
            admission.POLICY_VERSION,
            champion.id if champion else None,
            self._submissions.evidence_key(slot),
        )
        cached = self._decided.get(slot)
        if cached is not None and cached[0] == key:
            return cached[1]
        decisions = (
            [admission.Decision(champion.id, "leader", None, None)] if champion is not None else []
        )
        decisions += [
            admission.assess(evidence_of(r), bar)
            for r in self._submissions.scorable(slot)
            if champion is None or r.id != champion.id
        ]
        self._decided[slot] = (key, decisions)
        return decisions

    def leader(self, slot: str, bar: float) -> ScoredSubmission | None:
        # A slot's leader: the author of its champion, the one submission in it the setter pays.
        return self._leader(self._submissions.champion(slot))

    def board(self, slot: str, bar: float) -> Board:
        # Everything the weight setter needs for one slot.
        return Board(
            slot, self.best_per_hotkey(slot), self.admission(slot, bar), self.leader(slot, bar)
        )

    def _leader(self, row: models.Submission | None) -> ScoredSubmission | None:
        # The champion's row as scoring reads it, or None while the shipped champion holds.
        if row is None:
            return None
        return ScoredSubmission(row.id, row.hotkey, float(row.ratio or 0.0), row.submitted_at)

    def published_policy(self) -> dict[str, object] | None:
        # The policy the latest published scoring was computed under (reward.publication), or
        # None before the first publication: the competition's share lives only there.
        with session_scope(self._sessions) as session:
            policy = session.execute(
                select(models.WeightSet.api_snapshot["policy"])
                .where(
                    models.WeightSet.api_snapshot.is_not(None),
                    func.jsonb_typeof(models.WeightSet.api_snapshot) == "object",
                )
                .order_by(models.WeightSet.id.desc())
                .limit(1)
            ).scalar_one_or_none()
        return policy if isinstance(policy, dict) else None

    def submission_state_key(self) -> str:
        """Detect queue and verdict changes without duplicating submissions in the payload."""
        with session_scope(self._sessions) as session:
            rows = session.execute(
                select(
                    models.Submission.id,
                    models.Submission.state,
                    models.Submission.verification_attempt,
                    models.Submission.finished_at,
                ).order_by(models.Submission.id)
            ).all()
        return hashlib.sha256(
            json.dumps([list(row) for row in rows], default=str).encode()
        ).hexdigest()

    def record_weight_set(
        self,
        *,
        netuid: int,
        block: int,
        uids: list[int],
        weights: list[float],
        summary: str | None,
        accepted: bool,
        dry_run: bool = False,
        error: str | None = None,
        snapshots: list[dict[str, object]] | None = None,
        api_snapshot: dict[str, object] | None = None,
    ) -> int:
        row_id, _ = self.publish_weight_set(
            netuid=netuid,
            block=block,
            uids=uids,
            weights=weights,
            summary=summary,
            accepted=accepted,
            dry_run=dry_run,
            error=error,
            snapshots=snapshots,
            api_snapshot=api_snapshot,
        )
        return row_id

    def publish_weight_set(
        self,
        *,
        netuid: int,
        block: int,
        uids: list[int],
        weights: list[float],
        summary: str | None,
        accepted: bool,
        dry_run: bool = False,
        error: str | None = None,
        snapshots: list[dict[str, object]] | None = None,
        api_snapshot: dict[str, object] | None = None,
        reuse_unchanged: bool = False,
    ) -> tuple[int, bool]:
        """Persist one weight vector and the per-hotkey reasoning behind it.

        Returns (weight-set id, created). Reused publications remain immutable.
        Ignore only the pass's computed_at; scores, policy,
        vector and dry-run mode must all agree before a polling result can reuse it.
        """
        public_content = (
            {k: v for k, v in api_snapshot.items() if k != "computed_at"}
            if api_snapshot is not None
            else None
        )
        content_key = hashlib.sha256(
            json.dumps(
                [uids, weights, summary, dry_run, snapshots or [], public_content],
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode()
        ).hexdigest()
        with session_scope(self._sessions) as session:
            # Serialize publishers
            # for the same subnet so simultaneous workers cannot duplicate a pass.
            session.execute(
                text("SELECT pg_advisory_xact_lock(771003002, :netuid)"), {"netuid": netuid}
            )
            if reuse_unchanged:
                previous = session.execute(
                    select(
                        models.WeightSet.id,
                        models.WeightSet.scoring_content_sha256,
                        models.WeightSet.api_snapshot.is_not(None),
                    )
                    .where(models.WeightSet.netuid == netuid)
                    .order_by(models.WeightSet.id.desc())
                    .limit(1)
                ).first()
                if previous is not None and previous[1] == content_key:
                    if api_snapshot is None or previous[2]:
                        return cast(int, previous[0]), False
            row = models.WeightSet(
                netuid=netuid,
                block=block,
                uids=list(uids),
                weights=[float(w) for w in weights],
                summary=summary,
                api_snapshot=api_snapshot,
                accepted=accepted,
                dry_run=dry_run,
                error=error,
                scoring_content_sha256=content_key,
            )
            session.add(row)
            session.flush()
            for snap in snapshots or []:
                session.add(models.ScoreSnapshot(weight_set_id=row.id, **snap))
            return int(row.id), True

    def prune_api_snapshots(self, *, netuid: int, retention_seconds: int, batch_size: int) -> int:
        """Clear one batch of expired payloads, keeping the latest object for this subnet.

        Separate from publishing: failure rolls back only this maintenance batch.
        SQL NULL is intentional; JSON null is not a usable published snapshot.
        """
        if retention_seconds <= 0 or batch_size <= 0:
            raise ValueError("snapshot retention and batch size must be positive")
        with session_scope(self._sessions) as session:
            session.execute(text("SET LOCAL lock_timeout = '1s'"))
            session.execute(text("SET LOCAL statement_timeout = '10s'"))
            cleared = session.scalars(
                text("""
                    WITH latest AS (
                        SELECT id FROM weight_sets
                        WHERE netuid = :netuid AND api_snapshot IS NOT NULL
                          AND jsonb_typeof(api_snapshot) = 'object'
                        ORDER BY id DESC LIMIT 1
                    ), batch AS (
                        SELECT id FROM weight_sets
                        WHERE netuid = :netuid AND api_snapshot IS NOT NULL
                          AND created_at < CURRENT_TIMESTAMP
                              - make_interval(secs => :retention_seconds)
                          AND NOT EXISTS (SELECT 1 FROM latest WHERE latest.id = weight_sets.id)
                        ORDER BY created_at, id LIMIT :batch_size
                        FOR UPDATE SKIP LOCKED
                    )
                    UPDATE weight_sets AS w SET api_snapshot = NULL
                    FROM batch WHERE w.id = batch.id
                    RETURNING w.id
                """),
                {
                    "netuid": netuid,
                    "retention_seconds": retention_seconds,
                    "batch_size": batch_size,
                },
            ).all()
            return len(cleared)

    def weight_set_outcome(self, weight_set_id: int, *, accepted: bool, error: str | None) -> None:
        # The chain's answer for a weight set already recorded; its scoring stays as recorded.
        with session_scope(self._sessions) as session:
            row = session.get(models.WeightSet, weight_set_id)
            if row is None:
                raise LookupError(f"no weight set {weight_set_id}")
            row.accepted = accepted
            row.error = error
