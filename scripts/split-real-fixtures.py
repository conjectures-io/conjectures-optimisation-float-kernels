#!/usr/bin/env python3
"""Build sample-addressable weights and complete held-out input pools for the benchmark.

Weights are copied from the existing locally trained model fixtures. Input records are rebuilt
from the complete held-out evaluation partitions and serialized as fixed-width little-endian
float32 rows. Labels are omitted: the benchmark measures inference latency and checks output
hashes, not task accuracy.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import struct
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE_ROOT = ROOT / "data/real-model-backtest"
DEFAULT_OUTPUT_ROOT = ROOT / "data/benchmark/assets"
DEFAULT_IMAGE_DATA_ROOT = ROOT / "data/old/real-model-backtest/20261030-model-zoo/datasets"
DEFAULT_UCI_HAR_ROOT = ROOT / "data/real-model-backtest/assets/uci-har/UCI HAR Dataset"
DEFAULT_APPLIANCE_CSV = (
    ROOT / "data/real-model-backtest/20261103-appliance-energy-mixer/energydata_complete.csv"
)
IMAGE_FAMILIES = ("mlp", "normalized_mlp", "resnet", "mobilenet", "mixer")
IMAGE_DATASETS = ("digits", "mnist", "fashion")
UCI_SIGNALS = (
    "body_acc_x",
    "body_acc_y",
    "body_acc_z",
    "body_gyro_x",
    "body_gyro_y",
    "body_gyro_z",
    "total_acc_x",
    "total_acc_y",
    "total_acc_z",
)
APPLIANCE_FEATURES = ("Appliances", "lights", "T1", "RH_1", "T2", "RH_2", "T6", "RH_6")
APPLIANCE_TOKENS = 24
APPLIANCE_HORIZON = 6


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


def digest_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def image_idx(path: Path) -> np.ndarray:
    raw = path.read_bytes()
    if len(raw) < 16:
        raise ValueError(f"{path}: truncated IDX image file")
    magic, count, rows, cols = struct.unpack_from(">IIII", raw)
    if magic != 2051 or len(raw) != 16 + count * rows * cols:
        raise ValueError(f"{path}: invalid IDX image file")
    return np.frombuffer(raw, dtype=np.uint8, offset=16).reshape(count, rows, cols)


def load_image_evaluation(image_data_root: Path) -> dict[str, tuple[np.ndarray, str]]:
    from sklearn.datasets import load_digits

    digits = load_digits()
    # Match the existing model-zoo split: the first 1,437 examples train the models; all
    # remaining examples form the held-out evaluation partition (360 samples).
    digit_images = digits.images[1437:].astype(np.float32)[:, None, :, :] / np.float32(16.0)
    result = {"digits": (digit_images, "sklearn load_digits indices 1437:1797")}
    for dataset, folder in (("mnist", "MNIST"), ("fashion", "FashionMNIST")):
        path = image_data_root / folder / "raw/t10k-images-idx3-ubyte"
        images = image_idx(path).astype(np.float32)[:, None, :, :] / np.float32(255.0)
        result[dataset] = (images, f"{folder} official 10,000-image test split")
    return result


def image_runtime_inputs(family: str, images: np.ndarray) -> np.ndarray:
    if family != "mixer":
        return images.reshape(len(images), -1)
    _, _, height, width = images.shape
    patch = height // 4
    if height != width or 4 * patch != height:
        raise ValueError("image dimensions must divide into the Mixer 4x4 patch grid")
    cropped = images[:, 0, : 4 * patch, : 4 * patch]
    return (
        cropped.reshape(len(images), 4, patch, 4, patch)
        .transpose(0, 1, 3, 2, 4)
        .reshape(len(images), 16, patch * patch)
        .reshape(len(images), -1)
    )


def load_uci_evaluation(dataset_root: Path) -> tuple[np.ndarray, str]:
    signal_dir = dataset_root / "test/Inertial Signals"
    signals = [
        np.loadtxt(signal_dir / f"{name}_test.txt", dtype=np.float32) for name in UCI_SIGNALS
    ]
    inputs = np.stack(signals, axis=-1)
    return inputs, "UCI-HAR official subject-disjoint 2,947-window test split"


def uci_runtime_inputs(family: str, inputs: np.ndarray) -> np.ndarray:
    if family in {"resnet", "mobilenet"}:
        return inputs.transpose(0, 2, 1).reshape(len(inputs), -1)
    # MLPs consume flattened windows; the Mixer reshapes the same row-major values into
    # 16 tokens before inference, so its external flattened input is byte-identical.
    return inputs.reshape(len(inputs), -1)


def appliance_evaluation(csv_path: Path) -> tuple[np.ndarray, str]:
    rows: list[list[float]] = []
    with csv_path.open(newline="", encoding="utf-8") as stream:
        reader = csv.DictReader(stream)
        missing = set(APPLIANCE_FEATURES) - set(reader.fieldnames or ())
        if missing:
            raise ValueError(f"{csv_path}: missing columns {sorted(missing)}")
        for row in reader:
            rows.append([float(row[name]) for name in APPLIANCE_FEATURES])
    values = np.asarray(rows, dtype=np.float32)
    if values.ndim != 2 or not np.isfinite(values).all():
        raise ValueError(f"{csv_path}: invalid or non-finite appliance data")
    train_end = int(len(values) * 0.70)
    validation_end = int(len(values) * 0.85)
    means = values[:train_end].mean(axis=0, dtype=np.float64).astype(np.float32)
    scales = values[:train_end].std(axis=0, dtype=np.float64).astype(np.float32)
    scales[scales == 0] = 1.0
    normalized = (values - means) / scales
    # Reuse the training script's chronological test-window endpoint convention.
    final_start_exclusive = max(
        validation_end,
        len(values) - APPLIANCE_TOKENS - APPLIANCE_HORIZON + 1,
    )
    starts = np.arange(validation_end, final_start_exclusive, dtype=np.int64)
    inputs = np.stack([normalized[i : i + APPLIANCE_TOKENS] for i in starts]).astype(np.float32)
    return inputs.reshape(len(inputs), -1), "Appliance Energy chronological test-period windows"


def store_samples(
    output_root: Path, adapter: str, dataset: str, inputs: np.ndarray
) -> tuple[str, str, int, int]:
    rows = np.ascontiguousarray(inputs, dtype="<f4")
    if rows.ndim != 2 or not len(rows) or not rows.shape[1]:
        raise ValueError(f"{adapter}/{dataset}: expected nonempty 2D input rows")
    raw = rows.tobytes(order="C")
    sample_hash = digest_bytes(raw)
    sample_target = output_root / "samples" / adapter / dataset / sample_hash[:12]
    sample_target.mkdir(parents=True, exist_ok=True)
    sample_path = sample_target / "samples.bin"
    if sample_path.exists() and sample_path.read_bytes() != raw:
        raise ValueError(f"sample hash collision or modified sample asset: {sample_path}")
    if not sample_path.exists():
        sample_path.write_bytes(raw)
    return sample_path.relative_to(output_root).as_posix(), sample_hash, len(rows), rows.shape[1]


def convert_fixture(
    source: Path,
    target: Path,
    adapter: str,
    family: str,
    model: str,
    dataset: str,
    evaluation_inputs: np.ndarray,
    evaluation_split: str,
    output_root: Path,
):
    tensors = parse_tensors(source)
    weights = b"".join(struct.pack("<I", len(tensor) // 4) + tensor for tensor in tensors[:-2])
    if adapter == "image-zoo-v1":
        inputs = image_runtime_inputs(family, evaluation_inputs)
    elif adapter == "uci-har-v1":
        inputs = uci_runtime_inputs(family, evaluation_inputs)
    elif adapter == "appliance-mixer-v1":
        inputs = evaluation_inputs
    else:
        raise ValueError(f"unsupported adapter {adapter}")
    sample_path, sample_hash, sample_count, sample_words = store_samples(
        output_root, adapter, dataset, inputs
    )
    target.mkdir(parents=True, exist_ok=True)
    weight_path = target / "weights.bin"
    weight_path.write_bytes(weights)
    manifest = {
        "format": "sample-addressable-f32-v1",
        "source": source.relative_to(ROOT).as_posix(),
        "workload_id": (
            f"image-zoo-{family.replace('_', '-')}-{dataset}"
            if adapter == "image-zoo-v1"
            else f"uci-har-{family.replace('_', '-')}"
            if adapter == "uci-har-v1"
            else "appliance-energy-mixer"
        ),
        "adapter": adapter,
        "family": family,
        "model": model,
        "dataset": dataset,
        "evaluation_split": evaluation_split,
        "weight_tensor_count": len(tensors) - 2,
        "sample_count": sample_count,
        "sample_words": sample_words,
        "weights_path": weight_path.relative_to(output_root).as_posix(),
        "weights_sha256": digest_bytes(weights),
        "samples_path": sample_path,
        "samples_sha256": sample_hash,
        "labels_omitted": True,
    }
    (target / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"{manifest['workload_id']}: {sample_count} held-out samples × {sample_words} words")


def fixtures(source_root: Path, output_root: Path):
    image = source_root / "20261102-image-zoo/models"
    for family in IMAGE_FAMILIES:
        for dataset in IMAGE_DATASETS:
            yield (
                image / f"{family}-{dataset}.bin",
                output_root / "models/image-zoo" / family / dataset,
                "image-zoo-v1",
                family,
                f"compact-{family}",
                dataset,
            )
    har = source_root / "20261102-uci-har"
    for family in IMAGE_FAMILIES:
        yield (
            har / f"{family}.bin",
            output_root / "models/uci-har" / family,
            "uci-har-v1",
            family,
            f"uci-har-{family}",
            "UCI-HAR",
        )
    yield (
        source_root / "20261103-appliance-energy-mixer/appliance-energy-mixer.bin",
        output_root / "models/appliance-energy/mixer",
        "appliance-mixer-v1",
        "mixer",
        "appliance-energy-mixer",
        "Appliance Energy Prediction",
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--source-root", type=Path, default=DEFAULT_SOURCE_ROOT)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--image-data-root", type=Path, default=DEFAULT_IMAGE_DATA_ROOT)
    parser.add_argument("--uci-har-root", type=Path, default=DEFAULT_UCI_HAR_ROOT)
    parser.add_argument("--appliance-csv", type=Path, default=DEFAULT_APPLIANCE_CSV)
    args = parser.parse_args()
    source_root = args.source_root if args.source_root.is_absolute() else ROOT / args.source_root
    output_root = args.output_root if args.output_root.is_absolute() else ROOT / args.output_root
    image_data_root = (
        args.image_data_root if args.image_data_root.is_absolute() else ROOT / args.image_data_root
    )
    uci_har_root = (
        args.uci_har_root if args.uci_har_root.is_absolute() else ROOT / args.uci_har_root
    )
    appliance_csv = (
        args.appliance_csv if args.appliance_csv.is_absolute() else ROOT / args.appliance_csv
    )
    source_root = source_root.resolve()
    output_root = output_root.resolve()
    image_data_root = image_data_root.resolve()
    uci_har_root = uci_har_root.resolve()
    appliance_csv = appliance_csv.resolve()
    try:
        output_root.relative_to(ROOT.resolve())
    except ValueError:
        parser.error("--output-root must be inside this repository")

    fixtures_to_convert = list(fixtures(source_root, output_root))
    missing_models = [str(source) for source, *_ in fixtures_to_convert if not source.is_file()]
    if missing_models:
        parser.error("missing model fixtures:\n" + "\n".join(missing_models))
    if not args.overwrite and (output_root / "models").exists():
        parser.error(f"output exists: {output_root / 'models'} (pass --overwrite to replace it)")
    if not appliance_csv.is_file():
        parser.error(f"missing appliance test data: {appliance_csv}")

    image_evaluation = load_image_evaluation(image_data_root)
    uci_evaluation, uci_split = load_uci_evaluation(uci_har_root)
    appliance_evaluation_inputs, appliance_split = appliance_evaluation(appliance_csv)

    for source, target, adapter, family, model, dataset in fixtures_to_convert:
        if adapter == "image-zoo-v1":
            evaluation_inputs, evaluation_split = image_evaluation[dataset]
        elif adapter == "uci-har-v1":
            evaluation_inputs, evaluation_split = uci_evaluation, uci_split
        else:
            evaluation_inputs, evaluation_split = appliance_evaluation_inputs, appliance_split
        convert_fixture(
            source,
            target,
            adapter,
            family,
            model,
            dataset,
            evaluation_inputs,
            evaluation_split,
            output_root,
        )


if __name__ == "__main__":
    main()
