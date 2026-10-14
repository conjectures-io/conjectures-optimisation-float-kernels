"""A registration block's on-chain time: lite node, then archive, then an estimate from the tip."""

from __future__ import annotations

import datetime as dt
import time
from collections.abc import Callable

from loguru import logger

from axiom import config_error, get_events

from .types import BLOCK_SECONDS, ChainHead

# The lite node keeps state for roughly the last 300 blocks; stay inside that so its read can land.
LITE_STATE_BLOCKS = 256
# After an archive failure, estimate for this long before trying it again, so a down archive costs
# one connect timeout per window instead of one per poll.
ARCHIVE_RETRY_SECONDS = 300.0

# One node's read of a block's time; raises when the node cannot answer.
BlockTime = Callable[[int], dt.datetime]


class BlockTimeResolver:
    # A down archive must not stop registrations being recorded, so its last resort is an estimate.
    # Exact times are memoised as they never change; an estimate is not, so it is retried.

    def __init__(
        self,
        lite: BlockTime,
        archive: BlockTime,
        *,
        retry_seconds: float = ARCHIVE_RETRY_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._lite = lite
        self._archive = archive
        self._retry_seconds = retry_seconds
        self._clock = clock
        self._head: ChainHead | None = None
        self._archive_retry_at: float | None = None
        self._cache: dict[int, dt.datetime] = {}

    def observe(self, head: ChainHead) -> None:
        # The latest tip: it decides whether the lite node still has a block, and anchors estimates.
        self._head = head

    def __call__(self, block: int) -> dt.datetime:
        # The block's time, exact when a node can tell us, else estimated from the tip.
        cached = self._cache.get(block)
        if cached is not None:
            return cached
        exact = self._exact(block)
        if exact is None:
            return self._estimate(block)
        self._cache[block] = exact
        return exact

    def _exact(self, block: int) -> dt.datetime | None:
        # A fresh registration is still on the lite node; only older blocks need the archive.
        if self._head is not None and self._head.block - block <= LITE_STATE_BLOCKS:
            try:
                return self._lite(block)
            except Exception as exc:  # noqa: BLE001 - the archive may still have it
                logger.warning(f"[chain] lite node has no time for block {block}: {exc}")
        if self._archive_retry_at is not None and self._clock() < self._archive_retry_at:
            return None
        try:
            found = self._archive(block)
        except Exception as exc:  # noqa: BLE001 - fall back to an estimate, never fail the tick
            self._archive_retry_at = self._clock() + self._retry_seconds
            logger.warning(
                f"[chain] archive read failed, estimating block times for "
                f"{self._retry_seconds:.0f}s: {exc}"
            )
            get_events().warning(
                "chain_read_failed", node="archive", fallback="estimate", error=config_error(exc)
            )
            return None
        self._archive_retry_at = None
        return found

    def _estimate(self, block: int) -> dt.datetime:
        # Blocks are a fixed BLOCK_SECONDS apart, so count back from the tip's on-chain time.
        if self._head is None:
            raise RuntimeError(f"no time for block {block}: no node answered and no tip is known")
        estimate = self._head.timestamp - dt.timedelta(
            seconds=BLOCK_SECONDS * (self._head.block - block)
        )
        logger.warning(
            f"[chain] block {block} time estimated as {estimate.isoformat()} "
            f"from tip {self._head.block}"
        )
        return estimate
