"""The `WeightChain` protocol - the weight setter's only view of the network, faked in tests."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from .types import MetagraphView, PollState, SubnetParams


@runtime_checkable
class WeightChain(Protocol):
    # What the weight setter needs from the chain; finney.py is the real one.

    def params(self, netuid: int) -> SubnetParams:
        # Our uid plus the subnet's tempo and rate limit. Raises if not registered.
        ...

    def poll(self, netuid: int, uid: int) -> PollState:
        # The tip, and blocks since our uid last set weights.
        ...

    def metagraph(self, netuid: int) -> MetagraphView:
        # The subnet's uids and hotkey -> uid map.
        ...

    def set_weights(self, netuid: int, uids: Sequence[int], weights: Sequence[float]) -> bool:
        # Submit a weight vector; True when the chain accepted it.
        ...
