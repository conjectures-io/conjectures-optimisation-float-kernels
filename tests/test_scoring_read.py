"""Scoring reads remain coherent while other connections promote or pause slots."""

import time

import pytest
from sqlalchemy import event, text
from test_weight_setter import FakeChain, accept, config, vector

import admission
import reward
from workers.weight_setter import plan_for


def test_promotion_between_queries_keeps_old_vector_and_state_hash(store, monkeypatch):
    first = accept(store, "alice", 0.9, uid=1)
    old_key = store.scoring.submission_state_key()
    original = store.scoring._read_slot_inputs

    def promote_during_read(session, slot, bar):
        captured = original(session, slot, bar)
        # This commits through separate connections while the read transaction is still open.
        accept(store, "bob", 0.8, uid=2)
        assert store.submissions.champion("full").hotkey == "bob"
        return captured

    monkeypatch.setattr(store.scoring, "_read_slot_inputs", promote_during_read)
    chain = FakeChain(uids=(0, 1, 2), hotkeys={"alice": 1, "bob": 2})
    plan, result, publication = plan_for(
        store, chain.metagraph(2), config(), reward.ScoringConfig()
    )
    assert vector(plan) == {0: 0.88, 1: 0.12, 2: 0.0}
    assert result.weights == {"alice": 0.6}
    assert publication["slots"]["full"]["leader"] == first
    assert publication["submission_state_sha256"] == old_key
    monkeypatch.setattr(store.scoring, "_read_slot_inputs", original)
    following = store.scoring.read_boards(("full",), 0.97)
    assert following.boards[0].leader.hotkey == "bob"
    assert following.submission_state_sha256 != old_key
    assert reward.score(following.boards, reward.ScoringConfig()).weights == {
        "bob": 0.6,
        "alice": 0.3,
    }


@pytest.mark.parametrize("recover", [False, True])
@pytest.mark.parametrize("policy", ["preserve", "treasury"])
def test_pause_transition_between_queries(store, monkeypatch, recover, policy):
    accept(store, "alice", 0.9)
    sid = store.submissions.add("challenger", "b" * 64, "full")[0]
    sub = store.submissions.claim_next("w")

    def fail():
        store.pauses.fail(
            sub,
            reference_id=None,
            reference_sha256="c" * 64,
            phase="reference-build",
            reason="test outage",
            retry_seconds=300,
        )

    if recover:
        fail()
        store.pauses.retry("full")
        sub = store.submissions.claim_next("w")
    old_key = store.scoring.submission_state_key()
    original = store.scoring._read_slot_inputs

    def transition(session, slot, bar):
        captured = original(session, slot, bar)
        if recover:
            store.pauses.recover(sub, store.pauses.all()[slot].revision)
        else:
            fail()
        return captured

    monkeypatch.setattr(store.scoring, "_read_slot_inputs", transition)
    captured = store.scoring.read_boards(("full",), 0.97)
    assert captured.boards[0].paused is recover
    assert captured.submission_state_sha256 == old_key
    cfg = reward.ScoringConfig(paused_slot_policy=policy)
    assert reward.score(captured.boards, cfg).weights == (
        {} if recover and policy == "treasury" else {"alice": 0.6}
    )
    monkeypatch.setattr(store.scoring, "_read_slot_inputs", original)
    following = store.scoring.read_boards(("full",), 0.97)
    assert following.boards[0].paused is not recover
    assert following.submission_state_sha256 != old_key
    assert store.submissions.get(sid) is not None


def test_admission_after_connection_release_and_cache_reuse(store, monkeypatch):
    accept(store, "alice", 0.9, uid=1)
    accept(store, "bob", 1.01, uid=2)
    original = admission.assess
    calls = []
    queries = []
    active = 0

    def checkout(*args):
        nonlocal active
        active += 1

    def checkin(*args):
        nonlocal active
        active -= 1

    def query(conn, cursor, statement, parameters, context, executemany):
        queries.append(statement)

    def assess(evidence, bar):
        assert active == 0, "statistical work must not hold a database connection"
        calls.append(evidence.submission_id)
        return original(evidence, bar)

    event.listen(store.engine, "checkout", checkout)
    event.listen(store.engine, "checkin", checkin)
    event.listen(store.engine, "before_cursor_execute", query)
    monkeypatch.setattr(admission, "assess", assess)
    try:
        first = store.scoring.read_boards(("full",), 0.97)
        assert len(calls) == 1
        queries.clear()
        second = store.scoring.read_boards(("full",), 0.97)
        assert first == second
        assert len(calls) == 1
        assert not any("submissions.rounds" in q for q in queries)
        assert not any("submissions.report" in q or "submission_files" in q for q in queries)
        store.scoring.read_boards(("full",), 0.96)
        assert len(calls) == 2
        accept(store, "carol", 1.02, uid=3)
        calls.clear()
        store.scoring.read_boards(("full",), 0.96)
        assert len(calls) == 2
    finally:
        event.remove(store.engine, "checkout", checkout)
        event.remove(store.engine, "checkin", checkin)
        event.remove(store.engine, "before_cursor_execute", query)


def test_all_slots_share_one_read_only_snapshot(store, monkeypatch):
    accept(store, "alice", 0.9, uid=1, slot="full")
    accept(store, "old-mul", 0.9, uid=2, slot="mul")
    original = store.scoring._read_slot_inputs

    def read(session, slot, bar):
        assert session.scalar(text("SHOW transaction_isolation")) == "repeatable read"
        assert session.scalar(text("SHOW transaction_read_only")) == "on"
        captured = original(session, slot, bar)
        if slot == "full":
            accept(store, "new-mul", 0.8, uid=3, slot="mul")
        return captured

    monkeypatch.setattr(store.scoring, "_read_slot_inputs", read)
    captured = store.scoring.read_boards(("full", "mul"), 0.97)
    assert [b.leader.hotkey for b in captured.boards] == ["alice", "old-mul"]
    assert store.submissions.champion("mul").hotkey == "new-mul"


@pytest.mark.parametrize("champion", [False, True])
def test_reader_matches_existing_scoring_and_measure_cost(store, champion, capsys):
    accept(store, "alice", 0.9 if champion else 1.01, uid=1)
    for i in range(2, 12):
        accept(store, f"miner-{i}", 1.01 + i / 1000, uid=i)
    old = store.scoring.board("full", 0.97)
    captured = store.scoring.read_boards(("full",), 0.97)
    assert captured.boards == (old,)
    assert captured.submission_state_sha256 == store.scoring.submission_state_key()
    counts = []

    def query(*args):
        counts.append(1)

    event.listen(store.engine, "before_cursor_execute", query)
    try:
        timings = []
        for label in ("existing", "consistent"):
            counts.clear()
            start = time.perf_counter()
            for _ in range(10):
                if label == "existing":
                    store.scoring.board("full", 0.97)
                    store.scoring.submission_state_key()
                else:
                    store.scoring.read_boards(("full",), 0.97)
            timings.append((label, len(counts) / 10, (time.perf_counter() - start) * 100))
        with capsys.disabled():
            print(f"\nScoring read (champion={champion}), queries/ms per tick: {timings}")
    finally:
        event.remove(store.engine, "before_cursor_execute", query)
