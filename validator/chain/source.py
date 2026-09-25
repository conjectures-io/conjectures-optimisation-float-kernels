"""The `ChainSource` protocol - the watcher's only view of chain state, so fakes can stand in."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from .types import ChainHead, MetagraphSnapshot


@runtime_checkable
class ChainSource(Protocol):
    # A read-only source of subnet chain state; fakes in tests, finney.py in production.

    def head(self) -> ChainHead:
        # The current chain tip: latest block number and its timestamp.
        ...

    def metagraph(self, netuid: int, block: int | None = None) -> MetagraphSnapshot:
        # The metagraph snapshot for `netuid`, at `block` if given else the tip.
        ...
