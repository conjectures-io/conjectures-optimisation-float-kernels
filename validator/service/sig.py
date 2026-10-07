"""The hotkey signature that authorises one submission: the conjectures platform's message.

The same five lines the platform verifies for every competition, so a signature made for one
competition, or for one of this competition's slots, cannot be replayed against another. Each
slot is its own competition slug: `floatlib` is the add board (the one the platform serves),
`floatlib-mul`, `-fma`, `-div` and `-sqrt` the others. The miner keeps its own copy in miner/sig.py;
tests/test_miner_sig.py holds the two to the same bytes.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Final, cast

from bittensor_wallet import Keypair

COMPETITION: Final = "floatlib"
COMPETITION_SUBMIT_PREFIX: Final = "conjectures-competition-submit-v1"
SS58_FORMAT: Final = 42
# sr25519 in bittensor_wallet's numbering, named so a default change can't switch curves.
SR25519: Final = 1


def slug_of(slot: str) -> str:
    # The competition slug a slot is submitted under: the add board is the competition itself.
    return COMPETITION if slot == "add" else f"{COMPETITION}-{slot}"


def slot_of(slug: str) -> str | None:
    # The slot a slug names, or None for a slug that is not this competition's.
    if slug == COMPETITION:
        return "add"
    prefix = f"{COMPETITION}-"
    return slug.removeprefix(prefix) if slug.startswith(prefix) else None


def digest_of(kernel: bytes) -> str:
    # sha256(Kernel.lean), hex: what a submission is, and part of what is signed.
    return hashlib.sha256(kernel).hexdigest()


def submit_message(*, competition: str, digest: str, hotkey: str, timestamp: int) -> str:
    # The exact text a hotkey signs, rebuilt by the receiver from the path slug, the digest of
    # the bytes it read and the two headers; change a character and every submit is refused.
    return "\n".join(
        (
            COMPETITION_SUBMIT_PREFIX,
            f"competition: {competition}",
            f"digest: {digest}",
            f"hotkey: {hotkey}",
            f"timestamp: {timestamp}",
        )
    )


def verify(hotkey: str, message: str, signature_hex: str) -> bool:
    # True iff `signature_hex` is a valid sr25519 signature of the message's UTF-8 by `hotkey`.
    try:
        kp = Keypair(ss58_address=hotkey, ss58_format=SS58_FORMAT, crypto_type=SR25519)
        signature = bytes.fromhex(signature_hex.removeprefix("0x"))
        return bool(kp.verify(message.encode("utf-8"), signature))
    except Exception:  # noqa: BLE001 - any malformed key or signature is simply "not verified"
        return False


def sign(kp: Keypair, message: str) -> str:
    # Hex signature over the UTF-8 bytes of `message`, which is what the receiver verifies.
    return kp.sign(message.encode("utf-8")).hex()


def load_keypair(hotkey_file: str) -> Keypair:
    # A Bittensor hotkey JSON file (its `secretSeed`), or a `//Name` dev URI for tests.
    if hotkey_file.startswith("//"):
        return Keypair.create_from_uri(hotkey_file, crypto_type=SR25519)
    raw = json.loads(Path(hotkey_file).read_text())  # pyright: ignore[reportAny] - validated below
    if not isinstance(raw, dict):
        raise ValueError(f"{hotkey_file} is not a JSON object")
    # A JSON object's keys are always strings; that much the isinstance check above already proved.
    data = cast(dict[str, object], raw)
    seed = data.get("secretSeed") or data.get("privateKey")
    if not isinstance(seed, str) or not seed:
        raise ValueError(f"{hotkey_file} has no secretSeed")
    # The stub says bytes, but the binding takes the hex string a hotkey file stores.
    return Keypair.create_from_seed(seed, crypto_type=SR25519)  # pyright: ignore[reportArgumentType]
