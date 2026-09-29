"""The verifier's identity: a hash of everything that decides a verdict, stamped on each one.

An accepted submission counts only while its verdict carries the current fingerprint. Change the
gate, the contract, a Verify module, the bench or the Lean toolchain, and every older verdict drops
off the leaderboard until `tools/reverify.py` sends it through the new verifier. The incumbents are
not part of it: a promotion is tracked per slot by `incumbent_sha256`, so it leaves the other slots'
boards alone.

    python identity.py        prints the current fingerprint
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

HERE = Path(__file__).resolve().parent
LIB = HERE / "lean"

# Every file that takes part in a verdict, relative to the repository.
PINNED = sorted(
    [
        "gate.py",
        "identity.py",
        "lean/lakefile.lean",
        "lean/lake-manifest.json",
        "lean/lean-toolchain",
    ]
    + [
        str(p.relative_to(HERE))
        for d in ("contract", "verify", "bench")
        for p in (LIB / d).glob("*.lean")
        # The cost breakdown, never run by the gate.
        if p.name != "Micro.lean"
    ]
)
# Settings that change what a ratio means; the host-only ones (BENCH_CPU, caps) stay out.
KNOBS = {"BENCH_PAIRS": "500000", "BENCH_ROUNDS": "25", "BENCH_INNER": "3"}


def fingerprint() -> str:
    # The pinned files' hashes and the ratio-defining knobs, hashed together.
    parts: dict[str, str] = {
        name: hashlib.sha256((HERE / name).read_bytes()).hexdigest() for name in PINNED
    }
    parts |= {f"env:{k}": os.environ.get(k, default) for k, default in KNOBS.items()}
    return hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()


def required_fingerprint() -> str:
    # What scoring accepts: this checkout's, or an operator-published one for a scorer elsewhere.
    value = os.environ.get("GATE_REQUIRED_FINGERPRINT")
    if value is None:
        return fingerprint()
    if len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise ValueError("GATE_REQUIRED_FINGERPRINT must be a sha256 hex digest")
    return value


if __name__ == "__main__":
    print(fingerprint())
