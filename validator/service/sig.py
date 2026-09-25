"""Hotkey sr25519 signatures over the submission digest, shared by the service and the miner."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import cast

from bittensor_wallet import Keypair

SS58_FORMAT = 42
# sr25519 in bittensor_wallet's numbering, named so a default change can't switch curves.
SR25519 = 1


def digest_of(kernel: bytes) -> str:
    # sha256(Kernel.lean), hex: what a submission is, and what the miner signs.
    return hashlib.sha256(kernel).hexdigest()


def submit_message(digest: str, hotkey: str, timestamp: int) -> bytes:
    # What a miner signs: file digest, hotkey and timestamp, so a signature fits one upload once.
    return f"submit:{digest}:{hotkey}:{timestamp}".encode()


def verify(hotkey: str, message: bytes, signature_hex: str) -> bool:
    # True iff `signature_hex` is a valid sr25519 signature of `message` by the ss58 `hotkey`.
    try:
        kp = Keypair(ss58_address=hotkey, ss58_format=SS58_FORMAT, crypto_type=SR25519)
        return bool(kp.verify(message, bytes.fromhex(signature_hex.removeprefix("0x"))))
    except Exception:  # noqa: BLE001 - any malformed key or signature is simply "not verified"
        return False


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


def sign(kp: Keypair, message: bytes) -> str:
    # Hex signature of `message` by `kp`.
    return kp.sign(message).hex()
