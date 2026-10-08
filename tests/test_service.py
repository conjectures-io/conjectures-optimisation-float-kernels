"""The submission service end to end - submit, verify, rank - on a stub gate and real Postgres."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import sqlalchemy as sa

import db as store_pkg
from conftest import FINGERPRINT, OLD_FINGERPRINT, api, kernel_file, post_submission, register
from db import models
from service import sig, worker

REPO = Path(__file__).resolve().parents[1]

ALICE = sig.load_keypair("//Alice")
BOB = sig.load_keypair("//Bob")
CHARLIE = sig.load_keypair("//Charlie")


def enrol(store, *keypairs, slots: int = 4):
    # Give each hotkey `slots` registrations; test_entitlements.py covers running out.
    for i, kp in enumerate(keypairs):
        for n in range(slots):
            register(store, kp.ss58_address, uid=i, block=1000 + i * 100 + n)


# ── The happy path ────────────────────────────────────────────────────────


def test_submit_verify_rank(client, store, drain, tmp_path):
    # Two miners; Bob's kernel is faster; both accepted; Bob first.
    enrol(store, ALICE, BOB)
    ra = post_submission(client, ALICE, kernel_file(tmp_path, "alice", "RATIO=0.80"))
    rb = post_submission(client, BOB, kernel_file(tmp_path, "bob", "RATIO=0.53"))
    assert ra.status_code == 200 and rb.status_code == 200, ra.text + rb.text
    sa, sb = ra.json()["submission"], rb.json()["submission"]
    assert client.get(f"{api()}/submissions/{sa}").json()["state"] == "queued"
    assert client.get("/health").json()["queued"] == 2
    assert drain() == 2

    a = client.get(f"{api()}/submissions/{sa}").json()
    assert a["state"] == "accepted" and a["stage"] == "score" and a["ratio"] == 0.80
    assert a["incumbent_sha256"] == "a" * 64 and a["seed"] == 7
    assert a["classes"]["near-one"]["ratio"] == 0.80

    board = client.get(f"{api()}/leaderboard").json()
    assert board["incumbent_sha256"] == "a" * 64 and board["promotion_bar"] == 0.97
    # This is the add practice board: it ranks and names a leader, but does not pay.
    assert board["slot"] == "add" and board["pays"] is False
    assert client.get("/v1/competitions/floatlib/leaderboard").json()["pays"] is True
    assert [x["submission"] for x in board["ranking"]] == [sb, sa]
    assert [x["rank"] for x in board["ranking"]] == [1, 2]
    assert [x["qualifies"] for x in board["ranking"]] == [True, True]


def test_a_kernel_no_faster_than_the_bar_is_ranked_but_does_not_qualify(
    client, store, drain, tmp_path
):
    enrol(store, ALICE)
    post_submission(client, ALICE, kernel_file(tmp_path, "alice", "RATIO=0.99"))
    drain()
    (entry,) = client.get(f"{api()}/leaderboard").json()["ranking"]
    assert entry["ratio"] == 0.99 and entry["qualifies"] is False


def test_leaderboard_keeps_each_hotkeys_best_and_ties_go_to_the_earlier(
    client, store, drain, tmp_path, clock
):
    enrol(store, ALICE, BOB)
    post_submission(client, ALICE, kernel_file(tmp_path, "a1", "RATIO=0.80"))
    clock["tick"](60)
    post_submission(client, BOB, kernel_file(tmp_path, "b1", "RATIO=0.60"))
    clock["tick"](60)
    # Alice ties Bob, later: Bob keeps the rank.
    post_submission(client, ALICE, kernel_file(tmp_path, "a2", "RATIO=0.60"))
    drain()
    board = client.get(f"{api()}/leaderboard").json()
    ranking = board["ranking"]
    assert [x["hotkey"] for x in ranking] == [BOB.ss58_address, ALICE.ss58_address]
    assert [x["ratio"] for x in ranking] == [0.60, 0.60]
    # Bob's run was significantly faster than Alice's first; her later tie is not, so Bob leads.
    assert [x["admission"] for x in ranking] == ["leader", "not-significant"]
    assert board["leader"] == ranking[0]["submission"]


def test_a_verdict_from_another_verifier_is_not_ranked(client, store, drain, tmp_path):
    # Accepted, but by an older gate: it shows as not current and stays off the board.
    enrol(store, ALICE)
    sid = post_submission(client, ALICE, kernel_file(tmp_path, "alice", "STALE RATIO=0.80")).json()[
        "submission"
    ]
    drain()
    s = client.get(f"{api()}/submissions/{sid}").json()
    assert s["state"] == "accepted" and s["verifier_fingerprint"] == OLD_FINGERPRINT
    assert s["verification_current"] is False
    board = client.get(f"{api()}/leaderboard").json()
    assert board["ranking"] == [] and board["verifier_fingerprint"] == FINGERPRINT


def test_reverify_restores_the_ranking_without_charging_again(
    client, store, drain, tmp_path, monkeypatch
):
    # One registration, one acceptance; a verifier change retires it, re-verification restores
    # it, and the second acceptance reuses the registration instead of failing for want of one.
    enrol(store, ALICE, slots=1)
    sid = post_submission(client, ALICE, kernel_file(tmp_path, "alice", "RATIO=0.80")).json()[
        "submission"
    ]
    drain()
    assert [x["submission"] for x in client.get(f"{api()}/leaderboard").json()["ranking"]] == [sid]

    monkeypatch.setenv("GATE_REQUIRED_FINGERPRINT", "d" * 64)
    assert client.get(f"{api()}/leaderboard").json()["ranking"] == []
    assert [r.id for r in store.submissions.stale("d" * 64)] == [sid]

    assert store.submissions.reverify("d" * 64) == [sid]
    assert client.get(f"{api()}/submissions/{sid}").json()["state"] == "queued"
    assert drain() == 1
    s = client.get(f"{api()}/submissions/{sid}").json()
    assert s["state"] == "accepted" and s["verification_current"] is True
    assert [x["submission"] for x in client.get(f"{api()}/leaderboard").json()["ranking"]] == [sid]
    with store_pkg.session_scope(store.sessions) as session:
        claims = session.execute(sa.select(models.EntitlementClaim)).scalars().all()
        assert [c.submission_id for c in claims] == [sid]


def test_same_file_again_returns_the_same_submission(client, store, drain, tmp_path):
    enrol(store, ALICE)
    d = kernel_file(tmp_path, "alice", "RATIO=0.80")
    first = post_submission(client, ALICE, d).json()
    drain()
    again = post_submission(client, ALICE, d).json()
    assert again["submission"] == first["submission"] and again["state"] == "accepted"


# ── What the gate refuses ─────────────────────────────────────────────────


def test_rejected_submission_is_reported_not_ranked(client, store, drain, tmp_path):
    enrol(store, ALICE)
    sid = post_submission(client, ALICE, kernel_file(tmp_path, "alice", "REJECT")).json()[
        "submission"
    ]
    drain()
    s = client.get(f"{api()}/submissions/{sid}").json()
    assert s["state"] == "rejected" and s["stage"] == "statement" and s["ratio"] is None
    assert "does not typecheck" in s["reason"]
    assert client.get(f"{api()}/leaderboard").json()["ranking"] == []


def test_validator_error_stops_the_worker_and_blames_nobody(client, store, drain, tmp_path):
    # Exit 2 is the validator's fault: requeued untouched, and the drain stops for an operator.
    enrol(store, ALICE, BOB)
    ida = post_submission(client, ALICE, kernel_file(tmp_path, "a", "MISCONFIGURED")).json()[
        "submission"
    ]
    idb = post_submission(client, BOB, kernel_file(tmp_path, "b", "RATIO=0.60")).json()[
        "submission"
    ]
    assert drain() == 0
    assert client.get(f"{api()}/submissions/{ida}").json()["state"] == "queued"
    assert client.get(f"{api()}/submissions/{idb}").json()["state"] == "queued"


def test_a_gate_crash_without_a_verdict_is_retried_without_limit(
    client, store, settings, drain, tmp_path
):
    # No verdict means the gate failed before reaching the submission: never counted against it.
    enrol(store, ALICE)
    sid = post_submission(client, ALICE, kernel_file(tmp_path, "a", "MISCONFIGURED")).json()[
        "submission"
    ]
    for _ in range(settings.max_gate_errors + 2):
        assert drain() == 0
    assert client.get(f"{api()}/submissions/{sid}").json()["state"] == "queued"
    assert store.submissions.get(sid).gate_errors == 0


def test_a_submission_that_keeps_crashing_the_gate_ends_in_error(
    client, store, settings, drain, tmp_path
):
    # A stage that crashes on this file is counted; at the cap it leaves the queue, uncharged.
    enrol(store, ALICE, slots=1)
    sid = post_submission(client, ALICE, kernel_file(tmp_path, "a", "CRASH")).json()["submission"]
    for attempt in range(1, settings.max_gate_errors):
        assert drain() == 0
        assert client.get(f"{api()}/submissions/{sid}").json()["state"] == "queued"
        assert store.submissions.get(sid).gate_errors == attempt
    assert drain() == 0
    s = client.get(f"{api()}/submissions/{sid}").json()
    assert s["state"] == "error" and s["stage"] == "policy" and s["reason"] == "gate error: boom"
    assert s["exit_code"] == 2
    assert client.get("/health").json()["queued"] == 0
    # Never charged, and no longer pending: the registration buys the next submission.
    assert store.registrations.available_slots(ALICE.ss58_address) == 1
    r = post_submission(client, ALICE, kernel_file(tmp_path, "b", "RATIO=0.70"))
    assert r.status_code == 200, r.text


def test_a_submission_that_crashes_the_gate_does_not_hold_up_the_queue(
    client, store, drain, tmp_path, clock
):
    # The crashing file was first, but after one crash everything else goes ahead of it.
    enrol(store, ALICE, BOB)
    bad = post_submission(client, ALICE, kernel_file(tmp_path, "a", "CRASH")).json()["submission"]
    clock["tick"](60)
    good = post_submission(client, BOB, kernel_file(tmp_path, "b", "RATIO=0.60")).json()[
        "submission"
    ]
    assert drain() == 0
    assert drain() == 1
    assert client.get(f"{api()}/submissions/{good}").json()["state"] == "accepted"
    assert client.get(f"{api()}/submissions/{bad}").json()["state"] == "queued"


def test_the_worker_backs_off_after_validator_errors():
    assert worker.backoff(0) == worker.IDLE_SECONDS
    assert worker.backoff(1) == 2 * worker.IDLE_SECONDS
    assert worker.backoff(50) == worker.backoff(5000) == worker.MAX_BACKOFF_SECONDS


def test_a_gate_that_never_finishes_is_a_rejection(client, store, drain, tmp_path, monkeypatch):
    enrol(store, ALICE)
    slow = tmp_path / "slow.py"
    slow.write_text("import time; time.sleep(5)")
    monkeypatch.setattr(worker, "GATE", slow)
    monkeypatch.setattr(worker, "TOTAL_TIMEOUT", 0.5)
    sid = post_submission(client, ALICE, kernel_file(tmp_path, "alice")).json()["submission"]
    drain()
    s = client.get(f"{api()}/submissions/{sid}").json()
    assert s["state"] == "rejected" and s["stage"] == "timeout"
    assert "did not finish within" in s["reason"]


def test_a_worker_that_died_mid_gate_is_reclaimed(client, store, settings, drain, tmp_path, clock):
    # A crashed worker leaves its row in `verifying`; the next sweep puts it back on the queue.
    enrol(store, ALICE)
    sid = post_submission(client, ALICE, kernel_file(tmp_path, "alice", "RATIO=0.8")).json()[
        "submission"
    ]
    assert store.submissions.claim_next("dead-worker").id == sid
    assert store.submissions.requeue_stale(settings.stale_claim_seconds) == 0  # still fresh
    clock["tick"](settings.stale_claim_seconds + 1)
    assert store.submissions.requeue_stale(settings.stale_claim_seconds) == 1
    assert client.get(f"{api()}/submissions/{sid}").json()["state"] == "queued"
    drain()
    assert client.get(f"{api()}/submissions/{sid}").json()["state"] == "accepted"


def test_a_restarted_worker_requeues_its_own_claims_at_once(client, store, settings, tmp_path):
    # Its last process died mid-gate: no need to wait out the stale-claim timeout.
    enrol(store, ALICE, BOB)
    sa = post_submission(client, ALICE, kernel_file(tmp_path, "alice", "RATIO=0.8")).json()
    sb = post_submission(client, BOB, kernel_file(tmp_path, "bob", "RATIO=0.7")).json()
    assert store.submissions.claim_next(settings.worker_id).id == sa["submission"]
    assert store.submissions.claim_next("another-worker").id == sb["submission"]
    assert store.submissions.requeue_interrupted(settings.worker_id) == 1
    assert client.get(f"{api()}/submissions/{sa['submission']}").json()["state"] == "queued"
    assert client.get(f"{api()}/submissions/{sb['submission']}").json()["state"] == "verifying"


def test_the_kernel_lives_in_the_store_and_the_verdict_becomes_the_report(
    client, store, settings, drain, tmp_path
):
    # The gate reads Kernel.lean from submission_files, so no shared disk is needed.
    enrol(store, ALICE)
    d = kernel_file(tmp_path, "alice", "RATIO=0.8")
    sid = post_submission(client, ALICE, d).json()["submission"]
    assert store.submissions.kernel(sid) == (d / "Kernel.lean").read_bytes()
    assert not settings.submission_dir(sid).exists()
    drain()
    row = store.submissions.get(sid)
    assert row.state == "accepted" and json.loads(row.report)["ratio"] == 0.8


def test_ratios_from_an_older_incumbent_leave_the_board(client, store, drain, tmp_path):
    # After a promotion the pinned incumbent changes; old ratios are not comparable.
    enrol(store, ALICE, BOB)
    post_submission(client, ALICE, kernel_file(tmp_path, "a", "RATIO=0.50"))
    drain()
    gate = Path(worker.GATE)
    gate.write_text(gate.read_text().replace('"a" * 64', '"b" * 64'))
    post_submission(client, BOB, kernel_file(tmp_path, "b", "RATIO=0.95"))
    drain()
    board = client.get(f"{api()}/leaderboard").json()
    assert board["incumbent_sha256"] == "b" * 64
    assert [x["hotkey"] for x in board["ranking"]] == [BOB.ss58_address]


# ── Liveness and readiness ────────────────────────────────────────────────


def test_health_is_liveness_and_ready_is_the_store(client, store, tmp_path):
    enrol(store, ALICE)
    assert client.get("/health").json() == {"ok": True, "queued": 0, "promotion_bar": 0.97}
    ready = client.get("/ready").json()
    assert ready["ok"] is True and ready["database"] is True
    post_submission(client, ALICE, kernel_file(tmp_path, "alice"))
    assert client.get("/health").json()["queued"] == 1


# ── The real gate ─────────────────────────────────────────────────────────


def toolchain_present() -> bool:
    # The real gate needs lake, bwrap and a built lean/ workspace from setup.sh.
    lake = Path.home() / ".elan/bin/lake"
    built = (REPO / "lean/.lake/packages/floatlib").is_dir()
    return lake.exists() and built and shutil.which("bwrap") is not None


@pytest.mark.slow
@pytest.mark.skipif(not toolchain_present(), reason="no Lean toolchain or build - run ./setup.sh")
def test_the_real_gate_ranks_real_kernels(client, store, settings, monkeypatch):
    # fastlog and the incumbent through the real gate.py; wrong is rejected at statement.
    enrol(store, ALICE, BOB, CHARLIE)
    monkeypatch.setattr(worker, "GATE", REPO / "gate.py")
    monkeypatch.setattr(worker, "TOTAL_TIMEOUT", 1800.0)
    # The real gate stamps this checkout's verifier, so score against it, not the suite's pin.
    monkeypatch.delenv("GATE_REQUIRED_FINGERPRINT")
    subs = REPO / "kernels" / "add"
    for kp, name in [(ALICE, "fastlog"), (BOB, "incumbent"), (CHARLIE, "wrong")]:
        assert post_submission(client, kp, subs / name).status_code == 200
    assert worker.drain(store, settings) == 3
    board = client.get(f"{api()}/leaderboard").json()
    ratios = {x["hotkey"]: x["ratio"] for x in board["ranking"]}
    assert set(ratios) == {ALICE.ss58_address, BOB.ss58_address}
    assert ratios[ALICE.ss58_address] < 0.6 < 0.97 < ratios[BOB.ss58_address] < 1.03
    wrong = [
        s
        for s in (store.submissions.get(i) for i in range(1, 4))
        if s.hotkey == CHARLIE.ss58_address
    ]
    assert wrong[0].state == "rejected" and wrong[0].stage == "statement"
