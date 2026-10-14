"""The live chain via the bittensor SDK (./setup.sh --chain); SDK objects never leave this file."""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from typing import Protocol, SupportsInt, cast

from loguru import logger

try:
    import bittensor as bt
except ModuleNotFoundError as exc:  # pragma: no cover - depends on how the venv was built
    # The SDK is only in the chain workers' venv; say what to install instead of a bare ImportError.
    raise ModuleNotFoundError(
        "the bittensor SDK is not installed. It is needed only by the chain watcher and "
        "the weight setter; install it with `./setup.sh --chain` (or "
        "`pip install -r requirements/chain.txt`)."
    ) from exc

from .block_time import BlockTimeResolver
from .types import (
    ARCHIVE,
    FINNEY,
    ChainHead,
    MetagraphSnapshot,
    MetagraphView,
    NeuronInfo,
    PollState,
    SubnetParams,
)


def to_snapshot(netuid: int, meta, timestamp: dt.datetime) -> MetagraphSnapshot:
    # Registration time controls eligibility; missing data must not invent a new registration.
    count = meta.n.item()
    snapshot_block = meta.block.item()
    reg_blocks = getattr(meta, "block_at_registration", None)
    if count and (reg_blocks is None or len(reg_blocks) != count):
        raise RuntimeError("metagraph registration blocks are unavailable; retrying chain snapshot")
    if reg_blocks is None:
        reg_blocks = []
    neurons = tuple(
        NeuronInfo(
            uid=int(meta.uids[i]),
            hotkey=str(meta.hotkeys[i]),
            coldkey=str(meta.coldkeys[i]),
            stake=float(meta.stake[i]),
            block_at_registration=int(reg_blocks[i]),
        )
        for i in range(count)
    )
    return MetagraphSnapshot(
        netuid=netuid, block=snapshot_block, timestamp=timestamp, neurons=neurons
    )


class BittensorChainSource:
    # Reads subnet state from a live node; old block times come from a lazy archive, if it is up.

    def __init__(
        self,
        network: str = FINNEY,
        *,
        archive_network: str = ARCHIVE,
        subtensor: bt.Subtensor | None = None,
        archive: bt.Subtensor | None = None,
    ) -> None:
        # Connect the lite node now; the archive node only on the first old-block lookup.
        self._network = network
        self._archive_network = archive_network
        self._subtensor = subtensor or bt.Subtensor(network=network)
        logger.info(f"[chain] connected to {network}")
        self._archive = archive
        self._block_times = BlockTimeResolver(
            lite=lambda block: self._query_block_time(self._subtensor, block),
            archive=self._archive_block_time,
        )

    def _archive_subtensor(self) -> bt.Subtensor:
        # Connected on first use, so a watcher that sees no registration never pays for it.
        if self._archive is None:
            self._archive = bt.Subtensor(network=self._archive_network)
            logger.info(f"[chain] connected to archive {self._archive_network}")
        return self._archive

    def _archive_block_time(self, block: int) -> dt.datetime:
        # A failed read drops the connection, so the next attempt reconnects instead of reusing it.
        try:
            return self._query_block_time(self._archive_subtensor(), block)
        except Exception:
            self._archive = None
            raise

    @staticmethod
    def _query_block_time(subtensor: bt.Subtensor, block: int) -> dt.datetime:
        # On-chain time is milliseconds since the epoch (the Timestamp.Now storage value).
        block_hash = subtensor.get_block_hash(block)
        raw_ms = subtensor.substrate.query("Timestamp", "Now", block_hash=block_hash).value
        return dt.datetime.fromtimestamp(int(raw_ms) / 1000, tz=dt.timezone.utc)

    def registration_block_time(self, block: int) -> dt.datetime:
        # A registration block's on-chain time; see BlockTimeResolver for where it comes from.
        return self._block_times(block)

    def head(self) -> ChainHead:
        # The current tip and its on-chain time.
        block = self._subtensor.get_current_block()
        # The head is recent, so its time is on the lite node - no archive needed.
        head = ChainHead(block=block, timestamp=self._query_block_time(self._subtensor, block))
        self._block_times.observe(head)
        return head

    def metagraph(self, netuid: int, block: int | None = None) -> MetagraphSnapshot:
        # The subnet's neurons at a block (the tip by default), stamped with its time.
        meta = self._subtensor.metagraph(netuid=netuid, block=block)
        return to_snapshot(netuid, meta, self._query_block_time(self._subtensor, meta.block.item()))


