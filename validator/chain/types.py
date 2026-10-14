"""Immutable chain-state contracts - plain Python, no IO, no SDK types, so tests need no node."""

from __future__ import annotations

import dataclasses as dc
import datetime as dt

# Subnet 66 on the Bittensor mainnet, polled from Finney by default.
NETUID = 66
FINNEY = "finney"
# Registration blocks older than the lite node's ~300-block window need an archive for their times.
ARCHIVE = "archive"

# Blocks are ~12s apart; polling at half that comfortably catches every head.
BLOCK_SECONDS = 12
POLL_INTERVAL_SECONDS = BLOCK_SECONDS / 2


@dc.dataclass(frozen=True, slots=True)
class ChainHead:
    # The current chain tip: its block number and wall-clock time (UTC).

    block: int
    timestamp: dt.datetime


@dc.dataclass(frozen=True, slots=True)
class NeuronInfo:
    # One neuron; `block_at_registration` is its own height, which makes each slot a row.

    uid: int
    hotkey: str
    coldkey: str
    stake: float
    block_at_registration: int


@dc.dataclass(frozen=True, slots=True)
class MetagraphSnapshot:
    # The subnet's uid -> hot/cold key assignments captured at a single block.

    netuid: int
    block: int
    timestamp: dt.datetime
    neurons: tuple[NeuronInfo, ...]


@dc.dataclass(frozen=True, slots=True)
class MetagraphView:
    # What the weight setter needs from the metagraph: the uids, and who holds them.

    uids: tuple[int, ...]
    uid_by_hotkey: dict[str, int]


@dc.dataclass(frozen=True, slots=True)
class SubnetParams:
    # Startup state: our own uid, and the subnet's weight timing.

    uid: int
    tempo: int
    weights_rate_limit: int


@dc.dataclass(frozen=True, slots=True)
class PollState:
    # Per-tick chain state at the tip: the tip, blocks since our uid last set weights, and the
    # block of the subnet's most recent epoch (LastMechansimStepBlock).

    current_block: int
    blocks_since_last_update: int
    last_epoch_block: int


@dc.dataclass(frozen=True, slots=True)
class WeightPlan:
    # A uid-aligned vector, and whether it is fit to submit.

    uids: tuple[int, ...]
    weights: tuple[float, ...]
    submittable: bool
    skip_reason: str | None
    summary: str
