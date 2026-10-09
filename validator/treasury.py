"""How this validator's weight on Subnet 66 is divided: the treasury, and the competition.

The vector the weight setter submits is the whole validator's, as in the DEFLATE competition:

    treasury share    ->  the treasury uid
    competition share ->  miners by score (reward.py), multiplied by the share without
                          renormalizing; whatever the competition does not pay a miner -- an
                          unregistered hotkey, or the part of the budget nobody earned -- goes
                          to the treasury, not to a burn uid

On mainnet the treasury uid and the share are code constants: where emission goes on netuid 66 is
a reviewed code change, never an environment edit on a running validator. Off mainnet both are
configurable, because uid 121 means nothing on a testnet.

Failing closed means paying the treasury: if scoring fails, the whole weight goes to the treasury
that epoch, because a skipped epoch pays nobody and cannot be set later. The one skip is a
treasury absent from the metagraph (or a pinned treasury hotkey found elsewhere): then no vector
sums to one without paying a stranger the treasury's share.

Only one process may set weights per validator hotkey: two would overwrite each other's vector
every epoch. Which competition's weight setter submits on SN66 is decided at launch; until then
WEIGHT_DRY_RUN stays on (its default), and this vector is recorded but never submitted.
"""

from __future__ import annotations

import math
from collections.abc import Mapping

from chain.types import MetagraphView, WeightPlan

# Subnet 66 on mainnet, and the conjectures.io treasury's uid on it.
MAINNET_NETUID = 66
TREASURY_UID = 121

# The competition's share of the validator's weight in basis points, DEFLATE's figure; the treasury
# has the rest.
COMPETITION_BPS = 2_000
TREASURY_BPS = 10_000 - COMPETITION_BPS
COMPETITION_SHARE = COMPETITION_BPS / 10_000
assert 0 <= COMPETITION_BPS <= 10_000, "the competition's share must be between 0 and 100%"


def treasury_uid_for(netuid: int, override: int | None, *, burn_uid: int) -> int:
    # The uid the treasury share goes to: fixed on mainnet, where an override must agree or the
    # worker refuses to start, and elsewhere the override or else the burn uid.
    if netuid == MAINNET_NETUID:
        if override is not None and override != TREASURY_UID:
            raise ValueError(
                f"the treasury uid on netuid {MAINNET_NETUID} is {TREASURY_UID}, a code constant; "
                f"WEIGHT_TREASURY_UID={override} is refused"
            )
        return TREASURY_UID
    return burn_uid if override is None else override


def competition_share_for(netuid: int, override: float | None) -> float:
    # The competition's fraction of the validator's weight: fixed on mainnet at COMPETITION_BPS,
    # where an override must agree or the worker refuses to start, and elsewhere the override
    # (0..1).
    if override is not None and not (math.isfinite(override) and 0 <= override <= 1):
        raise ValueError("WEIGHT_COMPETITION_SHARE must be between 0 and 1")
    if netuid == MAINNET_NETUID:
        if override is not None and not math.isclose(override, COMPETITION_SHARE, abs_tol=1e-9):
            raise ValueError(
                f"the competition's share on netuid {MAINNET_NETUID} is {COMPETITION_SHARE}, "
                f"a code constant; WEIGHT_COMPETITION_SHARE={override} is refused"
            )
        return COMPETITION_SHARE
    return COMPETITION_SHARE if override is None else override


def resolve_treasury(
    meta: MetagraphView, *, netuid: int, treasury_uid: int, treasury_hotkey: str | None
) -> tuple[int | None, str | None]:
    # This epoch's treasury uid, or None and why: off mainnet a pinned hotkey follows the treasury
    # to a new uid but never pays a reassigned one, and on mainnet it only checks, skipping the
    # epoch if found elsewhere.
    if treasury_hotkey is None:
        return treasury_uid, None
    uid = meta.uid_by_hotkey.get(treasury_hotkey)
    if uid is None or uid not in meta.uids:
        return None, f"treasury hotkey {treasury_hotkey} absent from the metagraph"
    if netuid == MAINNET_NETUID and uid != TREASURY_UID:
        return None, (
            f"treasury hotkey {treasury_hotkey} is at uid {uid}, not the treasury uid "
            f"{TREASURY_UID} on netuid {MAINNET_NETUID}"
        )
    return uid, None


