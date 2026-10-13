"""The chain watcher against a fake chain: what it records is what miners may submit."""

from __future__ import annotations

import datetime as dt
from functools import partial

import pytest
import sqlalchemy as sa

import db as store_pkg
from chain import watcher
from chain.types import NETUID, ChainHead, MetagraphSnapshot, NeuronInfo
from db import models

T0 = dt.datetime(2026, 10, 1, 12, 0, tzinfo=dt.timezone.utc)


def neuron(uid: int, hot: str, cold: str = "cold", block: int = 1000) -> NeuronInfo:
    return NeuronInfo(uid=uid, hotkey=hot, coldkey=cold, stake=1.0, block_at_registration=block)


class FakeChain:
    # A chain the test moves by hand; call counts prove an unchanged tip costs one head query.

    def __init__(self, block: int = 100, neurons=()) -> None:
        self.block = block
        self.neurons = tuple(neurons)
        self.head_calls = 0
        self.metagraph_calls = 0

    def head(self) -> ChainHead:
        self.head_calls += 1
        return ChainHead(block=self.block, timestamp=T0 + dt.timedelta(seconds=12 * self.block))

    def metagraph(self, netuid: int, block: int | None = None) -> MetagraphSnapshot:
        self.metagraph_calls += 1
        at = block if block is not None else self.block
        return MetagraphSnapshot(
            netuid=netuid,
            block=at,
            timestamp=T0 + dt.timedelta(seconds=12 * at),
            neurons=self.neurons,
        )


def block_time(block: int) -> dt.datetime:
    # Stands in for the archive lookup; a block's time is a pure function of its height.
    return T0 + dt.timedelta(seconds=12 * block)


def sink_for(store):
    return partial(store.registrations.publish_snapshot, block_time=block_time)


def discard(head, metagraph) -> None:
    # A publish that stores nothing, for tests of the poll loop alone.
    pass


def registrations(store) -> list[tuple[int, str, int]]:
    with store_pkg.session_scope(store.sessions) as session:
        rows = session.execute(
            sa.select(models.Registration).order_by(models.Registration.id)
        ).scalars()
        return [(r.uid, r.ss58_hot, r.block) for r in rows]


# ── The poll loop ─────────────────────────────────────────────────────────


def test_a_tip_that_has_not_moved_costs_one_head_query():
    chain = FakeChain(block=100, neurons=[neuron(0, "alice")])
    sink = discard
    last = watcher.step(chain, sink, NETUID, None)
    assert last == 100 and chain.metagraph_calls == 1
    assert watcher.step(chain, sink, NETUID, last) == 100
    assert chain.metagraph_calls == 1, "an unchanged tip must not fetch the metagraph"


def test_the_snapshot_is_taken_at_the_block_the_head_reported():
    # The snapshot is taken at the head's block, not "now", or registrations get wrong blocks.
    chain = FakeChain(block=250, neurons=[neuron(0, "alice")])
    seen = []

    def recording(head, metagraph):
        seen.append((head.block, metagraph.block))

    watcher.step(chain, recording, NETUID, None)
    assert seen == [(250, 250)]


def test_one_bad_tick_does_not_stop_the_watcher():
    class Flaky(FakeChain):
        def head(self):
            self.head_calls += 1
            if self.head_calls == 1:
                raise RuntimeError("node hiccup")
            return super().head()

    chain = Flaky(block=100, neurons=[neuron(0, "alice")])
    ticks = {"n": 0}

    def sleep(_seconds: float) -> None:
        ticks["n"] += 1
        if ticks["n"] >= 3:
            raise KeyboardInterrupt

    watcher.run(chain, discard, netuid=NETUID, sleep=sleep)
    assert chain.metagraph_calls == 1, "it recovered and published after the bad tick"


# ── What reaches the store ────────────────────────────────────────────────


def test_the_first_snapshot_records_every_neuron(store):
    chain = FakeChain(block=100, neurons=[neuron(0, "alice", block=10), neuron(1, "bob", block=20)])
    watcher.step(chain, sink_for(store), NETUID, None)
    assert registrations(store) == [(0, "alice", 10), (1, "bob", 20)]


def test_a_registration_carries_its_own_block_not_the_snapshots(store):
    # A uid seen late still records its own block, since that block makes it a distinct slot.
    chain = FakeChain(block=9_999, neurons=[neuron(0, "alice", block=17)])
    watcher.step(chain, sink_for(store), NETUID, None)
    assert registrations(store) == [(0, "alice", 17)]


def test_an_unchanged_metagraph_writes_nothing(store):
    chain = FakeChain(block=100, neurons=[neuron(0, "alice", block=10)])
    sink = sink_for(store)
    last = watcher.step(chain, sink, NETUID, None)
    chain.block = 101
    watcher.step(chain, sink, NETUID, last)
    assert registrations(store) == [(0, "alice", 10)], "a quiet tick must not add a row"


