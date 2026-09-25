"""A fixed-window rate limiter that lives in Postgres, not in a process's memory."""

from __future__ import annotations

import datetime as dt
from typing import Any, cast

from sqlalchemy import CursorResult, delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from . import clock, models
from .engine import session_scope


def window_start(t: dt.datetime, seconds: int) -> dt.datetime:
    # Floor `t` to its window start, so every process agrees on the window without coordinating.
    epoch = int(t.timestamp()) // seconds * seconds
    return dt.datetime.fromtimestamp(epoch, tz=dt.timezone.utc)


class RateLimiter:
    # Counts writes per subject per window in one atomic upsert, so the limit holds across restarts.

    def __init__(self, sessions: sessionmaker[Session], *, limit: int, window_seconds: int) -> None:
        # Allow `limit` writes per subject in each `window_seconds` window.
        self._sessions = sessions
        self._limit = limit
        self._window = window_seconds

    @property
    def limit(self) -> int:
        # The writes allowed per window, for the 429 message.
        return self._limit

    def hit(self, subject: str) -> tuple[bool, int]:
        # Count every attempt, so a hammering caller stays refused; returns (allowed, hits).
        start = window_start(clock.now(), self._window)
        with session_scope(self._sessions) as session:
            stmt = (
                insert(models.RateLimitWindow)
                .values(subject=subject, window_start=start, hits=1)
                .on_conflict_do_update(
                    index_elements=["subject", "window_start"],
                    set_={"hits": models.RateLimitWindow.hits + 1},
                )
                .returning(models.RateLimitWindow.hits)
            )
            hits = int(session.execute(stmt).scalar_one())
            return hits <= self._limit, hits

    def peek(self, subject: str) -> int:
        # Hits so far in the current window, without counting one.
        start = window_start(clock.now(), self._window)
        with session_scope(self._sessions) as session:
            return int(
                session.execute(
                    select(models.RateLimitWindow.hits).where(
                        models.RateLimitWindow.subject == subject,
                        models.RateLimitWindow.window_start == start,
                    )
                ).scalar_one_or_none()
                or 0
            )

    def prune(self, keep_windows: int = 4) -> int:
        # Drop counters for windows that can no longer be current, so the table stays bounded.
        cutoff = window_start(clock.now(), self._window) - dt.timedelta(
            seconds=self._window * keep_windows
        )
        with session_scope(self._sessions) as session:
            result = session.execute(
                delete(models.RateLimitWindow).where(models.RateLimitWindow.window_start < cutoff)
            )
            return int(cast("CursorResult[Any]", result).rowcount or 0)
