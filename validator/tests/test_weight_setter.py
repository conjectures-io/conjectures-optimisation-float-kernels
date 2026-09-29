"""The weight setter and its scorer: who gets paid, and what gets recorded."""

from __future__ import annotations

import hashlib
import random
import sys
from pathlib import Path

import sqlalchemy as sa
from conftest import FINGERPRINT, register

VALIDATOR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(VALIDATOR))

import scoring  # noqa: E402
from chain.schedule import blocks_until_next_epoch  # noqa: E402
from chain.types import MetagraphView, PollState, SubnetParams  # noqa: E402
from db import SubmissionState, models  # noqa: E402
from workers.weight_setter import WeightSetterConfig, step  # noqa: E402

NETUID = 66
INCUMBENT = "a" * 64


class FakeChain:
    # A chain that is always ready to take a vector, unless told otherwise.
    def __init__(self, *, uids=(0, 1, 2), hotkeys=None, block=1000, since=1000, accept=True):
        self._uids = tuple(uids)
        self._hotkeys = hotkeys or {}
        self.block, self.since, self.accept = block, since, accept
        self.submitted: list[tuple[list[int], list[float]]] = []

    def params(self, netuid: int) -> SubnetParams:
        return SubnetParams(uid=9, tempo=100, weights_rate_limit=10)

    def poll(self, netuid: int, uid: int) -> PollState:
        return PollState(current_block=self.block, blocks_since_last_update=self.since)

    def metagraph(self, netuid: int) -> MetagraphView:
        return MetagraphView(uids=self._uids, uid_by_hotkey=dict(self._hotkeys))

    def set_weights(self, netuid, uids, weights) -> bool:
        self.submitted.append((list(uids), list(weights)))
        return self.accept


PARAMS = SubnetParams(uid=9, tempo=100, weights_rate_limit=10)


def at_epoch_boundary(tempo: int = 100, netuid: int = NETUID, margin: int = 12) -> int:
    # The first block at which should_set would proceed.
    for block in range(1, 10_000):
        if blocks_until_next_epoch(block, tempo, netuid) <= margin:
            return block
    raise AssertionError("no block inside the margin")


def paired(true_ratio: float, seed: str, rounds: int = 25) -> list[list[float]]:
    # The gate's per-round evidence around `true_ratio`, each run jittered by about half a percent.
    rng = random.Random(seed)
    return [
        [3e7 * (1 + rng.gauss(0, 0.005)), 3e7 * true_ratio * (1 + rng.gauss(0, 0.005))]
        for _ in range(rounds)
    ]


def accept(
    store,
    hotkey: str,
    ratio: float,
    *,
    incumbent: str = INCUMBENT,
    uid: int = 1,
    true_ratio: float | None = None,
) -> int:
    # One accepted submission through the store, so the scorer reads real rows; its rounds sit
    # at `true_ratio`, the headline `ratio` unless a lucky run is being simulated.
    register(store, hotkey, uid=uid, block=1000 + uid)
    digest = hashlib.sha256(f"{hotkey}{ratio}{incumbent}".encode()).hexdigest()
    sub_id, _ = store.submissions.add(hotkey, digest)
    store.submissions.claim_next("w")
    store.submissions.finish(
        sub_id,
        SubmissionState.ACCEPTED,
        ratio=ratio,
        stage="score",
        incumbent_sha256=incumbent,
        verifier_fingerprint=FINGERPRINT,
        rounds=paired(ratio if true_ratio is None else true_ratio, hotkey),
    )
    return sub_id


def pay(store) -> scoring.Scoring:
    # What the weight setter would pay: the admitted leader of the add board.
    config = scoring.ScoringConfig()
    return scoring.score(
        store.scoring.best_per_hotkey(), config, store.scoring.leader(config.max_ratio)
    )


# ── The scorer ────────────────────────────────────────────────────────────


def test_the_fastest_hotkey_takes_the_whole_share(store, clock):
    accept(store, "hk-slow", 0.80, uid=1)
    clock["tick"](10)
    accept(store, "hk-fast", 0.53, uid=2)
    result = pay(store)
    assert result.weights == {"hk-fast": 1.0}


