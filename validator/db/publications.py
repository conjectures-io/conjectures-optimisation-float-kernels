"""Compact read publication and actual chain-attempt evidence."""

from sqlalchemy import select

from .engine import session_scope
from .models import CoordinatorState, WeightSet


class PublicationsDb:
    def __init__(self, sessions):
        self.sessions = sessions

    def publication(self):
        with session_scope(self.sessions) as s:
            row = s.get(CoordinatorState, 1)
            if row is None or row.publication is None:
                raise RuntimeError("coordinator has not published reward inputs")
            return row.publication

    def record(self, *, netuid, block, plan, publication, dry_run):
        from config import digest

        with session_scope(self.sessions) as s:
            row = WeightSet(
                netuid=netuid,
                block=block,
                uids=list(plan.uids),
                weights=list(plan.weights),
                summary=plan.summary,
                publication=publication,
                publication_hash=digest(publication) if publication else None,
                dry_run=dry_run,
                accepted=False,
                error="attempt started",
            )
            s.add(row)
            s.flush()
            return row.id

    def outcome(self, wid, accepted, error):
        with session_scope(self.sessions) as s:
            row = s.get(WeightSet, wid, with_for_update=True)
            row.accepted, row.error = accepted, error

    def already_dry_run(self, netuid, block):
        with session_scope(self.sessions) as s:
            return (
                s.scalar(
                    select(WeightSet.id)
                    .where(WeightSet.netuid == netuid, WeightSet.block == block, WeightSet.dry_run)
                    .limit(1)
                )
                is not None
            )