def _absent(treasury_uid: int, meta: MetagraphView) -> WeightPlan | None:
    # The unsubmittable plan for a treasury uid missing from the metagraph, else None.
    if treasury_uid not in meta.uids:
        return WeightPlan(
            (), (), False, f"treasury uid {treasury_uid} absent from the metagraph", ""
        )
    return None


def split(
    weights: Mapping[str, float] | None,
    meta: MetagraphView,
    *,
    treasury_uid: int,
    competition_share: float = COMPETITION_SHARE,
    reason: str | None = None,
) -> WeightPlan:
    # The validator's vector: each miner gets its competition-local weight times the share and the
    # treasury the rest, or everything when `weights` is None (scoring failed, `reason` in the
    # summary).
    if (absent := _absent(treasury_uid, meta)) is not None:
        return absent
    uids = tuple(meta.uids)
    if weights is None:
        by_uid = dict.fromkeys(uids, 0.0)
        by_uid[treasury_uid] = 1.0
        summary = f"treasury=1.000->uid{treasury_uid} (competition unpaid: {reason or 'no vector'})"
        return WeightPlan(uids, tuple(by_uid[uid] for uid in uids), True, None, summary)
    if not 0 <= competition_share <= 1:
        raise ValueError("competition_share must be between 0 and 1")
    if any(not math.isfinite(w) or w < 0 for w in weights.values()):
        raise ValueError("competition weights must be finite and nonnegative")
    if sum(weights.values()) > 1 + 1e-9:
        raise ValueError("competition weights must sum to at most one")

    by_uid = dict.fromkeys(uids, 0.0)
    unregistered = 0.0
    for hotkey, weight in weights.items():
        uid = meta.uid_by_hotkey.get(hotkey)
        if uid in by_uid and uid != treasury_uid:
            by_uid[uid] += competition_share * weight
        else:
            unregistered += weight
    miner_total = sum(by_uid.values())
    by_uid[treasury_uid] = max(0.0, 1.0 - miner_total)
    miners = ", ".join(
        f"uid{uid}={w:.3f}" for uid, w in sorted(by_uid.items()) if uid != treasury_uid and w > 0
    )
    summary = (
        f"treasury={by_uid[treasury_uid]:.3f}->uid{treasury_uid} miners=[{miners}] "
        f"competition_share={competition_share:.3f}"
    )
    if unregistered > 0:
        summary += f" (unregistered={unregistered:.3f} of the competition's share)"
    return WeightPlan(uids, tuple(by_uid[uid] for uid in uids), True, None, summary)


def burn(
    meta: MetagraphView,
    *,
    treasury_uid: int,
    burn_uid: int,
    competition_share: float = COMPETITION_SHARE,
) -> WeightPlan:
    # Burn mode: the competition's share burns and the treasury is paid as usual, or everything when
    # the burn uid is missing from the metagraph.
    if (absent := _absent(treasury_uid, meta)) is not None:
        return absent
    if burn_uid not in meta.uids:
        return split(None, meta, treasury_uid=treasury_uid, reason=f"burn uid {burn_uid} absent")
    uids = tuple(meta.uids)
    by_uid = dict.fromkeys(uids, 0.0)
    by_uid[burn_uid] += competition_share
    by_uid[treasury_uid] += 1.0 - competition_share
    summary = (
        f"treasury={1.0 - competition_share:.3f}->uid{treasury_uid} "
        f"burn={competition_share:.3f}->uid{burn_uid} (burn mode)"
    )
    return WeightPlan(uids, tuple(by_uid[uid] for uid in uids), True, None, summary)
