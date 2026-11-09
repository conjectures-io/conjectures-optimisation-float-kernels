#!/usr/bin/env python3
"""Package trained recurrent models as sample-addressable benchmark fixtures (recurrent-v1).

Run with the training environment (it reloads the test splits through training/recurrent/data.py):

    training/recurrent/.venv/bin/python scripts/build-recurrent-fixtures.py [--overwrite]

For each trained run under training/recurrent/runs/<config>/ this writes
models/recurrent/<dataset>/<fixture>/{weights.bin,manifest.json} and the content-addressed
test-input pool samples/recurrent-v1/<dataset>/<sha256[:12]>/samples.bin under the asset root,
then ATTRIBUTION.md at the asset root. Labels are never written to the bundle; they stay next
to the checkpoints (runs/<config>/test_labels.npy). Each fixture is checked before it is
written: the rebuilt pool must hash to the pool the model was evaluated on, the numpy float64
reference forward pass (training/recurrent/reference.py) must agree with PyTorch, accuracy must
beat the trivial baseline and gate saturation must stay below 50%.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
TRAINING = ROOT / "training/recurrent"
sys.path.insert(0, str(TRAINING))

import data as datasets  # noqa: E402
import reference  # noqa: E402
from export import tensor_count  # noqa: E402

DEFAULT_OUTPUT_ROOT = ROOT / "data/benchmark/assets"
DEFAULT_RUNS = TRAINING / "runs"
ADAPTER = "recurrent-v1"
FAMILY = "recurrent"
# (training config, dataset directory, fixture directory); order = default build order
FIXTURES = (
    ("uci-har-lstm", "uci-har", "lstm"),
    ("uci-har-gru", "uci-har", "gru"),
    ("appliance-gru", "appliance-energy", "gru"),
    ("mnist-rows-lstm", "mnist-rows", "lstm"),
    ("spoken-arabic-digit-lstm", "spoken-arabic-digit", "lstm"),
    ("tiny-shakespeare-lstm", "tiny-shakespeare", "lstm"),
    ("mitbih-gru", "mitbih", "gru"),
    ("speech-commands-gru", "speech-commands", "gru"),
    ("speech-commands-lstm-gru", "speech-commands", "lstm-gru"),
)
MIN_ARGMAX_AGREEMENT = 0.995
MAX_GATE_SATURATION = 0.5
MIN_ACCURACY_MARGIN = 0.10  # absolute accuracy points above the majority baseline
OTHER_BUNDLE_DATASETS = (
    (
        "sklearn Digits (UCI Optical Recognition of Handwritten Digits)",
        "CC BY 4.0",
        "Alpaydin, E. & Kaynak, C. (1998). Optical Recognition of Handwritten Digits [Dataset]. "
        "UCI Machine Learning Repository. https://doi.org/10.24432/C50P49",
        "https://archive.ics.uci.edu/dataset/80",
    ),
    (
        "Fashion-MNIST",
        "MIT License",
        "Xiao, H., Rasul, K. & Vollgraf, R. (2017). Fashion-MNIST, arXiv:1708.07747. "
        "Copyright (c) 2017 Zalando SE",
        "https://github.com/zalandoresearch/fashion-mnist",
    ),
)


def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def load_split_real_fixtures():
    spec = importlib.util.spec_from_file_location(
        "split_real_fixtures", ROOT / "scripts/split-real-fixtures.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def existing_pool_rows(dataset: str) -> tuple[np.ndarray, str] | None:
    """The existing (non-recurrent) pool this dataset's test inputs must equal byte for byte."""
    srf = load_split_real_fixtures()
    if dataset == "appliance-energy":
        rows, _ = srf.appliance_evaluation(datasets.appliance_csv())
        return rows, "appliance-mixer-v1 Appliance Energy pool (scripts/split-real-fixtures.py)"
    if dataset == "mnist-rows":
        raw = datasets.fetch("mnist-rows")["t10k-images-idx3-ubyte.gz"]
        decompressed = gzip.decompress(raw.read_bytes())
        images = np.frombuffer(decompressed, dtype=np.uint8, offset=16).reshape(-1, 28, 28)
        images = images.astype(np.float32)[:, None, :, :] / np.float32(255.0)
        return srf.image_runtime_inputs("mlp", images), (
            "image-zoo-v1 MNIST pool (scripts/split-real-fixtures.py, non-Mixer layout)"
        )
    return None


