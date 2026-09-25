"""Shape and freshness checks on a signed write, run before the costly signature check."""

from __future__ import annotations

import re
import time

# Shape only (base58, 42-prefix length); whether the key is real is the signature's job.
SS58 = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{46,49}$")
# sr25519 signatures are 64 bytes; accept them with or without the 0x prefix.
SIGNATURE = re.compile(r"^(0x)?[0-9a-fA-F]{128}$")


class Invalid(ValueError):
    # A write that is malformed, stale or unsigned. Carries the miner-facing reason.
    pass


def check_hotkey(hotkey: str) -> str:
    # Refuse anything that is not shaped like an ss58 hotkey.
    if not SS58.match(hotkey or ""):
        raise Invalid("hotkey is not an ss58 address")
    return hotkey


def check_signature(signature: str) -> str:
    # Refuse anything that is not a hex-encoded 64-byte sr25519 signature.
    if not SIGNATURE.match(signature or ""):
        raise Invalid("signature is not 64 hex-encoded bytes")
    return signature


def check_fresh(timestamp: int, window_seconds: int, *, now: float | None = None) -> int:
    # Refuse a timestamp too far off either way: past is a replay, future outlives its round.
    current = time.time() if now is None else now
    drift = abs(current - timestamp)
    if drift > window_seconds:
        raise Invalid(
            f"signed timestamp is {drift:.0f}s from the validator's clock "
            f"(at most {window_seconds}s); check the clock and resubmit"
        )
    return timestamp
