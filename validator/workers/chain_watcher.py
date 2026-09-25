"""Stream subnet registrations into the store; without it nobody can submit."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from functools import partial
from pathlib import Path

from loguru import logger

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db  # noqa: E402
from chain.types import ARCHIVE, FINNEY, NETUID, POLL_INTERVAL_SECONDS  # noqa: E402
from chain.watcher import run  # noqa: E402


@dataclass(frozen=True, slots=True)
class ChainWatcherConfig:
    # Which chain and subnet the watcher polls, and how often.
    network: str = FINNEY
    # Historical block times come from an archive; the lite node prunes old state.
    archive_network: str = ARCHIVE
    netuid: int = NETUID
    poll_interval: float = POLL_INTERVAL_SECONDS

    @classmethod
    def from_env(cls) -> ChainWatcherConfig:
        # Override each default from its env var.
        d = cls()
        return cls(
            network=os.getenv("BITTENSOR_NETWORK", d.network),
            archive_network=os.getenv("BITTENSOR_ARCHIVE_NETWORK", d.archive_network),
            netuid=int(os.getenv("NETUID", str(d.netuid))),
            poll_interval=float(os.getenv("CHAIN_POLL_SECONDS", str(d.poll_interval))),
        )


def main() -> None:
    # Poll the chain and record registration changes until interrupted.
    from chain.finney import BittensorChainSource

    config = ChainWatcherConfig.from_env()
    store = db.connect()
    try:
        source = BittensorChainSource(
            network=config.network, archive_network=config.archive_network
        )
        # block_date comes from the source's cached archive resolver, called for changed uids only.
        publish = partial(
            store.registrations.publish_snapshot, block_time=source.registration_block_time
        )
        logger.info(
            f"[watcher] starting network={config.network} archive={config.archive_network} "
            f"netuid={config.netuid} poll={config.poll_interval:.1f}s"
        )
        run(source, publish, netuid=config.netuid, poll_interval=config.poll_interval)
    finally:
        store.close()


if __name__ == "__main__":
    main()
