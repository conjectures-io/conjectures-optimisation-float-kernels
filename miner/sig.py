"""Canonical signed upload message; the competition slug is coordinated with central API."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Final, cast

from bittensor_wallet import Keypair as Keypair  # re-exported: submit.py types its keypair with it

COMPETITION: Final = "torchlean"
COMPETITION_SUBMIT_PREFIX: Final = "conjectures-competition-submit-v1"
# sr25519 in bittensor_wallet's numbering, named so a default change can't switch curves.
SR25519: Final = 1


def digest_of(kernel: bytes) -> str:
    # sha256(Kernel.lean), hex: what a submission is, and part of what is signed.
    return hashlib.sha256(kernel).hexdigest()


def submit_message(*, competition: str, digest: str, hotkey: str, timestamp: int) -> str:
    # The exact text a hotkey signs to authorise one submission to one competition slug.
    return "\n".join(
        (
            COMPETITION_SUBMIT_PREFIX,
            f"competition: {competition}",
            f"digest: {digest}",
            f"hotkey: {hotkey}",
            f"timestamp: {timestamp}",
        )
    )


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
    data = cast(dict[str, object], raw)
    seed = data.get("secretSeed") or data.get("privateKey")
    if not isinstance(seed, str) or not seed:
        raise ValueError(f"{hotkey_file} has no secretSeed")
    # The stub says bytes, but the binding takes the hex string a hotkey file stores.
    return Keypair.create_from_seed(seed, crypto_type=SR25519)  # pyright: ignore[reportArgumentType]
