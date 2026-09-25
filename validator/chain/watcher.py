"""The chain watcher: poll a `ChainSource`, publish each new block's state; IO is injected."""

from __future__ import annotations

import time
from collections.abc import Callable

from loguru import logger

from .source import ChainSource
from .types import NETUID, POLL_INTERVAL_SECONDS, ChainHead, MetagraphSnapshot

# Where each new block's state goes; in production, the store's registrations.
Publish = Callable[[ChainHead, MetagraphSnapshot], None]


def step(source: ChainSource, publish: Publish, netuid: int, last_block: int | None) -> int | None:
    # One poll: publish if the tip moved, return the last-seen block; no sleep, so testable.
    head = source.head()
    if last_block is not None and head.block <= last_block:
        return last_block
    metagraph = source.metagraph(netuid, head.block)
    publish(head, metagraph)
    return head.block


def run(
    source: ChainSource,
    publish: Publish,
    *,
    netuid: int = NETUID,
    poll_interval: float = POLL_INTERVAL_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    # Poll forever; a failed tick is logged and retried, since a missed registration blocks a miner.
    last_block: int | None = None
    try:
        while True:
            try:
                last_block = step(source, publish, netuid, last_block)
            except Exception as exc:  # noqa: BLE001 - one bad tick must not kill the watcher
                logger.exception(f"[watcher] poll failed: {exc}")
            sleep(poll_interval)
    except KeyboardInterrupt:
        logger.info("[watcher] interrupted")