def store_samples(output_root: Path, dataset: str, rows: np.ndarray) -> tuple[str, str]:
    raw = np.ascontiguousarray(rows, dtype="<f4").tobytes(order="C")
    digest = sha256_bytes(raw)
    target = output_root / "samples" / ADAPTER / dataset / digest[:12]
    target.mkdir(parents=True, exist_ok=True)
    path = target / "samples.bin"
    if path.exists() and path.read_bytes() != raw:
        raise ValueError(f"sample hash collision or modified sample asset: {path}")
    if not path.exists():
        path.write_bytes(raw)
    return path.relative_to(output_root).as_posix(), digest


def acceptance(metrics: dict, check: dict) -> list[str]:
    failures = []
    torch_metrics, baselines = metrics["torch_fp32"], metrics["baselines"]
    if metrics["task"] == "classification":
        if (
            torch_metrics["test_accuracy"]
            < baselines["majority_baseline_accuracy"] + MIN_ACCURACY_MARGIN
        ):
            failures.append("accuracy not clearly above the majority baseline")
        if check["reference_argmax_agreement"] < MIN_ARGMAX_AGREEMENT:
            failures.append("reference argmax agreement below 99.5%")
    elif torch_metrics["test_rmse"] >= baselines["naive_last_value_rmse"]:
        failures.append("RMSE not below the naive last-value forecast")
    if check["reference_max_abs_output_diff"] > 1e-3:
        failures.append("reference output differs from PyTorch by more than 1e-3")
    if check["gate_saturation_fraction"] >= MAX_GATE_SATURATION:
        failures.append("gate saturation at or above 50%")
    return failures


