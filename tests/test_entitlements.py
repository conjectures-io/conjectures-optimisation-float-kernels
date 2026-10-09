"""One registration buys one accepted submission, even when two acceptances race."""

from __future__ import annotations

import threading

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import db as store_pkg
from conftest import api, kernel_file, post_submission, register
from db import SubmissionState, models
from db.registrations import NoSlot
from service import sig

ALICE = sig.load_keypair("//Alice")
BOB = sig.load_keypair("//Bob")


def claims(store) -> list[tuple[int, int]]:
    with store_pkg.session_scope(store.sessions) as session:
        return [
            (int(r.registration_id), int(r.submission_id))
            for r in session.execute(sa.select(models.EntitlementClaim)).scalars()
        ]


# ── The gate at the door ──────────────────────────────────────────────────


def test_an_unregistered_hotkey_cannot_submit(client, tmp_path):
    r = post_submission(client, ALICE, kernel_file(tmp_path, "alice"))
    assert r.status_code == 402
    assert "not registered" in r.json()["detail"]["reason"]


def test_one_registration_admits_one_queued_submission(client, store, tmp_path):
    # No slot is spent at submit, but nobody queues more gate work than they can pay for.
    register(store, ALICE.ss58_address)
    first = post_submission(client, ALICE, kernel_file(tmp_path, "a1", "RATIO=0.92"))
    assert first.status_code == 200 and first.json()["slots_remaining"] == 1
    second = post_submission(client, ALICE, kernel_file(tmp_path, "a2", "RATIO=0.88"))
    assert second.status_code == 402
    assert "register again" in second.json()["detail"]["reason"]


def test_a_second_registration_buys_a_second_submission(client, store, drain, tmp_path):
    register(store, ALICE.ss58_address, block=1000)
    a1 = post_submission(client, ALICE, kernel_file(tmp_path, "a1", "RATIO=0.92"))
    assert a1.status_code == 200
    assert drain() == 1
    assert (
        client.get(f"{api()}/submissions/{a1.json()['submission']}").json()["state"] == "accepted"
    )
    # Spent, so the next one needs another registration.
    assert post_submission(client, ALICE, kernel_file(tmp_path, "a2")).status_code == 402
    register(store, ALICE.ss58_address, block=1100)
    a3 = post_submission(client, ALICE, kernel_file(tmp_path, "a3", "RATIO=0.88"))
    assert a3.status_code == 200
    assert drain() == 1
    assert len(claims(store)) == 2


# ── What spends a slot, and what does not ─────────────────────────────────


def test_acceptance_spends_a_slot(client, store, drain, tmp_path):
    reg = register(store, ALICE.ss58_address)
    sid = post_submission(client, ALICE, kernel_file(tmp_path, "a", "RATIO=0.92")).json()[
        "submission"
    ]
    assert store.registrations.available_slots(ALICE.ss58_address) == 1
    drain()
    assert claims(store) == [(reg, sid)]
    assert store.registrations.available_slots(ALICE.ss58_address) == 0


def test_rejection_is_a_free_retry(client, store, drain, tmp_path):
    register(store, ALICE.ss58_address)
    post_submission(client, ALICE, kernel_file(tmp_path, "bad", "REJECT"))
    drain()
    assert claims(store) == []
    assert store.registrations.available_slots(ALICE.ss58_address) == 1
    # And the corrected submission goes through on the same registration.
    good = post_submission(client, ALICE, kernel_file(tmp_path, "good", "RATIO=0.92"))
    assert good.status_code == 200
    drain()
    assert len(claims(store)) == 1


def test_exhausting_the_gate_is_a_charged_rejection(client, store, drain, tmp_path):
    # An entry that runs out of its time budget or over a memory cap held the gate: no free retry.
    reg = register(store, ALICE.ss58_address)
    r = post_submission(client, ALICE, kernel_file(tmp_path, "slow", "EXHAUSTED"))
    sid = r.json()["submission"]
    drain()
    view = client.get(f"{api()}/submissions/{sid}").json()
    assert view["state"] == "rejected" and "time budget" in view["reason"]
    assert claims(store) == [(reg, sid)]
    assert store.registrations.available_slots(ALICE.ss58_address) == 0
    assert post_submission(client, ALICE, kernel_file(tmp_path, "again")).status_code == 402


def test_a_validator_error_never_charges_the_miner(client, store, drain, tmp_path):
    register(store, ALICE.ss58_address)
    post_submission(client, ALICE, kernel_file(tmp_path, "a", "MISCONFIGURED"))
    drain()
    assert claims(store) == []
    assert store.registrations.available_slots(ALICE.ss58_address) == 1


def test_slots_are_per_hotkey_not_shared(client, store, drain, tmp_path):
    register(store, ALICE.ss58_address, uid=1, block=1000)
    register(store, BOB.ss58_address, uid=2, block=1001)
    assert (
        post_submission(client, ALICE, kernel_file(tmp_path, "a", "RATIO=0.92")).status_code == 200
    )
    assert post_submission(client, BOB, kernel_file(tmp_path, "b", "RATIO=0.88")).status_code == 200
    drain()
    assert store.registrations.available_slots(ALICE.ss58_address) == 0
    assert store.registrations.available_slots(BOB.ss58_address) == 0
    assert len(claims(store)) == 2


