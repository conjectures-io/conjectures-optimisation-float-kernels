"""The validator's store: one Postgres engine, its repositories and a rate limiter."""

from __future__ import annotations

from sqlalchemy import Engine, text

from .clock import iso, now
from .engine import create_db_engine, database_url, session_factory, session_scope
from .ratelimit import RateLimiter
from .registrations import NoSlot, RegistrationsDb
from .scoring import ScoredSubmission, ScoringDb
from .status import PENDING, SubmissionState
from .submissions import SubmissionsDb

__all__ = [
    "NoSlot",
    "PENDING",
    "RateLimiter",
    "RegistrationsDb",
    "ScoredSubmission",
    "ScoringDb",
    "Store",
    "SubmissionState",
    "SubmissionsDb",
    "connect",
    "create_db_engine",
    "database_url",
    "iso",
    "now",
    "session_factory",
    "session_scope",
]

# Defaults for the write limiter; the service overrides them from its settings.
RATE_LIMIT = 10
RATE_WINDOW_SECONDS = 60


class Store:
    # One engine and the repositories that share it.
    def __init__(
        self,
        engine: Engine,
        *,
        rate_limit: int = RATE_LIMIT,
        rate_window_seconds: int = RATE_WINDOW_SECONDS,
    ) -> None:
        # Build the session factory and every repository over the one engine.
        self.engine = engine
        self.sessions = session_factory(engine)
        self.submissions = SubmissionsDb(self.sessions)
        self.registrations = RegistrationsDb(self.sessions)
        self.scoring = ScoringDb(self.sessions)
        self.rate = RateLimiter(self.sessions, limit=rate_limit, window_seconds=rate_window_seconds)

    def ping(self) -> bool:
        # Is the database reachable? /ready fails on this while the process itself is alive.
        with self.engine.connect() as conn:
            return conn.execute(text("SELECT 1")).scalar_one() == 1

    def close(self) -> None:
        # Release the connection pool.
        self.engine.dispose()


def connect(url: str | None = None, *, echo: bool = False, **kwargs: object) -> Store:
    # Open the store against `url`, or the environment-resolved default.
    return Store(create_db_engine(url, echo=echo), **kwargs)  # pyright: ignore[reportArgumentType]
