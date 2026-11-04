"""Chain scheduling and treasury protections survive the workflow replacement."""

import os
import subprocess
import sys
import textwrap
import threading
import typing
from pathlib import Path

import pytest

import treasury
from axiom import EventType
from chain.schedule import blocks_until_next_epoch
from chain.types import MetagraphView
from workers import weight_setter
from workers.weight_setter import TickRunner, TickStalled, WeightSetterConfig

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


def test_the_tick_deadline_is_configurable_and_must_be_positive(monkeypatch):
    assert WeightSetterConfig().tick_timeout_seconds == 300.0
    monkeypatch.setenv("WEIGHT_TICK_TIMEOUT_SECONDS", "45")
    assert WeightSetterConfig.from_env().tick_timeout_seconds == 45.0
    with pytest.raises(ValueError, match="WEIGHT_TICK_TIMEOUT_SECONDS"):
        WeightSetterConfig(tick_timeout_seconds=0)


@pytest.fixture
def stuck():
    # A call that blocks until the test ends, like the SDK joining a thread that never exits.
    release = threading.Event()
    yield lambda: release.wait(30)
    release.set()


def test_a_tick_that_returns_or_raises_is_an_ordinary_tick():
    runner = TickRunner(5)
    assert runner.call(lambda: 7) == 7

    def bad():
        raise ValueError("bad")

    with pytest.raises(ValueError, match="bad"):
        runner.call(bad)

    def sdk_timeout():
        raise TimeoutError("timed out")

    # The SDK's own TimeoutError is a failed tick to retry, not a stall.
    with pytest.raises(TimeoutError) as raised:
        runner.call(sdk_timeout)
    assert not isinstance(raised.value, TickStalled)


def test_a_tick_past_its_deadline_is_a_stall(stuck):
    with pytest.raises(TickStalled, match="connecting to the chain did not return within"):
        TickRunner(0.05).call(stuck, what="connecting to the chain")


def test_a_stalled_tick_leaves_the_loop_and_a_failed_one_does_not(monkeypatch, stuck):
    config = WeightSetterConfig(poll_seconds=1, tick_timeout_seconds=0.05)
    outcomes = iter(["fail", "stall"])

    def fake_step(chain, store, config):
        if next(outcomes) == "stall":
            stuck()
        raise RuntimeError("chain read failed")

    monkeypatch.setattr(weight_setter, "step", fake_step)
    sleeps = []
    with pytest.raises(TickStalled):
        weight_setter.run(None, None, config, sleep=sleeps.append)
    assert sleeps == [1]  # the failed tick was retried after a poll; the stall ended the loop


def test_a_stall_is_reported_and_exits_for_pm2_to_restart():
    class Events:
        def __init__(self):
            self.sent, self.closed = [], False

        def error(self, event_type, **fields):
            self.sent.append(("error", event_type, fields))

        def info(self, event_type, **fields):
            self.sent.append(("info", event_type, fields))

        def close(self):
            self.closed = True

    events, codes = Events(), []
    config = WeightSetterConfig(tick_timeout_seconds=300)
    with pytest.raises(AssertionError, match="unreachable"):
        weight_setter.exit_stalled(events, TickStalled("tick stuck"), config, exit=codes.append)
    assert codes == [weight_setter.EXIT_STALLED]
    assert events.sent[0] == (
        "error",
        "weights_stalled",
        {"reason": "tick stuck", "timeout_seconds": 300},
    )
    assert events.sent[1][:2] == ("info", "service_stopped") and events.closed
    assert "weights_stalled" in typing.get_args(EventType)


def test_a_stalled_setter_really_exits_while_its_tick_thread_is_stuck():
    # The real os._exit: interpreter shutdown would join the stuck tick thread and never finish.
    script = textwrap.dedent(
        """
        import threading
        from axiom import get_events
        from workers.weight_setter import TickRunner, TickStalled, WeightSetterConfig, exit_stalled

        config = WeightSetterConfig(tick_timeout_seconds=0.2)
        try:
            TickRunner(0.2).call(threading.Event().wait)
        except TickStalled as exc:
            exit_stalled(get_events(), exc, config)
        """
    )
    root = Path(__file__).resolve().parents[1]
    env = {**os.environ, "PYTHONPATH": os.pathsep.join((str(root), str(root / "validator")))}
    done = subprocess.run(
        [sys.executable, "-c", script], env=env, capture_output=True, text=True, timeout=30
    )
    assert done.returncode == weight_setter.EXIT_STALLED, done.stderr
    assert "did not return within 0.2s" in done.stderr
    # Every thread's stack, so the log names the frame the stuck tick is waiting in.
    assert "Current thread 0x" in done.stderr
    assert "concurrent/futures/thread.py" in done.stderr
