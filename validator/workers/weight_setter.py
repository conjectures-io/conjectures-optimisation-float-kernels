"""Score the round and set weights once an epoch, recording every outcome and its reasons."""

from __future__ import annotations

import dataclasses as dc
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from loguru import logger

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db  # noqa: E402
import scoring  # noqa: E402
from chain.schedule import should_set  # noqa: E402
from chain.types import BLOCK_SECONDS, FINNEY, NETUID, SubnetParams, WeightPlan  # noqa: E402
from chain.weights import WeightChain  # noqa: E402

Action = Literal["wait", "skip", "set", "failed"]


@dc.dataclass(frozen=True, slots=True)
class WeightSetterConfig:
    # Wallet, subnet, burn and timing knobs for the weight setter.
    network: str = FINNEY
    netuid: int = NETUID
    wallet_name: str = "default"
    wallet_hotkey: str = "default"
    wallet_path: str = "~/.bittensor/wallets"

    # The uid unclaimed emission burns to; uid 0 by convention.
    burn_uid: int = 0
    # Send everything to the burn uid, for a paused or not-yet-open round.
    burn_mode: bool = False

    # Set this many blocks before the epoch ends: fresh for consensus, time left for inclusion.
    set_margin: int = 12
    # Idle sleep between ticks; about one block.
    poll_seconds: float = 12.0
    # Compute and record the vector, but do not submit it.
    dry_run: bool = False

    def __post_init__(self) -> None:
        # Refuse nonsensical knobs at startup, not mid-run.
        if self.burn_uid < 0:
            raise ValueError("burn_uid must be >= 0")
        if self.set_margin < 0:
            raise ValueError("set_margin must be >= 0")
        if self.poll_seconds <= 0:
            raise ValueError("poll_seconds must be positive")

    @classmethod
    def from_env(cls) -> WeightSetterConfig:
        # Override each default from its env var.
        d = cls()
        env = os.environ
        return cls(
            network=env.get("BITTENSOR_NETWORK", d.network),
            netuid=int(env.get("NETUID", str(d.netuid))),
            wallet_name=env.get("BITTENSOR_WALLET_NAME", d.wallet_name),
            wallet_hotkey=env.get("BITTENSOR_WALLET_HOTKEY", d.wallet_hotkey),
            wallet_path=env.get("BITTENSOR_WALLET_PATH", d.wallet_path),
            burn_uid=int(env.get("WEIGHT_BURN_UID", str(d.burn_uid))),
            burn_mode=env.get("WEIGHT_BURN_MODE", "").lower() in ("1", "true", "yes"),
            set_margin=int(env.get("WEIGHT_SET_MARGIN", str(d.set_margin))),
            poll_seconds=float(env.get("WEIGHT_POLL_SECONDS", str(d.poll_seconds))),
            dry_run=env.get("WEIGHT_DRY_RUN", "").lower() in ("1", "true", "yes"),
        )


@dc.dataclass(frozen=True, slots=True)
class StepResult:
    # What one tick did (set, wait or skip), and the numbers behind it for the log.
    action: Action
    reason: str
    epoch_blocks: int | None = None
    rate_off_blocks: int | None = None
    next_try_block: int | None = None
    next_try_blocks: int | None = None
    block: int | None = None
    plan: WeightPlan | None = None
    scoring: scoring.Scoring | None = None
    weight_set_id: int | None = None


def plan_for(
    store: db.Store,
    meta,
    config: WeightSetterConfig,
    scoring_config: scoring.ScoringConfig,
) -> tuple[WeightPlan, scoring.Scoring | None]:
    # The weight vector to set: all to the burn uid in burn mode, else from the scoring.
    if config.burn_mode:
        return scoring.to_vector({}, meta, burn_uid=config.burn_uid), None
    result = scoring.score(
        store.scoring.best_per_hotkey(),
        scoring_config,
        store.scoring.leader(scoring_config.max_ratio),
    )
    return scoring.to_vector(result.weights, meta, burn_uid=config.burn_uid), result


