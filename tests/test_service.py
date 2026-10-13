"""Worker regression tests using a historical HTTP fixture and a stub gate on real Postgres."""

from __future__ import annotations

import fcntl
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
    # Alice beats the shipped champion and is promoted; Bob, timed against her kernel, beats it.
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
    b = client.get(f"{api()}/submissions/{sb}").json()
    assert b["champion_sha256"] == a["digest"]

    board = client.get(f"{api()}/leaderboard").json()
    assert board["incumbent_sha256"] == "a" * 64 and board["promotion_bar"] == 0.97
    # This is the add practice board: it promotes and names a champion, but does not pay.
    assert board["slot"] == "add" and board["pays"] is False
    assert client.get("/v1/competitions/floatlib/leaderboard").json()["pays"] is True
    assert board["leader"] == sb and board["champion_sha256"] == b["digest"]
    assert [x["submission"] for x in board["ranking"]] == [sb]
    assert [x["admission"] for x in board["ranking"]] == ["leader"]


def test_a_kernel_no_faster_than_the_bar_is_ranked_but_does_not_qualify(
    client, store, drain, tmp_path
):
    enrol(store, ALICE)
    post_submission(client, ALICE, kernel_file(tmp_path, "alice", "RATIO=0.99"))
    drain()
    (entry,) = client.get(f"{api()}/leaderboard").json()["ranking"]
    assert entry["ratio"] == 0.99 and entry["qualifies"] is False


def test_a_copy_of_the_champion_ranks_below_it_and_is_not_promoted(
    client, store, drain, tmp_path, clock
):
    enrol(store, ALICE, BOB)
    post_submission(client, ALICE, kernel_file(tmp_path, "a1", "RATIO=0.80"))
    clock["tick"](60)
    # Timed against Alice's kernel, a copy of it sits at 1.00, above the bar.
    post_submission(client, BOB, kernel_file(tmp_path, "b1", "RATIO=1.00"))
    clock["tick"](60)
    post_submission(client, ALICE, kernel_file(tmp_path, "a2", "RATIO=0.99"))
    drain()
    board = client.get(f"{api()}/leaderboard").json()
    ranking = board["ranking"]
    # The champion stands in for its author's line; the copy ranks below it.
    assert [x["hotkey"] for x in ranking] == [ALICE.ss58_address, BOB.ss58_address]
    assert [x["admission"] for x in ranking] == ["leader", "above-bar"]
    assert board["leader"] == ranking[0]["submission"]


def test_a_verdict_from_another_verifier_still_ranks(client, store, drain, tmp_path):
    # An older fingerprint is displayed as such, but the accepted result still ranks.
    enrol(store, ALICE)
    sid = post_submission(client, ALICE, kernel_file(tmp_path, "alice", "STALE RATIO=0.80")).json()[
        "submission"
    ]
    drain()
    s = client.get(f"{api()}/submissions/{sid}").json()
    assert s["state"] == "accepted" and s["verifier_fingerprint"] == OLD_FINGERPRINT
    assert s["verification_current"] is False
    board = client.get(f"{api()}/leaderboard").json()
    assert [r["submission"] for r in board["ranking"]] == [sid]
    assert board["verifier_fingerprint"] == FINGERPRINT


def test_explicit_reverify_without_charging_again(client, store, drain, tmp_path, monkeypatch):
    # Drift keeps eligibility; explicit reverification reuses the registration.
    enrol(store, ALICE, slots=1)
    sid = post_submission(client, ALICE, kernel_file(tmp_path, "alice", "RATIO=0.80")).json()[
        "submission"
    ]
    drain()
    assert [x["submission"] for x in client.get(f"{api()}/leaderboard").json()["ranking"]] == [sid]

    monkeypatch.setenv("GATE_REQUIRED_FINGERPRINT", "d" * 64)
    assert [r["submission"] for r in client.get(f"{api()}/leaderboard").json()["ranking"]] == [sid]
    assert [r.id for r in store.submissions.stale("d" * 64)] == [sid]

    assert store.submissions.reverify("d" * 64) == [sid]
    assert client.get(f"{api()}/submissions/{sid}").json()["state"] == "queued"
    assert drain() == 1
    s = client.get(f"{api()}/submissions/{sid}").json()
    assert s["state"] == "accepted" and s["verification_current"] is True
    assert [x["submission"] for x in client.get(f"{api()}/leaderboard").json()["ranking"]] == [sid]
    with store_pkg.session_scope(store.sessions) as session:
        claims = session.execute(sa.select(models.EntitlementClaim)).scalars().all()
        assert claims == []


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
    # Exit 2 is the validator's fault: requeued uncharged, and the drain stops for an operator.
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


