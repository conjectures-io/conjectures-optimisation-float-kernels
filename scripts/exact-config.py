#!/usr/bin/env python3
"""Write a local development config with this checkout/hardware identities. No DB writes."""

from __future__ import annotations

import argparse
import hashlib
import platform
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "validator")]

from benchmark import machine_identity  # noqa: E402

import fingerprint  # noqa: E402


def fixture_sample_count(path: Path) -> int:
    raw = path.read_bytes()
    counts = []
    offset = 0
    while offset < len(raw):
        if offset + 4 > len(raw):
            raise ValueError(f"truncated fixture tensor header: {path}")
        count = struct.unpack_from("<I", raw, offset)[0]
        offset += 4
        end = offset + count * 4
        if end > len(raw):
            raise ValueError(f"truncated fixture tensor data: {path}")
        counts.append(count)
        offset = end
    if len(counts) < 3 or counts[-1] < 3:
        raise ValueError(f"fixture needs at least three labeled samples: {path}")
    return counts[-1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--environment", default="local")
    parser.add_argument(
        "--quick", action="store_true", help="development only: 3 paired rounds, 1 process each"
    )
    parser.add_argument(
        "--reference", type=Path, default=ROOT / "kernels/exact/incumbent/Kernel.lean"
    )
    parser.add_argument(
        "--dev-intake",
        action="store_true",
        help="development deployment only: also evaluate uploads the API tagged dev",
    )
    parser.add_argument(
        "--board",
        choices=("overall", "mlp", "resnet", "mobilenet", "normalized_mlp", "mixer", "transformer"),
    )
    parser.add_argument(
        "--real-image-zoo",
        action="store_true",
        help="include local trained image-zoo fixtures as diagnostic real workloads",
    )
    parser.add_argument(
        "--real-uci-har",
        action="store_true",
        help="include local UCI-HAR sensor fixtures as diagnostic real workloads",
    )
    parser.add_argument(
        "--real-appliance-mixer",
        action="store_true",
        help="include the local Appliance Energy Mixer fixture as a diagnostic workload",
    )
    args = parser.parse_args()
    if args.output.exists():
        parser.error("output already exists; edit it or choose a new path")
    source = args.reference.resolve()
    proof, bench = fingerprint.proof(), fingerprint.benchmark()
    rounds, inner = (3, 1) if args.quick else (11, 3)

    def samples_per_run(sample_count: int, workload_id: str) -> int:
        capacity = sample_count // (rounds * inner)
        if capacity < 1:
            parser.error(
                f"{workload_id} has {sample_count} samples but the selected timing needs "
                f"{rounds * inner} distinct samples; use --quick or supply a larger fixture"
            )
        return min(3, capacity)

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
        f"dev_intake = {'true' if args.dev_intake else 'false'}",
        "",
        "[benchmark]",
        'suites = ["synthetic", "real"]'
        if args.real_image_zoo or args.real_uci_har or args.real_appliance_mixer
        else 'suites = ["synthetic"]',
        "",
        "[timing]",
        f"rounds = {rounds}",
        f"inner = {inner}",
        "run_seconds = 300",
        "# Build and measurement limits default by architecture and model count.",
        "",
        "[admission]",
        "bar = 0.97",
        "guard = 1.10",
        "draws = 2000",
        "quantile = 0.05",
        f"min_rounds = {min(5, rounds)}",
        "",
        f"[environments.{args.environment}]",
        "weight = 1.0",
        f"identity = {machine_identity()!r}",
        f"architecture = {platform.machine()!r}",
        "",
        "[rewards]",
        "rank_shares = [1.0, 0.0]",
        "paused_policy = 'preserve'",
        "",
        "[rewards.board_weights]",
    ]
    names = ["mlp", "resnet", "mobilenet", "normalized_mlp", "mixer", "transformer"]
    selected = [args.board] if args.board else ["overall", *names]
    lines += [
        f"{name} = {1.0 if args.board else 0.5 if name == 'overall' else 0.5 / len(names)}"
        for name in selected
    ]
    for board in selected:
        lines += ["", f"[boards.{board}.models]"]
        lines += [
            f"{name} = {1 / len(names) if board == 'overall' else 1.0}"
            for name in (names if board == "overall" else [board])
        ]
        lines += [
            "",
            f"[boards.{board}.reference]",
            f"path = {str(source)!r}",
            f"sha256 = {hashlib.sha256(source.read_bytes()).hexdigest()!r}",
        ]
    if args.real_image_zoo:
        model_dir = ROOT / "data/real-model-backtest/20261102-image-zoo/models"
        families = ("mlp", "normalized_mlp", "resnet", "mobilenet", "mixer")
        datasets = ("digits", "mnist", "fashion")
        fixtures = [
            (family, dataset, model_dir / f"{family}-{dataset}.bin")
            for family in families
            for dataset in datasets
        ]
        missing = [str(path) for _, _, path in fixtures if not path.is_file()]
        if missing:
            parser.error("--real-image-zoo requires local fixtures; missing: " + ", ".join(missing))
        for family, dataset, path in fixtures:
            relative = path.relative_to(ROOT).as_posix()
            asset_hash = hashlib.sha256(path.read_bytes()).hexdigest()
            sample_count = fixture_sample_count(path)
            lines += [
                "",
                "[[benchmark.real_workloads]]",
                f"id = {'image-zoo-' + family.replace('_', '-') + '-' + dataset!r}",
                f"family = {family!r}",
                f"model = {'compact-' + family!r}",
                f"dataset = {dataset!r}",
                'adapter = "image-zoo-v1"',
                f"asset_path = {relative!r}",
                f"asset_sha256 = {asset_hash!r}",
                f"asset_revision = {asset_hash!r}",
                f"sample_count = {sample_count}",
                f"samples_per_run = {samples_per_run(sample_count, family + '-' + dataset)}",
            ]
    if args.real_uci_har:
        model_dir = ROOT / "data/real-model-backtest/20261102-uci-har"
        for family in ("mlp", "normalized_mlp", "resnet", "mobilenet", "mixer"):
            path = model_dir / f"{family}.bin"
            if not path.is_file():
                parser.error(f"--real-uci-har requires local fixture {path}")
            relative = path.relative_to(ROOT).as_posix()
            asset_hash = hashlib.sha256(path.read_bytes()).hexdigest()
            sample_count = fixture_sample_count(path)
            lines += [
                "",
                "[[benchmark.real_workloads]]",
                f"id = {'uci-har-' + family.replace('_', '-')!r}",
                f"family = {family!r}",
                f"model = {'uci-har-' + family!r}",
                'dataset = "UCI-HAR"',
                'adapter = "uci-har-v1"',
                f"asset_path = {relative!r}",
                f"asset_sha256 = {asset_hash!r}",
                f"asset_revision = {asset_hash!r}",
                f"sample_count = {sample_count}",
                f"samples_per_run = {samples_per_run(sample_count, family + '-uci-har')}",
            ]
    if args.real_appliance_mixer:
        path = (
            ROOT
            / "data/real-model-backtest/20261103-appliance-energy-mixer/appliance-energy-mixer.bin"
        )
        if not path.is_file():
            parser.error(f"--real-appliance-mixer requires local fixture {path}")
        relative = path.relative_to(ROOT).as_posix()
        asset_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        sample_count = fixture_sample_count(path) // 6
        lines += [
            "",
            "[[benchmark.real_workloads]]",
            'id = "appliance-energy-mixer"',
            'family = "mixer"',
            'model = "appliance-energy-mixer"',
            'dataset = "Appliance Energy Prediction"',
            'adapter = "appliance-mixer-v1"',
            f"asset_path = {relative!r}",
            f"asset_sha256 = {asset_hash!r}",
            f"asset_revision = {asset_hash!r}",
            f"sample_count = {sample_count}",
            f"samples_per_run = {samples_per_run(sample_count, 'appliance-energy-mixer')}",
        ]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text("\n".join(lines) + "\n")
    print(args.output)


if __name__ == "__main__":
    main()
