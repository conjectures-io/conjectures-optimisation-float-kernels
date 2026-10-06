"""When to set weights: once an epoch, a margin before the boundary, inside the limit."""

from __future__ import annotations

import dataclasses as dc


@dc.dataclass(frozen=True, slots=True)
class SetDecision:
    # Whether to set weights now, and when the next attempt is due.
    proceed: bool
    rate_off_blocks: int
    epoch_blocks: int
    next_try_block: int


def blocks_until_next_epoch(current_block: int, tempo: int, last_epoch_block: int) -> int:
    # Blocks to the subnet's next epoch, in 1..tempo, anchored on the chain's own record of the
    # last one (LastMechansimStepBlock), which recurs every `tempo` blocks. The textbook
    # `tempo - (block + netuid + 1) % (tempo + 1)` assumes a 361-block cycle offset by the netuid;
    # SN66 steps every 360, so that formula ran 85-91 blocks late (seen by DEFLATE, 2026-09-28).
    if tempo <= 0:
        return 0
    return tempo - (current_block - last_epoch_block) % tempo


def should_set(
    *,
    current_block: int,
    tempo: int,
    last_epoch_block: int,
    blocks_since_last_update: int,
    weights_rate_limit: int,
    set_margin: int,
) -> SetDecision:
    # Set only when the rate limit is clear and the epoch end is close, so the vector is freshest;
    # `next_try_block` is what the caller logs, so an operator sees when the next attempt is due.
    rate_off_blocks = max(0, weights_rate_limit - blocks_since_last_update)
    epoch_blocks = blocks_until_next_epoch(current_block, tempo, last_epoch_block)
    rate_clear = current_block + rate_off_blocks
    margin_wait = max(0, blocks_until_next_epoch(rate_clear, tempo, last_epoch_block) - set_margin)
    next_try_block = rate_clear + margin_wait
    proceed = rate_off_blocks == 0 and epoch_blocks <= set_margin
    return SetDecision(proceed, rate_off_blocks, epoch_blocks, next_try_block)
