"""The weight setter, its scorer and the treasury split: who gets paid, and what gets recorded."""

from __future__ import annotations

import hashlib
import json
import random

import pytest
import sqlalchemy as sa

import reward
import treasury
from chain.schedule import blocks_until_next_epoch
from chain.types import MetagraphView, PollState, SubnetParams
from conftest import FINGERPRINT, register
from db import SubmissionState, models
from workers.weight_setter import WeightSetterConfig, step

# Off mainnet, so the treasury uid and the competition's share are the test's to choose.
TESTNET = 2
INCUMBENT = "a" * 64
TEMPO = 360
LAST_EPOCH = 900


class FakeChain:
    # A chain that is always ready to take a vector, unless told otherwise.
    def __init__(self, *, uids=(0, 1, 2), hotkeys=None, block=1000, since=1000, accept=True):
        self._uids = tuple(uids)
        self._hotkeys = hotkeys or {}
        self.block, self.since, self.accept = block, since, accept
        self.submitted: list[tuple[list[int], list[float]]] = []
        self.registered = True

    def current_block(self) -> int:
        return self.block

    def params(self, netuid: int) -> SubnetParams:
        if not self.registered:
            raise RuntimeError("the wallet hotkey is not registered")
        return PARAMS

    def poll(self, netuid: int, uid: int) -> PollState:
        return PollState(
            current_block=self.block,
            blocks_since_last_update=self.since,
            last_epoch_block=LAST_EPOCH,
        )

    def metagraph(self, netuid: int) -> MetagraphView:
        return MetagraphView(uids=self._uids, uid_by_hotkey=dict(self._hotkeys))

    def set_weights(self, netuid, uids, weights) -> bool:
        self.submitted.append((list(uids), list(weights)))
        return self.accept


PARAMS = SubnetParams(uid=9, tempo=TEMPO, weights_rate_limit=10)


def config(**overrides) -> WeightSetterConfig:
    # A live testnet setter: treasury at uid 0, the competition a fifth of the weight.
    knobs = dict(netuid=TESTNET, burn_uid=0, treasury_override=0, competition_share_override=0.2)
    return WeightSetterConfig(**{**knobs, "dry_run": False, **overrides})


def at_epoch_boundary(margin: int = 12) -> int:
    # The first block after the last epoch at which should_set would proceed.
    for block in range(LAST_EPOCH, LAST_EPOCH + 2 * TEMPO):
        if blocks_until_next_epoch(block, TEMPO, LAST_EPOCH) <= margin:
            return block
    raise AssertionError("no block inside the margin")


def vector(plan) -> dict[int, float]:
    # A plan as {uid: weight}, rounded for comparison.
    return {uid: round(w, 9) for uid, w in zip(plan.uids, plan.weights, strict=True)}


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
    slot: str = "full",
) -> int:
    # One accepted submission through the store, so the scorer reads real rows; its rounds sit
    # at `true_ratio`, the headline `ratio` unless a lucky run is being simulated.
    register(store, hotkey, uid=uid, block=1000 + uid)
    digest = hashlib.sha256(f"{hotkey}{ratio}{incumbent}{slot}".encode()).hexdigest()
    sub_id, _ = store.submissions.add(hotkey, digest, slot)
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


# The paying slot's part of the competition's budget: all of it, the full entry alone.
WHOLE = 1.0


def pay(store) -> reward.Scoring:
    # What the weight setter would pay: each paying slot's admitted leader its slot's part.
    config = reward.ScoringConfig()
    boards = [store.scoring.board(slot, config.max_ratio) for slot in reward.PAYING_SLOTS]
    return reward.score(boards, config)


# ── The scorer ────────────────────────────────────────────────────────────


def test_the_fastest_hotkey_takes_its_slots_share(store, clock):
    accept(store, "hk-slow", 0.80, uid=1)
    clock["tick"](10)
    accept(store, "hk-fast", 0.53, uid=2)
    result = pay(store)
    assert result.weights == {"hk-fast": WHOLE}


def test_a_tie_goes_to_the_earlier_submission(store, clock):
    accept(store, "hk-first", 0.60, uid=1)
    clock["tick"](10)
    accept(store, "hk-second", 0.60, uid=2)
    result = pay(store)
    assert result.weights == {"hk-first": WHOLE}


def test_a_lucky_later_copy_does_not_take_the_share(store, clock):
    # Same kernel, one lucky round: it ranks first on the headline ratio, but its rounds are no
    # faster than the leader's, so admission keeps the earlier submission paid.
    accept(store, "hk-first", 0.60, uid=1)
    clock["tick"](10)
    accept(store, "hk-copy", 0.58, uid=2, true_ratio=0.60)
    assert [r.hotkey for r in store.scoring.best_per_hotkey("full")] == ["hk-copy", "hk-first"]
    assert pay(store).weights == {"hk-first": WHOLE}


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
    accept(store, "hk", reward.PROMOTION_BAR)
    result = pay(store)
    assert result.weights == {"hk": WHOLE}


