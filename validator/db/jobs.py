"""Hardware-worker DB interface: execute-only grants, without promotion/table-write access."""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

from comparisons import validate_bundle_result
from scoring import validate_result
from sqlalchemy import text

from db.engine import session_scope


class WorkerDb:
    def __init__(self, sessions):
        self.sessions = sessions
        self.requests = {}
        self.claims = {}

    def claim(self, environment, identity, owner):
        with session_scope(self.sessions) as s:
            row = (
                s.execute(
                    text("SELECT * FROM public.claim(:env,:identity,:owner,:token)"),
                    {
                        "env": environment,
                        "identity": identity,
                        "owner": owner,
                        "token": uuid.uuid4().hex,
                    },
                )
                .mappings()
                .first()
            )
            if row is None:
                return None
            job = SimpleNamespace(**row)
            self.requests = {job.id: job.request}
            self.claims = {job.id: (job.owner, job.token)}
            return job

    def heartbeat(self, jid, owner, token):
        with session_scope(self.sessions) as s:
            return s.scalar(
                text("SELECT public.heartbeat(:id,:owner,:token)"),
                {"id": jid, "owner": owner, "token": token},
            )

    def write_progress(self, jid, owner, token, attempt, sequence, payload):
        with session_scope(self.sessions) as s:
            return s.scalar(
                text(
                    "SELECT public.write_benchmark_progress("
                    ":id,:owner,:token,:attempt,:sequence,CAST(:payload AS jsonb))"
                ),
                {
                    "id": jid,
                    "owner": owner,
                    "token": token,
                    "attempt": attempt,
                    "sequence": sequence,
                    "payload": json.dumps(payload, allow_nan=False),
                },
            )

    def complete(self, jid, owner, token, result):
        request = self.requests[jid]
        if "comparisons" in request:
            validate_bundle_result(request, result)
        else:
            validate_result(request, result)
        with session_scope(self.sessions) as s:
            return s.scalar(
                text("SELECT public.complete(:id,:owner,:token,CAST(:result AS jsonb))"),
                {
                    "id": jid,
                    "owner": owner,
                    "token": token,
                    "result": json.dumps(result, allow_nan=False),
                },
            )

    def fail(self, jid, owner, token, error):
        with session_scope(self.sessions) as s:
            return s.scalar(
                text("SELECT public.fail(:id,:owner,:token,CAST(:error AS jsonb))"),
                {
                    "id": jid,
                    "owner": owner,
                    "token": token,
                    "error": json.dumps(error),
                },
            )

    def sources(self, jid):
        with session_scope(self.sessions) as s:
            owner, token = self.claims[jid]
            row = s.execute(
                text("SELECT * FROM public.sources(:id,:owner,:token)"),
                {"id": jid, "owner": owner, "token": token},
            ).one()
            return bytes(row.candidate), bytes(row.reference)

    def sources_bundle(self, jid):
        with session_scope(self.sessions) as s:
            owner, token = self.claims[jid]
            rows = s.execute(
                text("SELECT * FROM public.bundle_sources(:id,:owner,:token)"),
                {"id": jid, "owner": owner, "token": token},
            ).all()
            if not rows:
                raise RuntimeError("claimed comparison bundle has no authorized source set")
            candidate = bytes(rows[0].candidate)
            references = {}
            for row in rows:
                if bytes(row.candidate) != candidate:
                    raise RuntimeError("bundle source query returned inconsistent candidate data")
                references[row.reference_hash] = bytes(row.reference)
            return candidate, references

    def access(self):
        with session_scope(self.sessions) as s:
            return s.scalar(text("SELECT public.worker_access()"))
