"""Consume the coordinator publication and audit actual weight submission attempts."""

from __future__ import annotations

import dataclasses as dc
import os
import sys
import time
from pathlib import Path
from typing import Literal

from loguru import logger

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import rewards  # noqa: E402

import db  # noqa: E402
import log  # noqa: E402
from axiom import EventType, Severity, config_error, get_events, init  # noqa: E402
from chain.schedule import should_set  # noqa: E402
from chain.types import (  # noqa: E402
    BLOCK_SECONDS,
    FINNEY,
    NETUID,
    MetagraphView,
    WeightPlan,
)
from treasury import (  # noqa: E402
    TREASURY_UID,
    burn,
    competition_share_for,
    resolve_treasury,
    split,
    treasury_uid_for,
)

Action = Literal["wait", "skip", "set", "failed"]


def _flag(name: str, value: str) -> bool:
    # A yes/no setting; anything else refuses to start rather than guessing.
    normalized = value.strip().lower()
    if normalized in ("1", "true", "yes"):
        return True
    if normalized in ("0", "false", "no", ""):
        return False
    raise ValueError(f"{name} must be 1/true/yes or 0/false/no")


@dc.dataclass(frozen=True, slots=True)
class WeightSetterConfig:
    # Wallet, subnet, treasury, burn and timing knobs for the weight setter.
    network: str = FINNEY
    netuid: int = NETUID
    wallet_name: str = "default"
    wallet_hotkey: str = "default"
    wallet_path: str = "~/.bittensor/wallets"

    # Burn mode's destination for the competition's share (and, off mainnet without
    # WEIGHT_TREASURY_UID, the treasury's), defaulting to the treasury uid so on netuid 66 burn mode
    # pays the treasury everything.
    burn_uid: int = TREASURY_UID
    # Burn the competition's share, ignoring the scores, for a paused or not-yet-open round;
    # the treasury is still paid its share.
    burn_mode: bool = False

    # Set this many blocks before the epoch ends: fresh for consensus, time left for inclusion.
    set_margin: int = 12
    # Idle sleep between ticks; about one block.
    poll_seconds: float = 12.0
    # Compute and record the vector without submitting it, on unless WEIGHT_DRY_RUN=0.
    dry_run: bool = True
    # Off mainnet only: where the treasury share goes (default: the burn uid).
    treasury_override: int | None = None
    # The treasury's registered hotkey; when set, an epoch whose metagraph does not have it where
    # expected is skipped, so a reassigned treasury uid is never paid.
    treasury_hotkey: str | None = None
    # Off mainnet only: the competition's fraction of the validator's weight.
    competition_share_override: float | None = None

    def __post_init__(self) -> None:
        # Refuse nonsensical knobs at startup, not mid-run.
        if self.burn_uid < 0:
            raise ValueError("burn_uid must be >= 0")
        if self.treasury_override is not None and self.treasury_override < 0:
            raise ValueError("WEIGHT_TREASURY_UID must be >= 0")
        if self.treasury_hotkey is not None and not self.treasury_hotkey.strip():
            raise ValueError("WEIGHT_TREASURY_HOTKEY must not be blank")
        self.treasury_uid  # noqa: B018 - validates the override against the netuid now
        self.competition_share  # noqa: B018 - likewise
        if self.set_margin < 0:
            raise ValueError("set_margin must be >= 0")
        if self.poll_seconds <= 0:
            raise ValueError("poll_seconds must be positive")

    @property
    def treasury_uid(self) -> int:
        # The treasury's uid on this netuid; a code constant on mainnet.
        return treasury_uid_for(self.netuid, self.treasury_override, burn_uid=self.burn_uid)

    @property
    def competition_share(self) -> float:
        # The competition's fraction of the validator's weight; a code constant on mainnet.
        return competition_share_for(self.netuid, self.competition_share_override)

    @classmethod
    def from_env(cls) -> WeightSetterConfig:
        # Override each default from its env var.
        d = cls()
        env = os.environ
        treasury_uid = env.get("WEIGHT_TREASURY_UID", "").strip()
        share = env.get("WEIGHT_COMPETITION_SHARE", "").strip()
        return cls(
            network=env.get("BITTENSOR_NETWORK", d.network),
            netuid=int(env.get("NETUID", str(d.netuid))),
            wallet_name=env.get("BITTENSOR_WALLET_NAME", d.wallet_name),
            wallet_hotkey=env.get("BITTENSOR_WALLET_HOTKEY", d.wallet_hotkey),
            wallet_path=env.get("BITTENSOR_WALLET_PATH", d.wallet_path),
            burn_uid=int(env.get("WEIGHT_BURN_UID", str(d.burn_uid))),
            burn_mode=_flag("WEIGHT_BURN_MODE", env.get("WEIGHT_BURN_MODE", "0")),
            set_margin=int(env.get("WEIGHT_SET_MARGIN", str(d.set_margin))),
            poll_seconds=float(env.get("WEIGHT_POLL_SECONDS", str(d.poll_seconds))),
            dry_run=_flag("WEIGHT_DRY_RUN", env.get("WEIGHT_DRY_RUN", "1")),
            treasury_override=int(treasury_uid) if treasury_uid else None,
            treasury_hotkey=env.get("WEIGHT_TREASURY_HOTKEY", "").strip() or None,
            competition_share_override=float(share) if share else None,
        )