def test_incumbent_drift_keeps_previous_winner(store, clock):
    # A different incumbent hash does not remove the previous winner.
    accept(store, "hk-old", 0.40, incumbent="b" * 64, uid=1)
    clock["tick"](10)
    accept(store, "hk-new", 0.90, incumbent="c" * 64, uid=2)
    result = pay(store)
    assert result.weights == {"hk-old": WHOLE}


def test_only_the_full_entry_pays(store):
    # One entry, one paid board: practice-board leaders earn nothing, the full entry's leader all.
    accept(store, "hk-full", 0.55, uid=1)
    accept(store, "hk-sum", 0.53, uid=2, slot="sum")
    accept(store, "hk-bmm", 0.57, uid=3, slot="bmm")
    assert pay(store).weights == {"hk-full": WHOLE}


def test_practice_boards_are_ranked_but_unpaid(store):
    accept(store, "hk-add", 0.50, uid=1, slot="add")
    accept(store, "hk-sqrt", 0.80, uid=2, slot="sqrt")
    assert pay(store).weights == {}
    assert [r.hotkey for r in store.scoring.best_per_hotkey("add")] == ["hk-add"]


def test_slots_without_a_leader_leave_their_part_to_the_treasury():
    # The full entry's leader takes the competition's 0.2; the rest is the treasury's.
    meta = MetagraphView(uids=(0, 1, 2), uid_by_hotkey={"hk": 2})
    plan = treasury.split({"hk": WHOLE}, meta, treasury_uid=0, competition_share=0.2)
    assert vector(plan) == {0: 0.8, 1: 0.0, 2: 0.2}


# ── The epoch and the treasury ────────────────────────────────────────────


def test_the_epoch_is_anchored_on_the_chains_last_epoch_block():
    # Every `tempo` blocks after LastMechansimStepBlock, whatever the netuid.
    assert blocks_until_next_epoch(LAST_EPOCH, TEMPO, LAST_EPOCH) == TEMPO
    assert blocks_until_next_epoch(LAST_EPOCH + TEMPO - 1, TEMPO, LAST_EPOCH) == 1
    assert blocks_until_next_epoch(LAST_EPOCH + TEMPO, TEMPO, LAST_EPOCH) == TEMPO


def test_miners_get_their_score_times_the_share_and_the_treasury_the_rest():
    meta = MetagraphView(uids=(0, 1, 2), uid_by_hotkey={"hk": 2})
    plan = treasury.split({"hk": 1.0}, meta, treasury_uid=0, competition_share=0.2)
    assert plan.submittable and vector(plan) == {0: 0.8, 1: 0.0, 2: 0.2}


def test_an_unregistered_winners_share_goes_to_the_treasury():
    meta = MetagraphView(uids=(0, 1, 2), uid_by_hotkey={})
    plan = treasury.split({"gone": 1.0}, meta, treasury_uid=0, competition_share=0.2)
    assert vector(plan) == {0: 1.0, 1: 0.0, 2: 0.0} and "unregistered" in plan.summary


def test_burn_mode_burns_only_the_competitions_share():
    meta = MetagraphView(uids=(0, 1, 121), uid_by_hotkey={})
    plan = treasury.burn(meta, treasury_uid=121, burn_uid=0, competition_share=0.2)
    assert vector(plan) == {0: 0.2, 1: 0.0, 121: 0.8}


def test_a_treasury_missing_from_the_metagraph_skips_the_epoch():
    meta = MetagraphView(uids=(0, 1), uid_by_hotkey={})
    assert not treasury.split({}, meta, treasury_uid=121).submittable


def test_on_mainnet_the_treasury_and_the_share_are_code_constants():
    assert treasury.treasury_uid_for(66, None, burn_uid=0) == treasury.TREASURY_UID == 121
    assert treasury.competition_share_for(66, None) == treasury.COMPETITION_SHARE
    with pytest.raises(ValueError, match="code constant"):
        treasury.treasury_uid_for(66, 5, burn_uid=0)
    with pytest.raises(ValueError, match="code constant"):
        WeightSetterConfig(netuid=66, competition_share_override=0.5)
    assert treasury.treasury_uid_for(TESTNET, 7, burn_uid=0) == 7


def test_a_pinned_treasury_hotkey_elsewhere_on_mainnet_is_never_paid():
    meta = MetagraphView(uids=(5, 121), uid_by_hotkey={"treasury": 5})
    uid, why = treasury.resolve_treasury(
        meta, netuid=66, treasury_uid=121, treasury_hotkey="treasury"
    )
    assert uid is None and "not the treasury uid" in why


