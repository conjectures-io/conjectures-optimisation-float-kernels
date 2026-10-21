"""Chain scheduling and treasury protections survive the workflow replacement."""

import pytest

import treasury
from chain.schedule import blocks_until_next_epoch
from chain.types import MetagraphView
from workers.weight_setter import WeightSetterConfig

LAST_EPOCH, TEMPO, TESTNET = 900, 360, 2


def vector(plan):
    return dict(zip(plan.uids, plan.weights, strict=True))


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


def test_dry_run_is_the_default_until_turned_off(monkeypatch):
    assert WeightSetterConfig().dry_run
    monkeypatch.delenv("WEIGHT_DRY_RUN", raising=False)
    assert WeightSetterConfig.from_env().dry_run
    monkeypatch.setenv("WEIGHT_DRY_RUN", "0")
    assert not WeightSetterConfig.from_env().dry_run
    monkeypatch.setenv("WEIGHT_DRY_RUN", "maybe")
    with pytest.raises(ValueError, match="WEIGHT_DRY_RUN"):
        WeightSetterConfig.from_env()
