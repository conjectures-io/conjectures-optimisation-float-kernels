"""Log provenance transitions once, without changing gate or scoring decisions."""

from loguru import logger
from sqlalchemy import text

from .engine import session_scope
from .models import ObservedState


def observe(sessions, slot: str, details: dict) -> None:
    # A separate, best-effort transaction: telemetry failure cannot lose a verdict.
    try:
        previous = None
        with session_scope(sessions) as session:
            session.execute(text("SET LOCAL lock_timeout = '1s'"))
            session.execute(text("SET LOCAL statement_timeout = '2s'"))
            session.execute(text("SELECT pg_advisory_xact_lock(771003001)"))
            row = session.get(ObservedState, slot)
            if row is None:
                session.add(ObservedState(key=slot, details=details))
            elif row.details != details:
                previous = row.details
                row.details = details
        if previous is not None:
            logger.warning(
                "[drift] {} gate provenance changed: old={} new={}", slot, previous, details
            )
    except Exception as exc:
        logger.warning("[drift] could not record provenance: {}", type(exc).__name__)
