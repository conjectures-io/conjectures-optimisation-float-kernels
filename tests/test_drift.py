"""Provenance changes inform operators without changing eligibility."""

import sqlalchemy as sa
from loguru import logger

import gate
from db import models
from db.drift import observe


def test_observation_warns_only_on_transitions_across_store_instances(store):
    messages = []
    sink = logger.add(lambda message: messages.append(str(message)), level="WARNING")
    try:
        observe(store.sessions, "full", {"fingerprint": "a"})
        observe(store.sessions, "full", {"fingerprint": "a"})
        observe(store.sessions, "full", {"fingerprint": "b"})
        # No in-memory cache: a fresh session sees the persisted observation.
        observe(store.sessions, "full", {"fingerprint": "b"})
        observe(store.sessions, "add", {"fingerprint": "a"})
        assert len(messages) == 1
        observe(store.sessions, "full", {"fingerprint": "a"})
        assert len(messages) == 2  # A rollback is also a transition.
    finally:
        logger.remove(sink)


def test_missing_observation_table_does_not_raise(store):
    with store.engine.begin() as connection:
        connection.execute(sa.text("ALTER TABLE observed_states RENAME TO saved_observed_states"))
    try:
        observe(store.sessions, "full", {"fingerprint": "a"})
    finally:
        with store.engine.begin() as connection:
            connection.execute(
                sa.text("ALTER TABLE saved_observed_states RENAME TO observed_states")
            )


def test_legacy_missing_hashes_are_still_eligible(store):
    with store.sessions.begin() as session:
        row = models.Submission(
            hotkey="legacy", digest="a" * 64, slot="full", state="accepted", ratio=0.8
        )
        session.add(row)
    assert [r.hotkey for r in store.submissions.scorable("full")] == ["legacy"]
    assert [r.hotkey for r in store.submissions.leaderboard("full")] == ["legacy"]


def test_midrun_drift_does_not_override_verdict(tmp_path, monkeypatch):
    fingerprints = iter(["a" * 64, "b" * 64])
    monkeypatch.setattr(gate.verifier, "current", lambda: next(fingerprints))
    monkeypatch.setattr(gate, "LAYER", tmp_path / "layer")
    monkeypatch.setattr(gate, "STAGES", [("intake", lambda src, verdict: None)])
    verdict = gate.gate(tmp_path, "c" * 64)
    assert verdict.passed
    assert verdict.verifier_fingerprint == "a" * 64
    assert verdict.verifier_fingerprint_end == "b" * 64
