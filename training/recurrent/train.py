"""Train one recurrent benchmark model deterministically on CPU.

    .venv/bin/python train.py --config uci-har-lstm [--seed 0] [--threads 4]

Writes ``runs/<config>/``: ``checkpoint.pt``, ``weights.bin`` (recurrent-v1 export),
``metrics.json``, ``test_outputs.npy`` (PyTorch fp32 outputs on the test pool) and
``test_labels.npy`` (kept here, never in the asset bundle).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import platform
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import torch
from torch import nn

sys.path.insert(0, str(Path(__file__).resolve().parent))  # also works under `python -I`

from export import export_weights  # noqa: E402
from models import RecurrentModel  # noqa: E402

import data as datasets  # noqa: E402

HERE = Path(__file__).resolve().parent
RUNS = HERE / "runs"

# fixture: (dataset, fixture directory name under models/recurrent/<dataset>/)
CONFIGS: dict[str, dict] = {
    "uci-har-lstm": {
        "dataset": "uci-har",
        "loader": {"steps": 64},
        "layers": [("lstm", 32)],
        "epochs": 60,
        "fixture": "lstm",
    },
    "uci-har-gru": {
        "dataset": "uci-har",
        "loader": {"steps": 64},
        "layers": [("gru", 32)],
        "epochs": 60,
        "fixture": "gru",
    },
    "uci-har-lstm-t128": {  # accuracy reference at full length; not packaged as a fixture
        "dataset": "uci-har",
        "loader": {"steps": 128},
        "layers": [("lstm", 32)],
        "epochs": 60,
        "fixture": None,
    },
    "appliance-gru": {
        "dataset": "appliance-energy",
        "loader": {},
        "layers": [("gru", 32)],
        "epochs": 60,
        "fixture": "gru",
    },
    "mnist-rows-lstm": {
        "dataset": "mnist-rows",
        "loader": {},
        "layers": [("lstm", 32)],
        "epochs": 60,
        "fixture": "lstm",
    },
    "spoken-arabic-digit-lstm": {
        "dataset": "spoken-arabic-digit",
        "loader": {"steps": 48},
        "layers": [("lstm", 32)],
        "epochs": 60,
        "fixture": "lstm",
    },
    "tiny-shakespeare-lstm": {
        "dataset": "tiny-shakespeare",
        "loader": {"steps": 64, "train_stride": 32},
        "layers": [("lstm", 32)],
        "epochs": 40,
        "sequence_loss": True,
        "fixture": "lstm",
    },
    "mitbih-gru": {
        "dataset": "mitbih",
        "loader": {"steps": 94},
        "layers": [("gru", 32)],
        "epochs": 60,  # ~20-35 s/epoch on 4 threads (~15 min idle, longer on a busy machine)
        "max_minutes": 50,
        "fixture": "gru",
    },
    "speech-commands-gru": {
        "dataset": "speech-commands",
        "loader": {},
        "layers": [("gru", 32)],
        "epochs": 40,
        "fixture": "gru",
    },
    "speech-commands-lstm-gru": {
        "dataset": "speech-commands",
        "loader": {},
        "layers": [("lstm", 32), ("gru", 32)],
        "epochs": 40,
        "fixture": "lstm-gru",
    },
}
HYPER = {
    "optimizer": "Adam",
    "lr": 2e-3,
    "lr_min": 1e-5,
    "schedule": "cosine per epoch",
    "weight_decay": 1e-4,
    "batch_size": 64,
    "grad_clip_norm": 1.0,
    "patience": 15,
}


def pool_sha256(x: np.ndarray) -> str:
    rows = np.ascontiguousarray(x.reshape(len(x), -1), dtype="<f4")
    return hashlib.sha256(rows.tobytes()).hexdigest()


def git_commit() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=HERE, capture_output=True, text=True, check=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def predict(model: RecurrentModel, x: np.ndarray, batch: int = 1024) -> np.ndarray:
    model.eval()
    outs = []
    with torch.no_grad():
        for start in range(0, len(x), batch):
            outs.append(model(torch.from_numpy(x[start : start + batch])).numpy())
    return np.concatenate(outs).astype(np.float32)


def evaluate(task: str, outputs: np.ndarray, y: np.ndarray) -> dict:
    if task == "classification":
        logits = torch.from_numpy(outputs)
        loss = nn.functional.cross_entropy(logits, torch.from_numpy(y)).item()
        return {"accuracy": float(np.mean(outputs.argmax(axis=1) == y)), "loss": loss}
    mse = float(np.mean((outputs.astype(np.float64) - y) ** 2))
    return {"rmse": math.sqrt(mse), "loss": mse}


def baselines(dataset: datasets.Dataset) -> dict:
    test = dataset.test
    if dataset.task == "classification":
        majority = int(np.bincount(dataset.train.y, minlength=dataset.outputs).argmax())
        return {
            "majority_class": majority,
            "majority_baseline_accuracy": float(np.mean(test.y == majority)),
        }
    last = np.repeat(test.x[:, -1, :1].astype(np.float64), test.y.shape[1], axis=1)
    return {
        "naive_last_value_rmse": math.sqrt(float(np.mean((last - test.y) ** 2))),
        "zero_forecast_rmse": math.sqrt(float(np.mean(test.y.astype(np.float64) ** 2))),
    }


def train(name: str, seed: int, threads: int, epochs: int | None, out_dir: Path) -> dict:
    config = CONFIGS[name]
    torch.set_num_threads(threads)
    torch.manual_seed(seed)
    np.random.seed(seed)
    torch.use_deterministic_algorithms(True)
    started = time.time()
    dataset = datasets.load(config["dataset"], **config["loader"])
    load_seconds = time.time() - started
    model = RecurrentModel(dataset.features, config["layers"], dataset.outputs)
    max_epochs = epochs or config["epochs"]
    optimizer = torch.optim.Adam(
        model.parameters(), lr=HYPER["lr"], weight_decay=HYPER["weight_decay"]
    )
    schedule = torch.optim.lr_scheduler.CosineAnnealingLR(
        optimizer, T_max=max_epochs, eta_min=HYPER["lr_min"]
    )
    sequence_loss = bool(config.get("sequence_loss"))
    x_train = torch.from_numpy(dataset.train.x)
    y_train = torch.from_numpy(dataset.train.seq_y if sequence_loss else dataset.train.y)
    loss_fn = nn.CrossEntropyLoss() if dataset.task == "classification" else nn.MSELoss()
    generator = torch.Generator().manual_seed(seed)
    best_score, best_state, best_epoch, stale = -math.inf, None, 0, 0
    history = []
    stop_reason = "max_epochs"
    # Safety net only: a time-limited run is not reproducible, and metrics.json says so.
    max_seconds = 60.0 * config["max_minutes"] if config.get("max_minutes") else None
    for epoch in range(1, max_epochs + 1):
        model.train()
        order = torch.randperm(len(x_train), generator=generator)
        total, batches = 0.0, 0
        for start in range(0, len(order), HYPER["batch_size"]):
            idx = order[start : start + HYPER["batch_size"]]
            xb, yb = x_train[idx], y_train[idx]
            optimizer.zero_grad(set_to_none=True)
            if sequence_loss:
                logits = model(xb, all_steps=True)
                loss = loss_fn(logits.reshape(-1, logits.shape[-1]), yb.reshape(-1))
            else:
                loss = loss_fn(model(xb), yb)
            loss.backward()
            nn.utils.clip_grad_norm_(model.parameters(), HYPER["grad_clip_norm"])
            optimizer.step()
            total += loss.item()
            batches += 1
        schedule.step()
        val = evaluate(dataset.task, predict(model, dataset.val.x), dataset.val.y)
        score = val["accuracy"] - 1e-6 * val["loss"] if "accuracy" in val else -val["loss"]
        history.append(
            {
                "epoch": epoch,
                "train_loss": total / batches,
                **{f"val_{k}": v for k, v in val.items()},
            }
        )
        print(
            f"[{name}] epoch {epoch:3d} train_loss {total / batches:.4f} "
            + " ".join(f"val_{k} {v:.4f}" for k, v in val.items())
            + f" ({time.time() - started:.0f}s)",
            flush=True,
        )
        if score > best_score:
            best_score, best_epoch, stale = score, epoch, 0
            best_state = {k: v.detach().clone() for k, v in model.state_dict().items()}
        else:
            stale += 1
            if stale >= HYPER["patience"]:
                stop_reason = "early_stopping"
                break
        if max_seconds is not None and time.time() - started > max_seconds:
            stop_reason = f"time budget ({config['max_minutes']} min)"
            break
    model.load_state_dict(best_state)
    test_outputs = predict(model, dataset.test.x)
    val_metrics = evaluate(dataset.task, predict(model, dataset.val.x), dataset.val.y)
    test_metrics = evaluate(dataset.task, test_outputs, dataset.test.y)
    steps = dataset.steps
    weights = export_weights(model, steps)

    out_dir.mkdir(parents=True, exist_ok=True)
    checkpoint_config = {**model.config(), "steps": steps}
    torch.save(
        {"config": checkpoint_config, "model": model.state_dict()}, out_dir / "checkpoint.pt"
    )
    (out_dir / "weights.bin").write_bytes(weights)
    np.save(out_dir / "test_outputs.npy", test_outputs)
    np.save(out_dir / "test_labels.npy", dataset.test.y)
    metrics = {
        "config": name,
        "dataset": dataset.name,
        "task": dataset.task,
        "fixture": config["fixture"],
        "model": checkpoint_config,
        "loader": config["loader"],
        "sequence_loss": sequence_loss,
        "hyperparameters": {**HYPER, "max_epochs": max_epochs},
        "best_epoch": best_epoch,
        "epochs_run": len(history),
        "stop_reason": stop_reason,
        "train_size": len(dataset.train.x),
        "val_size": len(dataset.val.x),
        "test_size": len(dataset.test.x),
        "test_inputs_sha256": pool_sha256(dataset.test.x),
        "weights_sha256": hashlib.sha256(weights).hexdigest(),
        "torch_fp32": {
            **{f"test_{k}": v for k, v in test_metrics.items()},
            **{f"val_{k}": v for k, v in val_metrics.items()},
        },
        "baselines": baselines(dataset),
        "training": {
            "seed": seed,
            "threads": threads,
            "torch_version": torch.__version__,
            "numpy_version": np.__version__,
            "python_version": sys.version.split()[0],
            "platform": platform.platform(),
            "git_commit": git_commit(),
            "deterministic_algorithms": True,
            "dataset_sha256": datasets.checksums(config["dataset"]),
            "load_seconds": round(load_seconds, 1),
            "wall_seconds": round(time.time() - started, 1),
        },
        "dataset_info": {k: v for k, v in dataset.info.items() if k != "normalisation"},
        "normalisation": dataset.info.get("normalisation"),
        "evaluation_split": dataset.evaluation_split,
        "history": history,
    }
    (out_dir / "metrics.json").write_text(json.dumps(metrics, indent=2) + "\n")
    return metrics


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--config", required=True, choices=sorted(CONFIGS))
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--threads", type=int, help="torch threads (default: the config's, else 4)")
    parser.add_argument("--epochs", type=int, help="override the configured maximum epochs")
    parser.add_argument("--output", type=Path, help="run directory (default runs/<config>)")
    args = parser.parse_args()
    threads = args.threads or CONFIGS[args.config].get("threads") or 4
    metrics = train(args.config, args.seed, threads, args.epochs, args.output or RUNS / args.config)
    summary = {
        k: metrics[k] for k in ("config", "best_epoch", "epochs_run", "torch_fp32", "baselines")
    }
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