def test_the_oldest_registration_is_spent_first(store):
    # Registrations are spent oldest first, so which one paid for a submission is answerable.
    old = register(store, ALICE.ss58_address, block=1000)
    new = register(store, ALICE.ss58_address, block=2000)
    first, _ = store.submissions.add(ALICE.ss58_address, "a" * 64)
    second, _ = store.submissions.add(ALICE.ss58_address, "b" * 64)
    store.submissions.finish(first, SubmissionState.ACCEPTED, ratio=0.5)
    store.submissions.finish(second, SubmissionState.ACCEPTED, ratio=0.5)
    assert claims(store) == [(old, first), (new, second)]


# ── The invariant under concurrency ───────────────────────────────────────


def test_accepting_with_no_slot_left_fails_closed(store):
    # If the door check is bypassed, an accepted but unpayable submission is rejected.
    register(store, ALICE.ss58_address)
    first, _ = store.submissions.add(ALICE.ss58_address, "a" * 64)
    second, _ = store.submissions.add(ALICE.ss58_address, "b" * 64)
    assert store.submissions.finish(first, SubmissionState.ACCEPTED, ratio=0.5) == "accepted"
    assert store.submissions.finish(second, SubmissionState.ACCEPTED, ratio=0.5) == "rejected"
    row = store.submissions.get(second)
    assert "no unclaimed registration" in row.reason
    assert len(claims(store)) == 1


def test_two_acceptances_racing_for_one_slot_produce_one_claim(store, database_url):
    # Two real threads race to accept against one registration; exactly one claim wins.
    register(store, ALICE.ss58_address)
    ids = [store.submissions.add(ALICE.ss58_address, c * 64)[0] for c in ("a", "b")]

    stores = [store_pkg.connect(database_url) for _ in ids]
    barrier = threading.Barrier(len(ids))
    outcomes: dict[int, str] = {}

    def accept(i: int) -> None:
        barrier.wait()
        outcomes[ids[i]] = stores[i].submissions.finish(
            ids[i], SubmissionState.ACCEPTED, ratio=0.5 + i / 100
        )

    threads = [threading.Thread(target=accept, args=(i,)) for i in range(len(ids))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    for s in stores:
        s.close()

    assert sorted(outcomes.values()) == ["accepted", "rejected"]
    assert len(claims(store)) == 1
    claimed_submission = claims(store)[0][1]
    assert outcomes[claimed_submission] == "accepted"


def test_a_claim_committed_after_the_snapshot_is_no_slot_not_a_crash(store, database_url):
    # The race's rare branch, made certain: a snapshot that predates a rival's committed claim.
    reg = register(store, ALICE.ss58_address)
    late, _ = store.submissions.add(ALICE.ss58_address, "a" * 64)
    rival, _ = store.submissions.add(ALICE.ss58_address, "b" * 64)
    engine = sa.create_engine(database_url, isolation_level="REPEATABLE READ")
    with Session(engine) as session:
        session.execute(sa.select(models.Registration.id)).all()
        with store_pkg.session_scope(store.sessions) as other:
            other.add(models.EntitlementClaim(registration_id=reg, submission_id=rival))
        with pytest.raises(NoSlot):
            store.registrations.claim_slot(session, ALICE.ss58_address, late)
        session.rollback()
    engine.dispose()
    assert claims(store) == [(reg, rival)]


def test_two_api_processes_cannot_queue_two_entries_on_one_registration(store, database_url):
    # Each round, two stores race to queue different files for one free registration: one wins.
    from db.submissions import QueueFull

    for round_ in range(10):
        hotkey = f"racer{round_}"
        register(store, hotkey, uid=100 + round_, block=2000 + round_)
        stores = [store_pkg.connect(database_url) for _ in range(2)]
        barrier = threading.Barrier(2)
        outcomes: list[str] = []

        def send(i: int, hotkey: str = hotkey) -> None:
            barrier.wait()
            try:
                stores[i].submissions.enqueue(hotkey, f"{round_}{i}".ljust(64, "a"), "full", b"x")
                outcomes.append("queued")
            except QueueFull:
                outcomes.append("full")

        threads = [threading.Thread(target=send, args=(i,)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        for s in stores:
            s.close()
        assert sorted(outcomes) == ["full", "queued"], f"round {round_}: {outcomes}"


def test_a_retry_of_a_queued_file_is_answered_even_with_no_registration_free(
    client, store, tmp_path
):
    # The same file again is the same submission, not a second claim on a registration.
    register(store, ALICE.ss58_address)
    path = kernel_file(tmp_path, "a", "RATIO=0.9")
    first = post_submission(client, ALICE, path).json()
    again = post_submission(client, ALICE, path)
    assert again.status_code == 200
    assert again.json()["submission"] == first["submission"] and again.json()["created"] is False


def test_the_database_itself_refuses_a_double_spend(store):
    # Belt and braces: even a direct insert cannot spend one registration twice.
    reg = register(store, ALICE.ss58_address)
    one, _ = store.submissions.add(ALICE.ss58_address, "a" * 64)
    two, _ = store.submissions.add(ALICE.ss58_address, "b" * 64)
    with store_pkg.session_scope(store.sessions) as session:
        session.add(models.EntitlementClaim(registration_id=reg, submission_id=one))
    with pytest.raises(IntegrityError):
        with store_pkg.session_scope(store.sessions) as session:
            session.add(models.EntitlementClaim(registration_id=reg, submission_id=two))


def test_claim_slot_raises_when_nothing_is_free(store):
    sub, _ = store.submissions.add(ALICE.ss58_address, "a" * 64)
    with pytest.raises(NoSlot):
        with store_pkg.session_scope(store.sessions) as session:
            store.registrations.claim_slot(session, ALICE.ss58_address, sub)