def build_fixture(config: str, dataset: str, fixture: str, runs: Path, output_root: Path) -> dict:
    run = runs / config
    metrics = json.loads((run / "metrics.json").read_text())
    weights = (run / "weights.bin").read_bytes()
    if sha256_bytes(weights) != metrics["weights_sha256"]:
        raise ValueError(f"{config}: weights.bin does not match its metrics.json")
    if metrics["dataset"] != dataset or metrics["fixture"] != fixture:
        raise ValueError(f"{config}: run is for {metrics['dataset']}/{metrics['fixture']}")
    loaded = datasets.load(dataset, **metrics["loader"])
    rows = loaded.test.x.reshape(len(loaded.test.x), -1)
    rows = np.ascontiguousarray(rows, dtype="<f4")
    if sha256_bytes(rows.tobytes()) != metrics["test_inputs_sha256"]:
        raise ValueError(f"{config}: rebuilt test pool differs from the pool used in training")
    identity = existing_pool_rows(dataset)
    pool_identity = None
    if identity is not None:
        existing, description = identity
        existing_raw = np.ascontiguousarray(existing, dtype="<f4").tobytes()
        if existing_raw != rows.tobytes():
            raise ValueError(f"{config}: test pool is not byte-identical to the {description}")
        pool_identity = {"identical_to": description, "sha256": sha256_bytes(existing_raw)}

    parsed, outputs, saturation = reference.run_pool(weights, rows.tobytes())
    torch_outputs = np.load(run / "test_outputs.npy")
    check = reference.compare(outputs, torch_outputs, metrics["task"])
    check["gate_saturation_fraction"] = saturation
    failures = acceptance(metrics, check)
    if failures:
        raise ValueError(f"{config}: acceptance failed: {'; '.join(failures)}")

    sample_path, sample_hash = store_samples(output_root, dataset, rows)
    target = output_root / "models" / FAMILY / dataset / fixture
    target.mkdir(parents=True, exist_ok=True)
    weight_path = target / "weights.bin"
    weight_path.write_bytes(weights)
    info = metrics["dataset_info"]
    model_metrics = {f"torch_fp32_{k}": v for k, v in metrics["torch_fp32"].items()}
    model_metrics.update(metrics["baselines"])
    model_metrics.update(check)
    model_metrics["gate_saturation_threshold"] = reference.SATURATION_THRESHOLD
    model_metrics["acceptance"] = {
        "passed": True,
        "min_argmax_agreement": MIN_ARGMAX_AGREEMENT,
        "max_gate_saturation": MAX_GATE_SATURATION,
        "min_accuracy_margin_over_majority": MIN_ACCURACY_MARGIN,
        "spec_eval": "pending (Lean realEval is the authoritative bit-exact check)",
    }
    manifest = {
        "format": "sample-addressable-f32-v1",
        "source": (run / "checkpoint.pt").relative_to(ROOT).as_posix(),
        "workload_id": f"recurrent-{config}",
        "adapter": ADAPTER,
        "family": FAMILY,
        "model": config,
        "dataset": dataset,
        "dataset_name": info.get("dataset_name", dataset),
        "evaluation_split": metrics["evaluation_split"],
        "weight_tensor_count": tensor_count(len(parsed.layers)),
        "sample_count": int(rows.shape[0]),
        "sample_words": int(rows.shape[1]),
        "weights_path": weight_path.relative_to(output_root).as_posix(),
        "weights_sha256": sha256_bytes(weights),
        "samples_path": sample_path,
        "samples_sha256": sample_hash,
        "labels_omitted": True,
        "architecture": {
            "steps": parsed.steps,
            "features": parsed.features,
            "outputs": parsed.outputs,
            "layers": [{"kind": layer.kind, "hidden": layer.hidden} for layer in parsed.layers],
            "readout": "last time step hidden state -> linear head (no activation)",
            "task": metrics["task"],
        },
        "license": info["license"],
        "attribution": info["attribution"],
        "source_url": info["source_url"],
        "preprocessing": info.get("preprocessing"),
        "validation_split": info.get("validation"),
        "metrics": model_metrics,
        "training": {
            "config": config,
            "seed": metrics["training"]["seed"],
            "torch_version": metrics["training"]["torch_version"],
            "numpy_version": metrics["training"]["numpy_version"],
            "git_commit": metrics["training"]["git_commit"],
            "dataset_sha256": metrics["training"]["dataset_sha256"],
            "hyperparameters": metrics["hyperparameters"],
            "sequence_loss": metrics["sequence_loss"],
            "best_epoch": metrics["best_epoch"],
            "epochs_run": metrics["epochs_run"],
            "train_size": metrics["train_size"],
            "val_size": metrics["val_size"],
        },
    }
    for key in ("source_note", "caveat", "classes", "vocabulary", "mfcc_params"):
        if key in info:
            manifest[key] = info[key]
    if metrics.get("normalisation"):
        manifest["normalisation"] = metrics["normalisation"]
    if pool_identity:
        manifest["sample_pool_identity"] = pool_identity
    (target / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def write_attribution(output_root: Path) -> Path:
    entries = {}
    for path in sorted((output_root / "models").glob("**/manifest.json")):
        manifest = json.loads(path.read_text())
        if "license" in manifest:
            entry = entries.setdefault(
                manifest.get("dataset_name", manifest["dataset"]),
                {
                    "license": manifest["license"],
                    "attribution": manifest["attribution"],
                    "source_url": manifest["source_url"],
                    "fixtures": [],
                },
            )
            entry["fixtures"].append(manifest["workload_id"])
    lines = [
        "# Dataset attribution",
        "",
        "The sample pools in this bundle are preprocessed held-out inputs derived from the",
        "datasets below (labels are not included). Each fixture manifest repeats its dataset's",
        "licence, attribution and source URL; preprocessing is described in the manifest and in",
        "`training/recurrent/README.md` of the competition repository.",
        "",
    ]
    for name, entry in sorted(entries.items()):
        lines += [
            f"## {name}",
            "",
            f"- Licence: {entry['license']}",
            f"- Attribution: {entry['attribution']}",
            f"- Source: {entry['source_url']}",
            f"- Fixtures: {', '.join(sorted(entry['fixtures']))}",
            "",
        ]
    lines += [
        "## Other datasets used by non-recurrent fixtures",
        "",
        "UCI-HAR, Appliances Energy Prediction and MNIST are listed above. The image fixtures also",
        "use:",
        "",
    ]
    for name, license_name, attribution, url in OTHER_BUNDLE_DATASETS:
        lines += [f"- **{name}** ({license_name}): {attribution}. {url}"]
    path = output_root / "ATTRIBUTION.md"
    path.write_text("\n".join(lines) + "\n")
    return path


def attach_spec_results(output_root: Path, paths: list[Path]) -> None:
    """Merge ``training/recurrent/spec_eval.py`` JSON lines into the matching manifests.

    Only ``metrics`` changes, so the pinned weight and sample checksums stay valid. A rebuild
    of a fixture drops its spec results (its weights may have changed); re-run spec_eval then.
    """
    results = {}
    for path in paths:
        for line in path.read_text().splitlines():
            if line.strip():
                result = json.loads(line)
                results[result["workload_id"]] = result
    manifests = {}
    for path in sorted((output_root / "models" / FAMILY).glob("**/manifest.json")):
        manifests[json.loads(path.read_text())["workload_id"]] = path
    for workload_id, result in sorted(results.items()):
        if workload_id not in manifests:
            raise ValueError(f"no manifest for spec result {workload_id}")
        path = manifests[workload_id]
        manifest = json.loads(path.read_text())
        metrics = manifest["metrics"]
        complete = result["rows"] == manifest["sample_count"]
        spec = {
            "spec_rows": result["rows"],
            "spec_full_pool": complete,
            "spec_eval_seconds": result["seconds"],
            "spec_outputs_finite": result["finite"],
            "spec_max_abs_diff_vs_torch": result["max_abs_diff_vs_torch"],
        }
        if "spec_accuracy" in result:
            spec["spec_accuracy"] = result["spec_accuracy"]
            spec["torch_accuracy_on_spec_rows"] = result["torch_accuracy"]
            spec["argmax_agreement_spec_vs_torch"] = result["argmax_agreement"]
        else:
            spec["spec_rmse"] = result["spec_rmse"]
            spec["torch_rmse_on_spec_rows"] = result["torch_rmse"]
        metrics.update(spec)
        metrics["acceptance"]["spec_eval"] = (
            ("passed" if result["accepted"] else "FAILED")
            + f" on {'the full pool' if complete else 'the first rows'} ({result['rows']} rows): "
            "spec accuracy within 0.5 pp of PyTorch (regression: RMSE within 1%) and argmax "
            "agreement >= 99.5%, Lean realEval"
        )
        metrics["acceptance"]["spec_passed"] = bool(result["accepted"])
        path.write_text(json.dumps(manifest, indent=2) + "\n")
        print(f"{workload_id}: attached spec results ({result['rows']} rows)")


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--runs", type=Path, default=DEFAULT_RUNS)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument(
        "--only", nargs="+", metavar="CONFIG", help="build only these training configs"
    )
    parser.add_argument(
        "--overwrite", action="store_true", help="replace existing recurrent fixture manifests"
    )
    parser.add_argument(
        "--attach-spec",
        nargs="+",
        type=Path,
        metavar="JSONL",
        help="only merge spec_eval.py output lines into the existing manifests",
    )
    args = parser.parse_args()
    output_root = (
        args.output_root if args.output_root.is_absolute() else ROOT / args.output_root
    ).resolve()
    try:
        output_root.relative_to(ROOT.resolve())
    except ValueError:
        parser.error("--output-root must be inside this repository")
    if args.attach_spec:
        attach_spec_results(output_root, args.attach_spec)
        return
    runs = (args.runs if args.runs.is_absolute() else ROOT / args.runs).resolve()
    known = {config for config, _, _ in FIXTURES}
    if args.only and set(args.only) - known:
        parser.error(f"unknown configs: {sorted(set(args.only) - known)}")
    selected = [
        item
        for item in FIXTURES
        if (item[0] in args.only if args.only else (runs / item[0] / "metrics.json").is_file())
    ]
    if not selected:
        parser.error(f"no trained runs found under {runs}")
    for config, dataset, fixture in selected:
        manifest_path = output_root / "models" / FAMILY / dataset / fixture / "manifest.json"
        if manifest_path.exists() and not args.overwrite:
            parser.error(f"output exists: {manifest_path} (pass --overwrite to replace it)")
    for config, dataset, fixture in selected:
        manifest = build_fixture(config, dataset, fixture, runs, output_root)
        metrics = manifest["metrics"]
        quality = (
            f"acc {metrics['torch_fp32_test_accuracy']:.4f} "
            f"(majority {metrics['majority_baseline_accuracy']:.4f}), "
            f"argmax agreement {metrics['reference_argmax_agreement']:.4f}"
            if "torch_fp32_test_accuracy" in metrics
            else f"RMSE {metrics['torch_fp32_test_rmse']:.4f} "
            f"(last value {metrics['naive_last_value_rmse']:.4f})"
        )
        print(
            f"{manifest['workload_id']}: {manifest['sample_count']} samples x "
            f"{manifest['sample_words']} words; {quality}; max |diff| "
            f"{metrics['reference_max_abs_output_diff']:.2e}; saturation "
            f"{metrics['gate_saturation_fraction']:.4f}"
        )
    print(f"wrote {write_attribution(output_root)}")


if __name__ == "__main__":
    main()
