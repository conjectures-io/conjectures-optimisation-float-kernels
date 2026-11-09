"""Score recurrent fixtures with the bit-exact Lean spec (`realEval`), not with PyTorch.

For each `recurrent-v1` manifest under the assets root, run `lean/.lake/build/bin/realEval` on the
whole sample pool (or a prefix with `--limit`), parse the binary32 output words, and compare:

* spec accuracy (classification) or RMSE (regression) against `runs/<model>/test_labels.npy`;
* argmax agreement and the largest absolute difference against PyTorch's
  `runs/<model>/test_outputs.npy`.

`realEval` runs the entry in `lean/work/Submission.lean`, which is proved equal to the spec. Build
it first: `(cd lean && lake build realEval)`. Prints one JSON object per fixture and exits 1 if a
fixture misses the acceptance gate (spec accuracy within 0.5 pp of PyTorch, regression RMSE within
1%, argmax agreement at least 99.5%).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
REAL_EVAL = ROOT / "lean/.lake/build/bin/realEval"


def run_spec(manifest: dict, assets: Path, limit: int | None, jobs: int) -> np.ndarray:
    """The spec's outputs for the first `limit` rows, as float32, one row per sample."""
    count = manifest["sample_count"] if limit is None else min(limit, manifest["sample_count"])
    chunk = -(-count // jobs)
    procs = []
    for first in range(0, count, chunk):
        command = [
            str(REAL_EVAL),
            manifest["adapter"],
            manifest["family"],
            str(assets / manifest["weights_path"]),
            str(assets / manifest["samples_path"]),
            str(manifest["sample_words"]),
            str(first),
            str(min(chunk, count - first)),
        ]
        procs.append(subprocess.Popen(command, stdout=subprocess.PIPE, text=True))
    rows: dict[int, list[int]] = {}
    for proc in procs:
        out, _ = proc.communicate()
        if proc.returncode:
            raise RuntimeError(f"realEval failed for {manifest['workload_id']}")
        for line in out.splitlines():
            index, *words = (int(t) for t in line.split())
            rows[index] = words
    if sorted(rows) != list(range(count)):
        raise RuntimeError(f"realEval returned an incomplete pool for {manifest['workload_id']}")
    words = np.array([rows[i] for i in range(count)], dtype=np.uint32)
    return words.view(np.float32)


def evaluate(manifest_path: Path, assets: Path, limit: int | None, jobs: int) -> dict:
    manifest = json.loads(manifest_path.read_text())
    run = HERE / "runs" / manifest["model"]
    labels = np.load(run / "test_labels.npy")
    torch_out = np.load(run / "test_outputs.npy").astype(np.float32)
    start = time.monotonic()
    spec = run_spec(manifest, assets, limit, jobs)
    elapsed = time.monotonic() - start
    n = len(spec)
    labels, torch_out = labels[:n], torch_out[:n]
    result = {
        "workload_id": manifest["workload_id"],
        "rows": n,
        "seconds": round(elapsed, 2),
        "max_abs_diff_vs_torch": float(np.max(np.abs(spec.astype(np.float64) - torch_out))),
        "finite": bool(np.isfinite(spec).all()),
    }
    if labels.ndim == 1 and np.issubdtype(labels.dtype, np.integer):
        spec_acc = float(np.mean(spec.argmax(axis=1) == labels))
        torch_acc = float(np.mean(torch_out.argmax(axis=1) == labels))
        agree = float(np.mean(spec.argmax(axis=1) == torch_out.argmax(axis=1)))
        result |= {
            "spec_accuracy": spec_acc,
            "torch_accuracy": torch_acc,
            "argmax_agreement": agree,
        }
        result["accepted"] = (
            result["finite"] and abs(spec_acc - torch_acc) <= 0.005 and agree >= 0.995
        )
    else:
        spec_rmse = float(np.sqrt(np.mean((spec.astype(np.float64) - labels) ** 2)))
        torch_rmse = float(np.sqrt(np.mean((torch_out.astype(np.float64) - labels) ** 2)))
        result |= {"spec_rmse": spec_rmse, "torch_rmse": torch_rmse}
        result["accepted"] = result["finite"] and abs(spec_rmse - torch_rmse) <= 0.01 * torch_rmse
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assets", type=Path, default=ROOT / "data/benchmark/assets")
    parser.add_argument("--limit", type=int, help="evaluate only the first N rows of each pool")
    parser.add_argument("--jobs", type=int, default=4, help="parallel realEval processes")
    parser.add_argument("--only", help="substring of the workload ids to evaluate")
    args = parser.parse_args()
    if not REAL_EVAL.is_file():
        parser.error(f"build {REAL_EVAL} first: (cd lean && lake build realEval)")
    ok = True
    for path in sorted((args.assets / "models/recurrent").glob("**/manifest.json")):
        manifest = json.loads(path.read_text())
        if args.only and args.only not in manifest["workload_id"]:
            continue
        result = evaluate(path, args.assets, args.limit, args.jobs)
        ok &= result["accepted"]
        print(json.dumps(result), flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