def test_a_gate_run_without_a_verdict_is_retried_up_to_the_cap(
    client, store, settings, drain, tmp_path
):
    # No verdict counts like a crash, so nothing is retried forever: at the cap it ends in error.
    enrol(store, ALICE, slots=1)
    d = kernel_file(tmp_path, "a", "MISCONFIGURED")
    sid = post_submission(client, ALICE, d).json()["submission"]
    for attempt in range(1, settings.max_gate_errors):
        assert drain() == 0
        assert client.get(f"{api()}/submissions/{sid}").json()["state"] == "queued"
        assert store.submissions.get(sid).gate_errors == attempt
    assert drain() == 0
    s = client.get(f"{api()}/submissions/{sid}").json()
    assert s["state"] == "error" and s["exit_code"] == 2
    assert store.registrations.available_slots(ALICE.ss58_address) == 1
    # Sending the same file again puts it back in the queue with a fresh count.
    again = post_submission(client, ALICE, d).json()
    assert again["submission"] == sid and again["state"] == "queued"
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
    # The worker's own timeout verdict has no provenance, so it records no false drift.
    with store.engine.connect() as conn:
        assert conn.execute(sa.text("SELECT count(*) FROM observed_states")).scalar() == 0


def test_a_busy_gate_leaves_the_submission_queued_not_rejected(client, store, drain, tmp_path):
    # An operator run holding the gate claims nothing, so waiting never eats a submission's time.
    enrol(store, ALICE)
    sid = post_submission(client, ALICE, kernel_file(tmp_path, "a", "RATIO=0.8")).json()[
        "submission"
    ]
    with open(worker.GATE_LOCK, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        assert drain() == 0
        assert client.get(f"{api()}/submissions/{sid}").json()["state"] == "queued"
    assert drain() == 1
    assert client.get(f"{api()}/submissions/{sid}").json()["state"] == "accepted"


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


def test_ratios_from_an_older_incumbent_stay_on_the_board(client, store, drain, tmp_path):
    # A rebuilt incumbent binary (a bench hotfix, same champion) is provenance: results stay.
    enrol(store, ALICE, BOB)
    post_submission(client, ALICE, kernel_file(tmp_path, "a", "RATIO=0.50"))
    drain()
    gate = Path(worker.GATE)
    gate.write_text(gate.read_text().replace('"a" * 64', '"b" * 64'))
    post_submission(client, BOB, kernel_file(tmp_path, "b", "RATIO=1.01"))
    drain()
    board = client.get(f"{api()}/leaderboard").json()
    assert board["incumbent_sha256"] == "b" * 64
    assert [x["hotkey"] for x in board["ranking"]] == [ALICE.ss58_address, BOB.ss58_address]


def test_a_promotion_starts_a_fresh_board(client, store, drain, tmp_path):
    # Beating the champion makes an entry the champion: the board then counts against it alone.
    enrol(store, ALICE, BOB, CHARLIE)
    post_submission(client, ALICE, kernel_file(tmp_path, "a", "RATIO=0.50"))
    drain()
    alice = client.get(f"{api()}/leaderboard").json()["leader"]
    assert alice is not None
    sb = post_submission(client, BOB, kernel_file(tmp_path, "b", "RATIO=0.90")).json()["submission"]
    post_submission(client, CHARLIE, kernel_file(tmp_path, "c", "RATIO=1.02"))
    drain()
    board = client.get(f"{api()}/leaderboard").json()
    assert board["leader"] == sb != alice
    assert board["champion_sha256"] == store.submissions.get(sb).digest
    assert [x["hotkey"] for x in board["ranking"]] == [BOB.ss58_address, CHARLIE.ss58_address]


def test_a_failing_champion_pauses_until_explicit_demotion(
    client, store, settings, drain, tmp_path, clock
):
    # Its failures are never the challenger's: the challenger is requeued uncounted and uncharged,
    # even after many retries. Only an explicit operator action demotes the champion.
    enrol(store, ALICE, BOB, slots=1)
    a = kernel_file(tmp_path, "a", "RATIO=0.50 BROKEN")
    sa = post_submission(client, ALICE, a).json()["submission"]
    drain()
    assert client.get(f"{api()}/leaderboard").json()["leader"] == sa
    sb = post_submission(client, BOB, kernel_file(tmp_path, "b", "RATIO=0.90")).json()["submission"]
    for _ in range(settings.max_gate_errors + 1):
        assert drain() == 0
        assert store.submissions.get(sb).state == "queued"
        assert store.submissions.get(sb).gate_errors == 0
        assert store.registrations.available_slots(BOB.ss58_address) == 1
        assert store.submissions.champion("add").id == sa
        clock["tick"](settings.champion_retry_seconds)
    store.pauses.demote("add", sa, "operator confirmed broken champion")
    assert store.submissions.champion("add") is None
    assert drain() == 1
    bob = store.submissions.get(sb)
    assert bob.state == "accepted"
    assert bob.champion_sha256 == store.submissions.get(sa).champion_sha256
    assert client.get(f"{api()}/leaderboard").json()["leader"] == sb
    assert store.pauses.all() == {}


def test_an_entry_queued_before_a_promotion_is_timed_against_the_new_champion(
    client, store, drain, tmp_path
):
    # The worker points the gate at the champion as it is when the entry's turn comes.
    enrol(store, ALICE, BOB)
    sa = post_submission(client, ALICE, kernel_file(tmp_path, "a", "RATIO=0.50")).json()[
        "submission"
    ]
    sb = post_submission(client, BOB, kernel_file(tmp_path, "b", "RATIO=1.01")).json()["submission"]
    assert drain() == 2
    assert store.submissions.get(sb).champion_sha256 == store.submissions.get(sa).digest


def test_reverifying_the_champion_keeps_it_champion_timed_against_its_predecessor(
    client, store, drain, tmp_path
):
    # After a gate update, the champion is re-checked against the one before it, never against
    # itself, and is not promoted a second time.
    enrol(store, ALICE, BOB)
    alice_id = post_submission(client, ALICE, kernel_file(tmp_path, "a", "RATIO=0.50")).json()[
        "submission"
    ]
    bob_id = post_submission(client, BOB, kernel_file(tmp_path, "b", "RATIO=0.90")).json()[
        "submission"
    ]
    drain()
    assert sorted(store.submissions.reverify("d" * 64)) == [alice_id, bob_id]
    assert drain() == 2
    alice, bob = store.submissions.get(alice_id), store.submissions.get(bob_id)
    assert bob.state == "accepted" and bob.champion_sha256 == alice.digest
    assert store.submissions.champion("add").id == bob_id
    with store_pkg.session_scope(store.sessions) as session:
        assert (
            session.execute(sa.select(sa.func.count()).select_from(models.Promotion)).scalar() == 2
        )
    assert client.get(f"{api()}/leaderboard").json()["leader"] == bob_id


# ── Liveness and readiness ────────────────────────────────────────────────


def test_health_is_liveness_and_ready_is_the_store(client, store, tmp_path):
    enrol(store, ALICE)
    assert client.get("/health").json() == {
        "ok": True,
        "queued": 0,
        "promotion_bar": 0.97,
        "competition_share": None,
        "admission": {"draws": 2000, "quantile": 0.05},
    }
    ready = client.get("/ready").json()
    assert ready["ok"] is True and ready["database"] is True
    post_submission(client, ALICE, kernel_file(tmp_path, "alice"))
    assert client.get("/health").json()["queued"] == 1


def test_health_states_the_share_the_latest_publication_applied(client, store):
    # The share lives only in what the weight setter published; the newest publication wins.
    for share in (0.2, 0.25):
        store.scoring.record_weight_set(
            netuid=66,
            block=1,
            uids=[0],
            weights=[1.0],
            summary=None,
            accepted=True,
            api_snapshot={"policy": {"competition_share": share}},
        )
    assert client.get("/health").json()["competition_share"] == 0.25
    assert client.get("/ready").json()["competition_share"] == 0.25


# ── The page's reads: kernel times, champions ─────────────────────────────


def test_kernel_times_sum_each_kernels_classes():
    from legacy_service.routes import kernel_times

    classes = {
        "add:near-one": {"submission_ns": 2.0},
        "add:wide": {"submission_ns": 3.0},
        "mul:near-one": {"submission_ns": 4.0},
        "sum:relu": {"submission_ns": 5.0},
        "bmm:relu": {"submission_ns": 6.0},
    }
    assert kernel_times("full", classes) == {"add": 5.0, "mul": 4.0, "sum": 5.0, "bmm": 6.0}
    # A practice board times one kernel, so its unprefixed classes are all that kernel's.
    assert kernel_times("mul", {"a": {"submission_ns": 1.0}, "b": {"submission_ns": 2.5}}) == {
        "mul": 3.5
    }
    assert kernel_times("add", None) is None and kernel_times("add", {}) is None


def test_the_board_carries_kernel_times_and_when_the_champion_took_over(
    client, store, drain, tmp_path, clock
):
    enrol(store, ALICE, BOB)
    alice = post_submission(client, ALICE, kernel_file(tmp_path, "a", "RATIO=0.80")).json()
    drain()
    clock["tick"](60)
    # Timed against Alice's kernel, Bob's is above the bar: ranked, not promoted.
    post_submission(client, BOB, kernel_file(tmp_path, "b", "RATIO=0.99"))
    drain()
    champion, challenger = client.get(f"{api()}/leaderboard").json()["ranking"]
    assert champion["submission"] == alice["submission"]
    assert champion["promoted_at"] == "2026-10-01T12:00:00Z"
    assert champion["kernels"] == {"add": pytest.approx(24.0)}
    assert challenger["promoted_at"] is None
    assert challenger["kernels"] == {"add": pytest.approx(30.0 * 0.99)}


def test_champions_lists_every_champion_newest_first(client, store, drain, tmp_path, clock):
    enrol(store, ALICE, BOB)
    a = post_submission(client, ALICE, kernel_file(tmp_path, "a", "RATIO=0.80")).json()
    drain()
    clock["tick"](60)
    b = post_submission(client, BOB, kernel_file(tmp_path, "b", "RATIO=0.53")).json()
    drain()
    history = client.get(f"{api()}/champions").json()
    assert [(c["submission"], c["current"]) for c in history] == [
        (b["submission"], True),
        (a["submission"], False),
    ]
    assert history[0] == {
        "submission": b["submission"],
        "hotkey": BOB.ss58_address,
        "ratio": 0.53,
        "promoted_at": "2026-10-01T12:01:00Z",
        "demoted_at": None,
        "current": True,
        # A practice board pays no one.
        "rank": None,
        "rank_share": None,
    }
    # Superseded, Alice was never demoted; demoting Bob hands the board back to her.
    assert history[1]["demoted_at"] is None
    # A reference outage preserves the champion's identity in the new public reads.
    post_submission(client, ALICE, kernel_file(tmp_path, "waiting", "RATIO=0.99"))
    waiting = store.submissions.claim_next("test-worker")
    assert waiting is not None
    store.pauses.fail(
        waiting,
        reference_id=b["submission"],
        reference_sha256=store.submissions.get(b["submission"]).digest,
        phase="reference-build",
        reason="compiler unavailable",
        retry_seconds=300,
    )
    assert client.get(f"{api()}/champions").json() == history
    board = client.get(f"{api()}/leaderboard").json()
    assert board["leader"] == b["submission"]
    assert board["ranking"][0]["promoted_at"] == history[0]["promoted_at"]
    clock["tick"](60)
    bob = store.submissions.get(b["submission"])
    with worker.gate_slot() as free:
        assert free
        store.pauses.demote("add", bob.id, "operator reviewed the champion")
    history = client.get(f"{api()}/champions").json()
    assert [(c["submission"], c["current"]) for c in history] == [
        (b["submission"], False),
        (a["submission"], True),
    ]
    assert history[0]["demoted_at"] == "2026-10-01T12:02:00Z"
    # The full entry has had no champion, and an unknown slug is a 404.
    assert client.get("/v1/competitions/floatlib/champions").json() == []
    assert client.get("/v1/competitions/nope/champions").status_code == 404


def test_champions_on_the_paid_board_carry_their_rank_and_share(client, store, clock):
    from test_weight_setter import accept

    subs = []
    for uid, hotkey in enumerate(["hk-1", "hk-2", "hk-3", "hk-4"], start=1):
        subs.append(accept(store, hotkey, 0.90, uid=uid))
        clock["tick"](10)
    history = client.get("/v1/competitions/floatlib/champions").json()
    assert [(c["submission"], c["rank"], c["rank_share"]) for c in history] == [
        (subs[3], 1, 0.6),
        (subs[2], 2, 0.3),
        (subs[1], 3, 0.1),
        (subs[0], None, None),
    ]


# ── The real gate ─────────────────────────────────────────────────────────


def toolchain_present() -> bool:
    # The real gate needs lake, bwrap and a built lean/ workspace from setup.sh.
    lake = Path.home() / ".elan/bin/lake"
    built = (REPO / "lean/.lake/packages/floatlib").is_dir()
    return lake.exists() and built and shutil.which("bwrap") is not None


@pytest.mark.slow
@pytest.mark.skipif(not toolchain_present(), reason="no Lean toolchain or build - run ./setup.sh")
def test_the_real_gate_ranks_real_kernels(client, store, settings, monkeypatch):
    # fastlog is promoted by the real gate.py, the incumbent then timed against it; wrong fails.
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
    assert ratios[ALICE.ss58_address] < 0.6 and 1.5 < ratios[BOB.ss58_address] < 2.2
    assert board["leader"] == store.submissions.champion("add").id
    assert store.submissions.champion("add").hotkey == ALICE.ss58_address
    wrong = [
        s
        for s in (store.submissions.get(i) for i in range(1, 4))
        if s.hotkey == CHARLIE.ss58_address
    ]
    assert wrong[0].state == "rejected" and wrong[0].stage == "statement"