def step(
    chain: WeightChain,
    store: db.Store,
    config: WeightSetterConfig,
    scoring_config: scoring.ScoringConfig,
    params: SubnetParams,
) -> StepResult:
    # One tick: gate on the cadence, then score, record and submit.
    poll = chain.poll(config.netuid, params.uid)
    decision = should_set(
        current_block=poll.current_block,
        tempo=params.tempo,
        netuid=config.netuid,
        blocks_since_last_update=poll.blocks_since_last_update,
        weights_rate_limit=params.weights_rate_limit,
        set_margin=config.set_margin,
    )
    if not decision.proceed:
        return StepResult(
            "wait",
            "",
            epoch_blocks=decision.epoch_blocks,
            rate_off_blocks=decision.rate_off_blocks,
            next_try_block=decision.next_try_block,
            next_try_blocks=decision.next_try_block - poll.current_block,
        )

    meta = chain.metagraph(config.netuid)
    plan, result = plan_for(store, meta, config, scoring_config)
    summary = plan.summary + (f" | {result.summary()}" if result else " | burn mode")

    if not plan.submittable:
        weight_set_id = record(
            store, config, poll.current_block, plan, result, False, plan.skip_reason
        )
        return StepResult(
            "skip",
            plan.skip_reason or "unsubmittable vector",
            block=poll.current_block,
            plan=plan,
            scoring=result,
            weight_set_id=weight_set_id,
        )

    if config.dry_run:
        weight_set_id = record(
            store, config, poll.current_block, plan, result, False, "dry run: not submitted"
        )
        return StepResult(
            "skip",
            f"dry run: {summary}",
            block=poll.current_block,
            plan=plan,
            scoring=result,
            weight_set_id=weight_set_id,
        )

    accepted = chain.set_weights(config.netuid, list(plan.uids), list(plan.weights))
    weight_set_id = record(
        store,
        config,
        poll.current_block,
        plan,
        result,
        accepted,
        None if accepted else "the chain refused the vector",
    )
    if not accepted:
        return StepResult(
            "failed",
            "set_weights rejected",
            block=poll.current_block,
            plan=plan,
            scoring=result,
            weight_set_id=weight_set_id,
        )
    return StepResult(
        "set",
        f"block {poll.current_block}: {summary}",
        epoch_blocks=decision.epoch_blocks,
        block=poll.current_block,
        plan=plan,
        scoring=result,
        weight_set_id=weight_set_id,
    )


def record(
    store: db.Store,
    config: WeightSetterConfig,
    block: int,
    plan: WeightPlan,
    result: scoring.Scoring | None,
    accepted: bool,
    error: str | None,
) -> int:
    # Record the vector and its reasons in one transaction, even for a skipped or refused epoch.
    return store.scoring.record_weight_set(
        netuid=config.netuid,
        block=block,
        uids=list(plan.uids),
        weights=list(plan.weights),
        summary=plan.summary,
        accepted=accepted,
        dry_run=config.dry_run,
        error=error,
        snapshots=result.snapshots() if result else None,
    )


def run(
    chain: WeightChain,
    store: db.Store,
    config: WeightSetterConfig,
    scoring_config: scoring.ScoringConfig,
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    # Tick forever, logging each outcome; a failed tick is logged and retried next poll.
    params = chain.params(config.netuid)
    logger.info(
        f"[weights] running netuid={config.netuid} uid={params.uid} tempo={params.tempo} "
        f"rate_limit={params.weights_rate_limit} margin={config.set_margin} "
        f"burn_mode={config.burn_mode} burn_uid={config.burn_uid} dry_run={config.dry_run} "
        f"scoring=king-of-the-hill winner_share={scoring_config.winner_share:.2f}"
    )
    ticks = TickLog()
    try:
        while True:
            try:
                result = step(chain, store, config, scoring_config, params)
                ticks.report(result)
                if result.action == "set":
                    # Re-read tempo and the rate limit: both can change between epochs.
                    params = chain.params(config.netuid)
            except Exception as exc:  # noqa: BLE001 - a bad tick must not end the worker
                logger.exception(f"[weights] tick failed: {exc}")
            sleep(config.poll_seconds)
    except KeyboardInterrupt:
        logger.info("[weights] interrupted")


def human(seconds: float) -> str:
    # A duration as a short human string (s, m or h).
    if seconds < 90:
        return f"~{seconds:.0f}s"
    if seconds < 5400:
        return f"~{seconds / 60:.0f}m"
    return f"~{seconds / 3600:.1f}h"


class TickLog:
    # Log a wait only when its due block moves, so idle ticks do not bury real lines.

    def __init__(self) -> None:
        # The next-try block of the last wait logged.
        self._wait_key: int | None = None

    def report(self, result: StepResult) -> None:
        # Log a set, skip or failure every time, and a wait only when its due block moves.
        if result.action == "wait":
            if result.next_try_block != self._wait_key:
                self._wait_key = result.next_try_block
                seconds = (result.next_try_blocks or 0) * BLOCK_SECONDS
                logger.info(
                    f"[weights] waiting: rate-off ~{result.rate_off_blocks}b, "
                    f"epoch ~{result.epoch_blocks}b, next try block {result.next_try_block} "
                    f"in {human(seconds)}"
                )
            return
        self._wait_key = None
        if result.action == "set":
            logger.info(
                f"[weights] set: {result.reason}; next epoch in "
                f"{human((result.epoch_blocks or 0) * BLOCK_SECONDS)}"
            )
        elif result.action == "skip":
            logger.warning(f"[weights] skipped: {result.reason}")
        else:
            logger.error(f"[weights] failed: {result.reason}")


def main() -> None:
    # Build the chain client from env and run the weight setter.
    from chain.finney import BittensorWeightChain

    config = WeightSetterConfig.from_env()
    scoring_config = scoring.ScoringConfig.from_env()
    chain = BittensorWeightChain(
        network=config.network,
        wallet_name=config.wallet_name,
        wallet_hotkey=config.wallet_hotkey,
        wallet_path=config.wallet_path,
    )
    store = db.connect()
    try:
        run(chain, store, config, scoring_config)
    finally:
        store.close()


if __name__ == "__main__":
    main()
