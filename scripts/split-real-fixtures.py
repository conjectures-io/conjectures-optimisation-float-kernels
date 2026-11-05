#!/usr/bin/env python3
"""Split local monolithic real-model fixtures into weights and addressable samples.

The source fixtures stay untouched. Output directories contain weights.bin (the original
length-prefixed tensor encoding), samples.bin (fixed-width little-endian f32 bit patterns),
and manifest.json. Labels are deliberately omitted because the inference benchmark does not
score model accuracy.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import struct
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = ROOT / "data/real-model-backtest"
OUTPUT_ROOT = SOURCE_ROOT / "20261105-sample-addressable"


def parse_tensors(path: Path) -> list[bytes]:
    raw = path.read_bytes()
    tensors = []
    offset = 0
    while offset < len(raw):
        if offset + 4 > len(raw):
            raise ValueError(f"{path}: truncated tensor header")
        count = struct.unpack_from("<I", raw, offset)[0]
        offset += 4
        end = offset + count * 4
        if end > len(raw):
            raise ValueError(f"{path}: truncated tensor payload")
        tensors.append(raw[offset:end])
        offset = end
    if len(tensors) < 3:
        raise ValueError(f"{path}: expected weights, input tensor and labels")
    return tensors


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def convert(source: Path, target: Path, adapter: str, family: str, model: str, dataset: str):
    tensors = parse_tensors(source)
    input_bytes, labels = tensors[-2:]
    if adapter == "appliance-mixer-v1":
        if len(labels) % (6 * 4):
            raise ValueError(f"{source}: appliance labels are not six values per sample")
        sample_count = len(labels) // (6 * 4)
    else:
        if len(labels) % 4:
            raise ValueError(f"{source}: labels are not binary32 words")
        sample_count = len(labels) // 4
    if not sample_count or len(input_bytes) % (sample_count * 4):
        raise ValueError(f"{source}: input count does not divide into samples")
    sample_words = len(input_bytes) // (sample_count * 4)
    weights = b"".join(struct.pack("<I", len(tensor) // 4) + tensor for tensor in tensors[:-2])
    workload_id = (
        f"image-zoo-{family.replace('_', '-')}-{dataset}"
        if adapter == "image-zoo-v1"
        else f"uci-har-{family.replace('_', '-')}"
        if adapter == "uci-har-v1"
        else "appliance-energy-mixer"
    )
    target.mkdir(parents=True, exist_ok=True)
    weight_path = target / "weights.bin"
    sample_hash = hashlib.sha256(input_bytes).hexdigest()
    sample_target = OUTPUT_ROOT / "samples" / adapter / dataset / sample_hash[:12]
    sample_target.mkdir(parents=True, exist_ok=True)
    sample_path = sample_target / "samples.bin"
    if sample_path.exists() and sample_path.read_bytes() != input_bytes:
        raise ValueError(f"sample hash collision or modified sample asset: {sample_path}")
    weight_path.write_bytes(weights)
    if not sample_path.exists():
        sample_path.write_bytes(input_bytes)
    manifest = {
        "format": "sample-addressable-f32-v1",
        "source": source.relative_to(ROOT).as_posix(),
        "workload_id": workload_id,
        "adapter": adapter,
        "family": family,
        "model": model,
        "dataset": dataset,
        "weight_tensor_count": len(tensors) - 2,
        "sample_count": sample_count,
        "sample_words": sample_words,
        "weights_path": weight_path.relative_to(ROOT).as_posix(),
        "weights_sha256": digest(weight_path),
        "samples_path": sample_path.relative_to(ROOT).as_posix(),
        "samples_sha256": digest(sample_path),
        "labels_omitted": True,
    }
    (target / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"{target.relative_to(ROOT)}: {sample_count} samples × {sample_words} words")


def fixtures():
    image = SOURCE_ROOT / "20261102-image-zoo/models"
    for family in ("mlp", "normalized_mlp", "resnet", "mobilenet", "mixer"):
        for dataset in ("digits", "mnist", "fashion"):
            model = f"compact-{family}"
            yield (
                image / f"{family}-{dataset}.bin",
                OUTPUT_ROOT / "models/image-zoo" / f"{family}-{dataset}",
                "image-zoo-v1", family, model, dataset,
            )
    har = SOURCE_ROOT / "20261102-uci-har"
    for family in ("mlp", "normalized_mlp", "resnet", "mobilenet", "mixer"):
        yield (
            har / f"{family}.bin", OUTPUT_ROOT / "models/uci-har" / family,
            "uci-har-v1", family, f"uci-har-{family}", "UCI-HAR",
        )
    yield (
        SOURCE_ROOT / "20261103-appliance-energy-mixer/appliance-energy-mixer.bin",
        OUTPUT_ROOT / "models/appliance-energy" / "mixer",
        "appliance-mixer-v1", "mixer", "appliance-energy-mixer", "Appliance Energy Prediction",
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()
    fixtures_to_convert = list(fixtures())
    missing = [str(source) for source, *_ in fixtures_to_convert if not source.is_file()]
    if missing:
        parser.error("missing fixtures:\n" + "\n".join(missing))
    if OUTPUT_ROOT.exists() and not args.overwrite:
        parser.error(f"output exists: {OUTPUT_ROOT} (pass --overwrite to replace it)")
    for source, target, adapter, family, model, dataset in fixtures_to_convert:
        convert(source, target, adapter, family, model, dataset)


if __name__ == "__main__":
    main()
