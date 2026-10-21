"""Hardware-worker DB interface: execute-only grants, without promotion/table-write access."""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

from sqlalchemy import text

from db.engine import session_scope

from .scoring import validate_result


class WorkerDb:
    def __init__(self, sessions):
        self.sessions = sessions
        self.requests = {}

    def claim(self, environment, identity, owner, lease):
        with session_scope(self.sessions) as s:
            row = (
                s.execute(
                    text("SELECT * FROM public.exact_claim(:env,:identity,:owner,:token,:lease)"),
                    {
                        "env": environment,
                        "identity": identity,
                        "owner": owner,
                        "token": uuid.uuid4().hex,
                        "lease": lease,
                    },
                )
                .mappings()
                .first()
            )
            if row is None:
                return None
            job = SimpleNamespace(**row)
            self.requests = {job.id: job.request}
            return job

    def heartbeat(self, jid, owner, token, lease):
        with session_scope(self.sessions) as s:
            return s.scalar(
                text("SELECT public.exact_heartbeat(:id,:owner,:token,:lease)"),
                {"id": jid, "owner": owner, "token": token, "lease": lease},
            )

    def complete(self, jid, owner, token, result):
        validate_result(self.requests[jid], result)
        with session_scope(self.sessions) as s:
            return s.scalar(
                text("SELECT public.exact_complete(:id,:owner,:token,CAST(:result AS jsonb))"),
                {
                    "id": jid,
                    "owner": owner,
                    "token": token,
                    "result": json.dumps(result, allow_nan=False),
                },
            )

    def fail(self, jid, owner, token, error, retry_seconds):
        with session_scope(self.sessions) as s:
            return s.scalar(
                text("SELECT public.exact_fail(:id,:owner,:token,CAST(:error AS jsonb),:retry)"),
                {
                    "id": jid,
                    "owner": owner,
                    "token": token,
                    "error": json.dumps(error),
                    "retry": retry_seconds,
                },
            )

    def sources(self, jid):
        with session_scope(self.sessions) as s:
            row = s.execute(text("SELECT * FROM public.exact_sources(:id)"), {"id": jid}).one()
            return bytes(row.candidate), bytes(row.reference)
