"""The champion audit: queueing, outcomes and the three failure policies (decision D8)."""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
import threading
from types import SimpleNamespace

import pytest
import test_exact_competition as competition
from sqlalchemy import select
from test_exact_competition import accepted, result

import db
from db import models as m
from db.exact import ExactDb
from exact.config import Config
from workers import exact_coordinator

# The same two-board competition as the exact competition tests.
config = competition.config


def with_policy(config, policy):
    # The same competition under another audit failure policy.
    return dataclasses.replace(config, audit=config.audit | {"policy": policy})


def promote(store, config, repo, hotkeys):
    # Promote one source per hotkey, in order, on every board.
    repo.reconcile(config)
    for hotkey in hotkeys:
        accepted(store, config, hotkey, f"source-{hotkey}".encode())
        repo.plan(config)
        while job := repo.claim("local", "a" * 64, "worker", 30):
            repo.complete(job.id, job.owner, job.token, result(job.request))
        repo.aggregate(config)


def paid(repo, board="mlp"):
    return [(k["hotkey"], k["audit"]) for k in repo.publication()["boards"][board]["kings"]]


def audit_all(repo, config, outcomes):
    # Claim every due audit and finish it with the outcome its source's hotkey maps to.
    while task := repo.claim_audit():
        assert hashlib.sha256(task.content).hexdigest() == task.source_hash
        hotkey = task.content.decode().removeprefix("source-")
        repo.finish_audit(task.id, outcomes[hotkey], {"reason": "test"}, config)


def test_audit_config_defaults_and_validation(config):
    assert config.audit == {"policy": "alert", "cases": 50, "seconds": 900}
    raw = json.loads(json.dumps(dataclasses.asdict(config)))
    assert Config.parse(raw).audit == config.audit
    for bad in ({"policy": "ignore"}, {"cases": 0}, {"seconds": 0}, {"budget": 1}):
        with pytest.raises(ValueError):
            Config.parse(raw | {"audit": bad})


def test_champions_are_queued_once_per_audit_version(store, config):
    repo = ExactDb(store.sessions)
    promote(store, config, repo, ["a", "b", "c"])
    # The two champions of both boards are the same two sources: two audits, not four.
    assert repo.ensure_audits(config, "v1") == 2
    assert repo.ensure_audits(config, "v1") == 0
    assert paid(repo) == [("c", "pending"), ("b", "pending")]
    audit_all(repo, config, {"b": "clean", "c": "clean"})
    assert paid(repo) == [("c", "clean"), ("b", "clean")]
    # A new audit version re-audits the champions.
    assert repo.ensure_audits(config, "v2") == 2


@pytest.mark.parametrize(
    "policy, expected",
    [
        ("alert", [("c", "failed"), ("b", "clean")]),
        ("disqualify", [("b", "clean"), ("a", "pending")]),
        ("hold", [("b", "clean")]),
    ],
)
def test_failure_policies(store, config, policy, expected):
    config = with_policy(config, policy)
    repo = ExactDb(store.sessions)
    promote(store, config, repo, ["a", "b", "c"])
    repo.ensure_audits(config, "v1")
    if policy == "hold":
        # Until an audit is clean, a held board pays nobody new.
        assert paid(repo) == []
    audit_all(repo, config, {"b": "clean", "c": "failed"})
    assert paid(repo) == expected
    assert paid(repo, "resnet") == expected


def test_an_error_retries_with_backoff_and_never_disqualifies(store, config):
    config = with_policy(config, "disqualify")
    repo = ExactDb(store.sessions)
    promote(store, config, repo, ["a"])
    repo.ensure_audits(config, "v1")
    task = repo.claim_audit()
    assert repo.finish_audit(task.id, "error", {"reason": "sandbox"}, config)
    assert not repo.finish_audit(task.id, "clean", {}, config)
    assert repo.claim_audit() is None
    assert paid(repo) == [("a", "pending")]
    with db.session_scope(store.sessions) as s:
        s.get(m.ExactAudit, task.id).due_at = dt.datetime.now(dt.timezone.utc)
    again = repo.claim_audit()
    assert again.id == task.id
    # A coordinator stopped mid-audit runs it again.
    repo.reconcile(config)
    with db.session_scope(store.sessions) as s:
        row = s.get(m.ExactAudit, task.id)
        assert (row.state, row.attempts) == ("queued", 2)
        assert s.scalar(select(m.ExactPromotion.disqualified_at)) is None


def fake_gate(code, verdict):
    # A supervisor that writes `verdict` where the gate would, and exits with `code`.
    def supervised(command, *args, **kwargs):
        if verdict is not None:
            path = command[command.index("--verdict") + 1]
            with open(path, "w") as f:
                json.dump(verdict, f)
        return code

    return supervised


class FakeAuditRepo:
    def __init__(self):
        self.outcomes = []

    def finish_audit(self, aid, state, report, config):
        self.outcomes.append((state, report.get("mismatches")))


@pytest.mark.parametrize(
    "code, verdict, source, expected",
    [
        (0, {"stage": "audit", "classes": {"audit": {"mismatches": 0}}}, None, ("clean", 0)),
        (1, {"stage": "audit", "classes": {"audit": {"mismatches": 3}}}, None, ("failed", 3)),
        # Only the audit's own finding fails a champion; anything else is an error, retried.
        (1, {"stage": "policy", "reason": "banned name"}, None, ("error", None)),
        (2, None, None, ("error", None)),
        (0, {}, b"other bytes", ("error", None)),
    ],
)
def test_run_audit_maps_the_gate_outcome(monkeypatch, code, verdict, source, expected):
    content = b"Kernel.lean bytes"
    monkeypatch.setattr(exact_coordinator, "supervised", fake_gate(code, verdict))
    repo = FakeAuditRepo()
    task = SimpleNamespace(
        id=1,
        source_hash=hashlib.sha256(content).hexdigest(),
        seed=7,
        cases=5,
        content=source or content,
    )
    config = SimpleNamespace(audit={"seconds": 900}, lease_seconds=120)
    exact_coordinator.run_audit(repo, task, config, threading.Event())
    assert repo.outcomes == [expected]


def test_audit_identity_follows_the_audit_sources_and_proof(config):
    one = exact_coordinator.audit_identity(config)
    assert one == exact_coordinator.audit_identity(config)
    assert one != exact_coordinator.audit_identity(
        dataclasses.replace(config, proof_identity="d" * 64)
    )