@dc.dataclass(frozen=True, slots=True)
class StepResult:
    # What one tick did (set, wait, skip or failed), and the numbers behind it for the log.
    action: Action
    reason: str
    epoch_blocks: int | None = None
    rate_off_blocks: int | None = None
    next_try_block: int | None = None
    next_try_blocks: int | None = None
    block: int | None = None
    plan: WeightPlan | None = None
    scoring: rewards.Scoring | None = None
    publication: dict[str, object] | None = None
    weight_set_id: int | None = None


def plan_for(
    store: db.Store,
    meta: MetagraphView,
    config: WeightSetterConfig,
) -> tuple[WeightPlan, rewards.Scoring | None, dict[str, object] | None]:
    # The validator's vector, its scoring and its published snapshot, where scoring that raises pays
    # the treasury everything rather than failing the tick, since an epoch with no weight set cannot
    # be made up.
    treasury, missing = resolve_treasury(
        meta,
        netuid=config.netuid,
        treasury_uid=config.treasury_uid,
        treasury_hotkey=config.treasury_hotkey,
    )
    if treasury is None:
        plan = WeightPlan((), (), False, missing, f"treasury_hotkey={config.treasury_hotkey}")
        return plan, None, None
    share = config.competition_share
    if config.burn_mode:
        plan = burn(meta, treasury_uid=treasury, burn_uid=config.burn_uid, competition_share=share)
        return plan, None, None
    try:
        publication = store.publications.publication()
        result = rewards.score(publication)
        snapshot = publication
    except Exception as exc:  # noqa: BLE001 - any scoring failure pays the treasury
        logger.exception(f"[weights] scoring failed; paying the treasury this epoch: {exc}")
        plan = split(None, meta, treasury_uid=treasury, reason=f"scoring failed: {exc}")
        return plan, None, None
    plan = split(result.weights, meta, treasury_uid=treasury, competition_share=share)
    return plan, result, snapshot


