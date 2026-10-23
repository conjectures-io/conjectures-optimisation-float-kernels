"""Independent proof and benchmark identities; scoring/rewards never invalidate proofs."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
COMMON = [
    "fingerprint.py",
    "validator/sandbox.py",
    "validator/macos_worker.py",
    "lean/lakefile.lean",
    "lean/lake-manifest.json",
    "lean/lean-toolchain",
]
CONTRACT = ["lean/contract/ContractExact.lean", "lean/contract/ContractExactChecked.lean"] + sorted(
    str(p.relative_to(HERE)) for p in (HERE / "lean/contract/ExactToolkit").glob("*.lean")
)
PROOF_FILES = COMMON + CONTRACT + ["validator/verification.py", "lean/verify/VerifyExact.lean"]
BENCHMARK_FILES = (
    COMMON
    + CONTRACT
    + [
        "validator/benchmark.py",
        "validator/timing.py",
        "lean/bench/ExactSuite.lean",
        "lean/bench/MainExact.lean",
    ]
)
PINNED = sorted(set(PROOF_FILES + BENCHMARK_FILES))


def identity(files):
    parts = {name: hashlib.sha256((HERE / name).read_bytes()).hexdigest() for name in files}
    return hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()


def proof():
    return identity(PROOF_FILES)


def benchmark():
    return identity(BENCHMARK_FILES)


def current():
    return proof()


if __name__ == "__main__":
    print(json.dumps({"proof_identity": proof(), "benchmark_identity": benchmark()}))