class _HasScaleValue(Protocol):
    # A decoded SCALE query result.
    value: object


class BittensorWeightChain:
    # Sets weights with the validator's wallet, and reads the timing that governs when.

    def __init__(
        self,
        *,
        network: str = FINNEY,
        wallet_name: str = "default",
        wallet_hotkey: str = "default",
        wallet_path: str = "~/.bittensor/wallets",
        subtensor: bt.Subtensor | None = None,
        wallet: bt.Wallet | None = None,
    ) -> None:
        # Connect to the chain with the wallet that signs our weights.
        self._subtensor = subtensor if subtensor is not None else bt.Subtensor(network=network)
        self._wallet = (
            wallet
            if wallet is not None
            else bt.Wallet(name=wallet_name, hotkey=wallet_hotkey, path=wallet_path)
        )
        logger.info(f"[chain] connected to {network} as wallet {wallet_name}/{wallet_hotkey}")

    def params(self, netuid: int) -> SubnetParams:
        # Our uid, the subnet's tempo and its weights rate limit.
        uid = self._subtensor.get_uid_for_hotkey_on_subnet(self._wallet.hotkey.ss58_address, netuid)
        if uid is None:
            raise RuntimeError(f"the wallet hotkey is not registered on netuid {netuid}")
        tempo = self._subtensor.tempo(netuid)
        rate_limit = self._subtensor.weights_rate_limit(netuid)
        return SubnetParams(
            uid=int(uid),
            tempo=int(tempo) if tempo is not None else 0,
            weights_rate_limit=int(rate_limit) if rate_limit is not None else 0,
        )

    def current_block(self) -> int:
        # The chain's tip.
        return int(self._subtensor.get_current_block())

    def poll(self, netuid: int, uid: int) -> PollState:
        # The tip, how long since our uid last set weights, and the last epoch's block, all at
        # one block; LastMechansimStepBlock is the pallet's own spelling.
        block = self.current_block()
        since = self._subtensor.blocks_since_last_update(netuid, uid, block=block)
        epoch = self._storage("LastMechansimStepBlock", netuid, block)
        return PollState(
            current_block=block,
            blocks_since_last_update=int(since) if since is not None else 0,
            last_epoch_block=int(cast(SupportsInt, epoch)),
        )

    def _storage(self, name: str, netuid: int, block: int) -> object:
        # One subtensor storage item for the subnet; `.value` is the decoded SCALE value.
        query = self._subtensor.query_subtensor(name, block=block, params=[netuid])
        return cast(_HasScaleValue, query).value

    def metagraph(self, netuid: int) -> MetagraphView:
        # The subnet's uids and the hotkey -> uid map scoring needs.
        meta = self._subtensor.metagraph(netuid=netuid)
        count = meta.n.item()
        return MetagraphView(
            uids=tuple(int(meta.uids[i]) for i in range(count)),
            uid_by_hotkey={str(meta.hotkeys[i]): int(meta.uids[i]) for i in range(count)},
        )

    def set_weights(self, netuid: int, uids: Sequence[int], weights: Sequence[float]) -> bool:
        # Submit the weights and wait for inclusion; True when the chain took them.
        response = self._subtensor.set_weights(
            wallet=self._wallet,
            netuid=netuid,
            uids=list(uids),
            weights=list(weights),
            wait_for_inclusion=True,
            wait_for_finalization=False,
        )
        if not response.success:
            logger.warning(
                f"[chain] set_weights rejected (error={response.error} message={response.message})"
            )
        return bool(response.success)