def step(chain, store, config, params=None):
    """An idle poll never creates a row. Record only actual or explicit dry-run attempts."""
    block = chain.current_block()
    params = params if params is not None else chain.params(config.netuid)
    poll = chain.poll(config.netuid, params.uid)
    decision = should_set(
        current_block=poll.current_block,
        tempo=params.tempo,
        last_epoch_block=poll.last_epoch_block,
        blocks_since_last_update=poll.blocks_since_last_update,
        weights_rate_limit=params.weights_rate_limit,
        set_margin=config.set_margin,
    )
    if not decision.proceed:
        return StepResult(
            "wait",
            "waiting for chain window",
            block=block,
            epoch_blocks=decision.epoch_blocks,
            rate_off_blocks=decision.rate_off_blocks,
            next_try_block=decision.next_try_block,
            next_try_blocks=decision.next_try_block - poll.current_block,
        )
    meta = chain.metagraph(config.netuid)
    plan, result, publication = plan_for(store, meta, config)
    if not plan.submittable:
        return StepResult(
            "skip", plan.skip_reason or "unsubmittable vector", block=block, plan=plan
        )
    if config.dry_run and store.publications.already_dry_run(config.netuid, block):
        return StepResult("skip", "dry run already recorded at this block", block=block, plan=plan)
    wid = store.publications.record(
        netuid=config.netuid,
        block=block,
        plan=plan,
        publication=publication,
        dry_run=config.dry_run,
    )
    accepted, error = False, None
    if config.dry_run:
        action, error = "skip", "dry run: not submitted"
    else:
        try:
            accepted = chain.set_weights(config.netuid, list(plan.uids), list(plan.weights))
            action, error = ("set", None) if accepted else ("failed", "chain refused vector")
        except Exception as exc:
            action, error = "failed", f"chain submission outcome unknown: {exc}"
    store.publications.outcome(wid, accepted, error)
    emit_plan(
        "info" if accepted or config.dry_run else "error",
        "weights_planned" if config.dry_run else "weights_set" if accepted else "weights_failed",
        config,
        meta,
        block,
        plan,
        error,
    )
    return StepResult(
        action,
        error or plan.summary,
        block=block,
        plan=plan,
        scoring=result,
        publication=publication,
        weight_set_id=wid,
        epoch_blocks=decision.epoch_blocks,
    )


def emit_plan(
    severity: Severity,
    event_type: EventType,
    config: WeightSetterConfig,
    meta: MetagraphView,
    block: int,
    plan: WeightPlan,
    error: str | None = None,
) -> None:
    # One event for a tick's outcome: the vector's nonzero entries and how it splits.
    treasury, _ = resolve_treasury(
        meta,
        netuid=config.netuid,
        treasury_uid=config.treasury_uid,
        treasury_hotkey=config.treasury_hotkey,
    )
    paid = {uid: w for uid, w in zip(plan.uids, plan.weights, strict=True) if w > 0}
    get_events().emit(
        severity,
        event_type,
        block=block,
        dry_run=config.dry_run,
        burn_mode=config.burn_mode,
        uids=list(paid),
        weights=list(paid.values()),
        treasury_uid=treasury,
        treasury_share=paid.get(treasury, 0.0) if treasury is not None else None,
        competition_share=config.competition_share,
        burn_uid=config.burn_uid,
        # Off mainnet the treasury may be the burn uid; the two shares are then one number.
        burn_share=paid.get(config.burn_uid, 0.0) if config.burn_uid != treasury else None,
        miners=sum(1 for uid in paid if uid not in (treasury, config.burn_uid)),
        summary=plan.summary,
        error=error,
    )


def run(chain, store, config, *, sleep=time.sleep):
    logger.info(
        "[weights] netuid={} dry_run={} coordinator board/rank publication",
        config.netuid,
        config.dry_run,
    )
    ticks = TickLog()
    try:
        while True:
            try:
                ticks.report(step(chain, store, config))
            except Exception as exc:
                logger.exception("[weights] tick failed: {}", exc)
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

    log.setup()
    events = init("competition-weight-setter")
    try:
        config = WeightSetterConfig.from_env()
    except Exception as exc:
        events.error("service_misconfigured", error=config_error(exc))
        raise
    events.bind(netuid=config.netuid, network=config.network)
    events.info(
        "service_started",
        dry_run=config.dry_run,
        burn_mode=config.burn_mode,
        treasury_uid=config.treasury_uid,
        competition_share=config.competition_share,
    )
    chain = BittensorWeightChain(
        network=config.network,
        wallet_name=config.wallet_name,
        wallet_hotkey=config.wallet_hotkey,
        wallet_path=config.wallet_path,
    )
    store = db.connect()
    try:
        run(chain, store, config)
    finally:
        events.info("service_stopped", reason="interrupted")
        store.close()


if __name__ == "__main__":
    main()