def test_a_re_registration_is_a_new_row_and_so_a_new_slot(store):
    # Registering again buys another submission by leaving another row behind.
    chain = FakeChain(block=100, neurons=[neuron(0, "alice", block=10)])
    sink = sink_for(store)
    last = watcher.step(chain, sink, NETUID, None)
    assert store.registrations.available_slots("alice") == 1

    # uid 0 is taken over by a new hotkey at a later block.
    chain.block, chain.neurons = 200, (neuron(0, "carol", block=150),)
    last = watcher.step(chain, sink, NETUID, last)
    assert registrations(store) == [(0, "alice", 10), (0, "carol", 150)]
    assert store.registrations.available_slots("carol") == 1

    # And alice registering again on a different uid buys her a second.
    chain.block, chain.neurons = 300, (neuron(0, "carol", block=150), neuron(1, "alice", block=250))
    watcher.step(chain, sink, NETUID, last)
    assert store.registrations.available_slots("alice") == 2


def test_the_same_block_is_never_recorded_twice(store):
    # The watcher must be idempotent per block: a restart replays the same snapshot.
    chain = FakeChain(block=100, neurons=[neuron(0, "alice", block=10)])
    sink = sink_for(store)
    watcher.step(chain, sink, NETUID, None)
    watcher.step(chain, sink, NETUID, None)  # a fresh start, same tip
    assert registrations(store) == [(0, "alice", 10)]


def test_an_empty_metagraph_is_refused_rather_than_recorded(store):
    # A degraded payload must not be read as "everyone deregistered".
    chain = FakeChain(block=100, neurons=[neuron(0, "alice", block=10)])
    sink = sink_for(store)
    watcher.step(chain, sink, NETUID, None)
    chain.block, chain.neurons = 200, ()
    watcher.step(chain, sink, NETUID, 100)
    assert registrations(store) == [(0, "alice", 10)]


def test_the_archive_is_only_asked_about_registrations_that_changed(store):
    # One archive RPC per real registration, never per tick, so a quiet subnet costs nothing.
    asked: list[int] = []

    def counting_block_time(block: int) -> dt.datetime:
        asked.append(block)
        return block_time(block)

    chain = FakeChain(block=100, neurons=[neuron(0, "alice", block=10)])
    sink = partial(store.registrations.publish_snapshot, block_time=counting_block_time)
    last = watcher.step(chain, sink, NETUID, None)
    assert asked == [10]
    chain.block = 101
    watcher.step(chain, sink, NETUID, last)
    assert asked == [10], "an unchanged tick must not touch the archive"


@pytest.mark.parametrize("uid_count", [1, 5, 32])
def test_a_backfill_records_the_whole_subnet_once(store, uid_count):
    chain = FakeChain(
        block=5_000, neurons=[neuron(i, f"hot{i}", block=100 + i) for i in range(uid_count)]
    )
    sink = sink_for(store)
    watcher.step(chain, sink, NETUID, None)
    assert len(registrations(store)) == uid_count
    chain.block += 1
    watcher.step(chain, sink, NETUID, 5_000)
    assert len(registrations(store)) == uid_count


def test_same_keys_and_uid_with_new_registration_block_are_recorded(store):
    chain = FakeChain(block=100, neurons=[neuron(0, "alice", block=10)])
    sink = sink_for(store)
    watcher.step(chain, sink, NETUID, None)
    chain.block, chain.neurons = 200, (neuron(0, "alice", block=150),)
    watcher.step(chain, sink, NETUID, 100)
    assert registrations(store) == [(0, "alice", 10), (0, "alice", 150)]


def test_missing_sdk_registration_blocks_cannot_create_fresh_eligibility(monkeypatch):
    import importlib.util
    import sys
    from pathlib import Path
    from types import SimpleNamespace

    # Exercise the real adapter without requiring chain credentials or the SDK in a test venv.
    monkeypatch.setitem(sys.modules, "bittensor", SimpleNamespace())
    source = Path(__file__).resolve().parents[1] / "validator/chain/finney.py"
    spec = importlib.util.spec_from_file_location("chain._snapshot_test", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    meta = SimpleNamespace(
        n=SimpleNamespace(item=lambda: 1), block=SimpleNamespace(item=lambda: 100)
    )
    with pytest.raises(RuntimeError, match="registration blocks"):
        module.to_snapshot(NETUID, meta, T0)
    meta.block_at_registration = [10]
    meta.uids, meta.hotkeys, meta.coldkeys, meta.stake = [0], ["alice"], ["cold"], [1.0]
    assert module.to_snapshot(NETUID, meta, T0).neurons[0].block_at_registration == 10
