"""The verifier fingerprint: a hash of everything that decides a verdict, stamped on each one.

Fingerprints describe the verifier used for a result. Changes do not invalidate verdicts,
exclude submissions from scoring, or trigger automatic reverification. Operators can use
`validator/tools/reverify.py` to explicitly requeue accepted submissions when necessary.

    python fingerprint.py     prints the current fingerprint
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import benchmark_suite

HERE = Path(__file__).resolve().parent
LIB = HERE / "lean"

# Every file that takes part in a verdict, relative to the repository.
PINNED = sorted(
    [
        "gate.py",
        "fingerprint.py",
        "benchmark_suite.py",
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
# Settings that change what a ratio means; the host-only ones (BENCH_CPU, caps) stay out. The
# suite file GATE_FULL_SUITE names counts by its hash, so a private suite's contents stay private.
KNOBS = {"BENCH_PAIRS": "500000", "BENCH_ROUNDS": "25", "BENCH_INNER": "3"}


def current() -> str:
    # The pinned files' hashes and the ratio-defining knobs, hashed together.
    parts: dict[str, str] = {
        name: hashlib.sha256((HERE / name).read_bytes()).hexdigest() for name in PINNED
    }
    parts |= {f"env:{k}": os.environ.get(k, default) for k, default in KNOBS.items()}
    suite = benchmark_suite.file_sha256()
    if suite is not None:
        parts["suite"] = suite
    return hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()


def required() -> str:
    # Reference for display and manual reverification, never a scoring requirement.
    value = os.environ.get("GATE_REQUIRED_FINGERPRINT")
    if value is None:
        return current()
    if len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise ValueError("GATE_REQUIRED_FINGERPRINT must be a sha256 hex digest")
    return value


if __name__ == "__main__":
    print(current())
