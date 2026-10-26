#!/usr/bin/env python3
"""Write a local development config with this checkout/hardware identities. No DB writes."""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "validator")]

from benchmark import machine_identity  # noqa: E402

import fingerprint  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--environment", default="local")
    parser.add_argument(
        "--quick", action="store_true", help="development only: 5 rounds, 2 ms cells"
    )
    parser.add_argument(
        "--reference", type=Path, default=ROOT / "kernels/exact/incumbent/Kernel.lean"
    )
    parser.add_argument(
        "--board", choices=("overall", "mlp", "resnet", "mobilenet", "normalized_mlp", "mixer")
    )
    args = parser.parse_args()
    if args.output.exists():
        parser.error("output already exists; edit it or choose a new path")
    source = args.reference.resolve()
    proof, bench = fingerprint.proof(), fingerprint.benchmark()
    lines = [
        "# Development configuration; calibrate production settings before enabling payouts.",
        f"proof_identity = {proof!r}",
        f"benchmark_identity = {bench!r}",
        "seeds = [42]" if args.quick else "seed_count = 1",
        f"seed_mode = {'fixed' if args.quick else 'fresh'!r}",
        "retry_seconds = 300",
        "lease_seconds = 120",
        "alert_seconds = 900",
        "verification_seconds = 1800",
        "",
        "[timing]",
        f"rounds = {5 if args.quick else 11}",
        "inner = 3",
        f"cell_ms = {2 if args.quick else 50}",
        "run_seconds = 300",
        "build_seconds = 960",
        "benchmark_seconds = 900",
        "",
        "[admission]",
        "bar = 0.97",
        "guard = 1.10",
        "draws = 2000",
        "quantile = 0.05",
        "min_rounds = 5",
        "",
        f"[environments.{args.environment}]",
        "weight = 1.0",
        f"identity = {machine_identity()!r}",
        "",
        "[rewards]",
        "rank_shares = [0.8, 0.2]",
        "paused_policy = 'preserve'",
        "",
        "[rewards.board_weights]",
    ]
    names = ["mlp", "resnet", "mobilenet", "normalized_mlp", "mixer"]
    selected = [args.board] if args.board else ["overall", *names]
    lines += [
        f"{name} = {1.0 if args.board else 0.5 if name == 'overall' else 0.1}" for name in selected
    ]
    for board in selected:
        lines += ["", f"[boards.{board}.models]"]
        lines += [
            f"{name} = {0.2 if board == 'overall' else 1.0}"
            for name in (names if board == "overall" else [board])
        ]
        lines += [
            "",
            f"[boards.{board}.reference]",
            f"path = {str(source)!r}",
            f"sha256 = {hashlib.sha256(source.read_bytes()).hexdigest()!r}",
        ]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text("\n".join(lines) + "\n")
    print(args.output)


if __name__ == "__main__":
    main()
