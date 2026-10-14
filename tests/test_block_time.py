"""Registration block times: a down archive must cost an estimate, never a lost registration."""

from __future__ import annotations

import datetime as dt
from functools import partial

from test_chain_watcher import FakeChain, block_time, neuron, registrations

from chain import watcher
from chain.block_time import ARCHIVE_RETRY_SECONDS, LITE_STATE_BLOCKS, BlockTimeResolver
from chain.types import BLOCK_SECONDS, NETUID, ChainHead

TIP = 10_000


class Node:
    # One node's block-time read; `down` makes it raise, as an unreachable archive does.

    def __init__(self, *, down: bool = False) -> None:
        self.down = down
        self.asked: list[int] = []

    def __call__(self, block: int) -> dt.datetime:
        self.asked.append(block)
        if self.down:
            raise TimeoutError("timed out")
        return block_time(block)


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def resolver(lite: Node, archive: Node, clock: Clock | None = None) -> BlockTimeResolver:
    found = BlockTimeResolver(lite, archive, clock=clock or Clock())
    found.observe(ChainHead(block=TIP, timestamp=block_time(TIP)))
    return found


def test_a_fresh_registration_is_read_from_the_lite_node_without_the_archive():
    lite, archive = Node(), Node()
    assert resolver(lite, archive)(TIP - 3) == block_time(TIP - 3)
    assert lite.asked == [TIP - 3] and archive.asked == []


def test_an_old_registration_goes_straight_to_the_archive():
    # The lite node has pruned it, so asking would only cost a failed read.
    lite, archive = Node(), Node()
    old = TIP - LITE_STATE_BLOCKS - 1
    assert resolver(lite, archive)(old) == block_time(old)
    assert lite.asked == [] and archive.asked == [old]


def test_a_lite_miss_falls_back_to_the_archive():
    lite, archive = Node(down=True), Node()
    assert resolver(lite, archive)(TIP - 3) == block_time(TIP - 3)
    assert archive.asked == [TIP - 3]


def test_a_down_archive_yields_an_estimate_counted_back_from_the_tip():
    lite, archive = Node(), Node(down=True)
    old = TIP - 1_000
    estimate = resolver(lite, archive)(old)
    assert estimate == block_time(TIP) - dt.timedelta(seconds=BLOCK_SECONDS * 1_000)


def test_a_down_archive_is_not_asked_again_until_the_retry_window_passes():
    # One connect timeout per window, not one per registration or per poll.
    clock = Clock()
    lite, archive = Node(), Node(down=True)
    times = resolver(lite, archive, clock)
    times(TIP - 1_000)
    times(TIP - 1_001)
    assert archive.asked == [TIP - 1_000]

    clock.now = ARCHIVE_RETRY_SECONDS
    archive.down = False
    assert times(TIP - 1_001) == block_time(TIP - 1_001)
    assert archive.asked == [TIP - 1_000, TIP - 1_001]


def test_exact_times_are_memoised_and_estimates_are_not():
    clock = Clock()
    lite, archive = Node(), Node(down=True)
    times = resolver(lite, archive, clock)
    old = TIP - 1_000
    times(old)  # estimated
    clock.now = ARCHIVE_RETRY_SECONDS
    archive.down = False
    times(old)
    times(old)
    assert archive.asked == [old, old], "the estimate was retried, the exact time was kept"


def test_a_new_registration_is_recorded_while_the_archive_is_down(store):
    # The outage this guards against: every tick failed, so nobody new could submit.
    chain = FakeChain(block=TIP, neurons=[neuron(0, "alice", block=TIP - 1_000)])
    times = BlockTimeResolver(Node(), Node(down=True))
    times.observe(chain.head())
    sink = partial(store.registrations.publish_snapshot, block_time=times)
    watcher.step(chain, sink, NETUID, None)
    assert registrations(store) == [(0, "alice", TIP - 1_000)]