# ── The worker step ───────────────────────────────────────────────────────


def weight_sets(store):
    # Every recorded weight set and its score snapshots.
    with store.engine.connect() as conn:
        sets = conn.execute(sa.select(models.WeightSet).order_by(models.WeightSet.id)).all()
        snaps = conn.execute(sa.select(models.ScoreSnapshot)).all()
    return sets, snaps


def test_dry_run_is_the_default_until_turned_off(monkeypatch):
    assert WeightSetterConfig().dry_run
    monkeypatch.delenv("WEIGHT_DRY_RUN", raising=False)
    assert WeightSetterConfig.from_env().dry_run
    monkeypatch.setenv("WEIGHT_DRY_RUN", "0")
    assert not WeightSetterConfig.from_env().dry_run
    monkeypatch.setenv("WEIGHT_DRY_RUN", "maybe")
    with pytest.raises(ValueError, match="WEIGHT_DRY_RUN"):
        WeightSetterConfig.from_env()


def test_a_step_at_the_boundary_sets_the_split_and_records_why(store):
    sub = accept(store, "hk-fast", 0.53, uid=2)
    chain = FakeChain(hotkeys={"hk-fast": 2}, block=at_epoch_boundary())
    result = step(chain, store, config(), reward.ScoringConfig())
    assert result.action == "set", result.reason
    # The full entry's leader gets the competition's 0.2, the treasury the rest.
    ((uids, weights),) = chain.submitted
    assert uids == [0, 1, 2] and weights == pytest.approx([0.8, 0.0, 0.2])
    (weight_set,), snapshots = weight_sets(store)
    assert weight_set.accepted is True and weight_set.error is None
    assert [(s.slot, s.hotkey, s.ratio) for s in snapshots] == [("full", "hk-fast", 0.53)]
    assert snapshots[0].weight == pytest.approx(WHOLE)
    published = weight_set.api_snapshot
    assert published["policy"]["competition_share"] == 0.2
    assert published["policy"]["slot_shares"] == {slot: WHOLE for slot in reward.PAYING_SLOTS}
    assert published["slots"]["full"]["leader"] == sub
    assert published["slots"]["full"]["items"][0]["admission"] == "leader"
    assert list(published["slots"]) == ["full"]
    json.dumps(published)


def test_a_dry_run_records_but_does_not_submit(store):
    accept(store, "hk-fast", 0.53, uid=2)
    chain = FakeChain(hotkeys={"hk-fast": 2}, block=at_epoch_boundary())
    result = step(chain, store, config(dry_run=True), reward.ScoringConfig())
    assert result.action == "skip" and "dry run" in result.reason and chain.submitted == []
    (weight_set,), _ = weight_sets(store)
    assert weight_set.dry_run and not weight_set.accepted and weight_set.api_snapshot


def test_before_the_boundary_it_waits_with_the_scores_saved(store):
    accept(store, "hk-fast", 0.53, uid=2)
    chain = FakeChain(hotkeys={"hk-fast": 2}, block=at_epoch_boundary() - 50)
    assert step(chain, store, config(), reward.ScoringConfig()).action == "wait"
    (weight_set,), snapshots = weight_sets(store)
    assert "waiting" in weight_set.error and snapshots and chain.submitted == []


def test_a_scoring_failure_pays_the_treasury_instead_of_skipping(store, monkeypatch):
    def broken():
        raise RuntimeError("store hiccup")

    monkeypatch.setattr(store.scoring, "best_per_hotkey", broken)
    chain = FakeChain(block=at_epoch_boundary())
    result = step(chain, store, config(), reward.ScoringConfig())
    assert result.action == "set" and chain.submitted == [([0, 1, 2], [1.0, 0.0, 0.0])]
    assert "scoring failed" in result.plan.summary


def test_an_unregistered_validator_still_saves_the_scores(store):
    accept(store, "hk-fast", 0.53, uid=2)
    chain = FakeChain(hotkeys={"hk-fast": 2}, block=at_epoch_boundary())
    chain.registered = False
    result = step(chain, store, config(), reward.ScoringConfig())
    assert result.action == "skip" and "scores saved" in result.reason
    (weight_set,), snapshots = weight_sets(store)
    assert snapshots and weight_set.api_snapshot and chain.submitted == []


def test_verifier_drift_keeps_previous_winner(store, monkeypatch):
    accept(store, "hk-old", 0.40, uid=1)
    monkeypatch.setenv("GATE_REQUIRED_FINGERPRINT", "e" * 64)
    assert pay(store).weights == {"hk-old": WHOLE}