def test_a_tie_goes_to_the_earlier_submission(store, clock):
    accept(store, "hk-first", 0.60, uid=1)
    clock["tick"](10)
    accept(store, "hk-second", 0.60, uid=2)
    result = pay(store)
    assert result.weights == {"hk-first": 1.0}


def test_a_lucky_later_copy_does_not_take_the_share(store, clock):
    # Same kernel, one lucky round: it ranks first on the headline ratio, but its rounds are no
    # faster than the leader's, so admission keeps the earlier submission paid.
    accept(store, "hk-first", 0.60, uid=1)
    clock["tick"](10)
    accept(store, "hk-copy", 0.58, uid=2, true_ratio=0.60)
    assert [r.hotkey for r in store.scoring.best_per_hotkey()] == ["hk-copy", "hk-first"]
    assert pay(store).weights == {"hk-first": 1.0}


def test_no_faster_than_the_incumbent_pays_nothing(store):
    accept(store, "hk", 1.01)
    result = pay(store)
    assert result.weights == {}


def test_a_near_copy_of_the_incumbent_pays_nothing(store):
    # Inside the timing noise, not past the promotion bar: the leaderboard says it does not
    # qualify, so the weight setter must not pay it either.
    accept(store, "hk", 0.99)
    result = pay(store)
    assert result.weights == {}


def test_a_ratio_on_the_promotion_bar_pays(store):
    accept(store, "hk", scoring.PROMOTION_BAR)
    result = pay(store)
    assert result.weights == {"hk": 1.0}


def test_only_the_current_incumbent_counts(store, clock):
    # A ratio against an older incumbent is not comparable, so it cannot win.
    accept(store, "hk-old", 0.40, incumbent="b" * 64, uid=1)
    clock["tick"](10)
    accept(store, "hk-new", 0.90, incumbent="c" * 64, uid=2)
    result = pay(store)
    assert result.weights == {"hk-new": 1.0}


def test_the_unclaimed_share_burns():
    meta = MetagraphView(uids=(0, 1, 2), uid_by_hotkey={"hk": 2})
    plan = scoring.to_vector({"hk": 0.6}, meta, burn_uid=0)
    assert plan.submittable and dict(zip(plan.uids, plan.weights)) == {0: 0.4, 1: 0.0, 2: 0.6}


# ── The worker step ───────────────────────────────────────────────────────


def test_a_step_at_the_boundary_sets_the_leader_and_records_why(store):
    accept(store, "hk-fast", 0.53, uid=2)
    chain = FakeChain(hotkeys={"hk-fast": 2}, block=at_epoch_boundary())
    config = WeightSetterConfig(netuid=NETUID, burn_uid=0)
    result = step(chain, store, config, scoring.ScoringConfig(), PARAMS)
    assert result.action == "set"
    assert chain.submitted == [([0, 1, 2], [0.0, 0.0, 1.0])]
    with store.engine.connect() as conn:
        (weight_set,) = conn.execute(sa.select(models.WeightSet)).all()
        snapshots = conn.execute(sa.select(models.ScoreSnapshot)).all()
    assert weight_set.accepted is True
    assert [(s.hotkey, s.ratio, s.weight) for s in snapshots] == [("hk-fast", 0.53, 1.0)]


def test_a_dry_run_records_but_does_not_submit(store):
    accept(store, "hk-fast", 0.53, uid=2)
    chain = FakeChain(hotkeys={"hk-fast": 2}, block=at_epoch_boundary())
    config = WeightSetterConfig(netuid=NETUID, burn_uid=0, dry_run=True)
    result = step(chain, store, config, scoring.ScoringConfig(), PARAMS)
    assert result.action == "skip" and "dry run" in result.reason and chain.submitted == []


def test_before_the_boundary_it_waits(store):
    chain = FakeChain(block=at_epoch_boundary() - 50)
    config = WeightSetterConfig(netuid=NETUID, burn_uid=0)
    assert step(chain, store, config, scoring.ScoringConfig(), PARAMS).action == "wait"
