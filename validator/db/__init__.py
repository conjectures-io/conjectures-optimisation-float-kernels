"""One engine and the repositories used by the tensor competition."""

from sqlalchemy import text

from .clock import iso, now
from .engine import create_db_engine, database_url, session_factory, session_scope
from .publications import PublicationsDb
from .registrations import NoSlot, RegistrationsDb
from .submissions import SubmissionsDb


class Store:
    def __init__(self, engine):
        self.engine = engine
        self.sessions = session_factory(engine)
        self.submissions = SubmissionsDb(self.sessions)
        self.registrations = RegistrationsDb(self.sessions)
        self.publications = PublicationsDb(self.sessions)

    def ping(self):
        with self.engine.connect() as conn:
            return conn.scalar(text("SELECT 1")) == 1

    def close(self):
        self.engine.dispose()


def connect(url=None, *, echo=False):
    return Store(create_db_engine(url, echo=echo))


__all__ = [
    "NoSlot",
    "Store",
    "connect",
    "create_db_engine",
    "database_url",
    "iso",
    "now",
    "session_factory",
    "session_scope",
]
