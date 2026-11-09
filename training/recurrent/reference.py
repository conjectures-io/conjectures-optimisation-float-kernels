"""Numpy float64 reference forward pass of a ``recurrent-v1`` fixture (sanity check only).

Parses ``weights.bin`` (header + tensors), runs the exact model semantics on a ``samples.bin``
pool (N rows of T*F little-endian float32), and reports gate saturation: the share of gate
pre-activations with ``|a| > 8`` over every gate unit, step and sample. For a GRU the candidate
pre-activation is ``W_in x + b_in + r * (W_hn h + b_hn)``.

The authoritative bit-exact check is the Lean ``realEval`` executable; this module checks that
the export agrees with PyTorch up to float rounding.
"""

from __future__ import annotations

import argparse
import json
import struct
from dataclasses import dataclass
from pathlib import Path

import numpy as np

SATURATION_THRESHOLD = 8.0
KIND_NAMES = {0: "lstm", 1: "gru"}
CAPS = {"T": 256, "F": 1024, "outputs": 256, "L": 2, "H": 64}


@dataclass
class Layer:
    kind: str
    hidden: int
    w_ih: np.ndarray
    w_hh: np.ndarray
    b_ih: np.ndarray
    b_hh: np.ndarray


@dataclass
class Fixture:
    steps: int
    features: int
    outputs: int
    layers: list[Layer]
    head_w: np.ndarray
    head_b: np.ndarray


def read_tensors(raw: bytes) -> list[np.ndarray]:
    tensors, offset = [], 0
    while offset < len(raw):
        if offset + 4 > len(raw):
            raise ValueError("truncated tensor header")
        (count,) = struct.unpack_from("<I", raw, offset)
        offset += 4
        if offset + 4 * count > len(raw):
            raise ValueError("truncated tensor payload")
        tensors.append(np.frombuffer(raw, dtype="<u4", count=count, offset=offset).copy())
        offset += 4 * count
    return tensors


def parse_fixture(raw: bytes) -> Fixture:
    tensors = read_tensors(raw)
    header = [int(v) for v in tensors[0]]
    if len(header) < 5 or header[0] != 1:
        raise ValueError(f"bad recurrent-v1 header {header}")
    _, steps, features, outputs, count = header[:5]
    if len(header) != 5 + 2 * count or not 1 <= count <= CAPS["L"]:
        raise ValueError(f"bad layer count in header {header}")
    if not (
        1 <= steps <= CAPS["T"] and 1 <= features <= CAPS["F"] and 1 <= outputs <= CAPS["outputs"]
    ):
        raise ValueError(f"header exceeds caps {header}")
    if len(tensors) != 1 + 4 * count + 2:
        raise ValueError(f"expected {1 + 4 * count + 2} tensors, got {len(tensors)}")

    def floats(index: int, shape: tuple[int, ...]) -> np.ndarray:
        words = tensors[index]
        if words.size != int(np.prod(shape)):
            raise ValueError(f"tensor {index}: {words.size} words, expected shape {shape}")
        values = words.view("<f4").astype(np.float64).reshape(shape)
        if not np.isfinite(values).all():
            raise ValueError(f"tensor {index}: non-finite weight")
        return values

    layers, width, index = [], features, 1
    for k in range(count):
        kind_id, hidden = header[5 + 2 * k], header[6 + 2 * k]
        if kind_id not in KIND_NAMES or not 1 <= hidden <= CAPS["H"]:
            raise ValueError(f"bad layer {k} in header {header}")
        kind = KIND_NAMES[kind_id]
        rows = (4 if kind == "lstm" else 3) * hidden
        layers.append(
            Layer(
                kind,
                hidden,
                floats(index, (rows, width)),
                floats(index + 1, (rows, hidden)),
                floats(index + 2, (rows,)),
                floats(index + 3, (rows,)),
            )
        )
        index += 4
        width = hidden
    head_w = floats(index, (outputs, width))
    head_b = floats(index + 1, (outputs,))
    return Fixture(steps, features, outputs, layers, head_w, head_b)


def _sigmoid(a: np.ndarray) -> np.ndarray:
    return 0.5 * (1.0 + np.tanh(0.5 * a))


def forward(fixture: Fixture, x: np.ndarray) -> tuple[np.ndarray, int, int]:
    """Outputs [N, outputs] in float64, plus (saturated, total) gate pre-activation counts."""
    x = np.asarray(x, dtype=np.float64).reshape(-1, fixture.steps, fixture.features)
    n = len(x)
    saturated = total = 0
    sequence = x
    for layer in fixture.layers:
        h_size = layer.hidden
        h = np.zeros((n, h_size))
        c = np.zeros((n, h_size))
        projected = sequence @ layer.w_ih.T + layer.b_ih  # [N, T, G*H]
        out = np.empty((n, fixture.steps, h_size))
        for t in range(fixture.steps):
            recurrent = h @ layer.w_hh.T + layer.b_hh
            if layer.kind == "lstm":
                a = projected[:, t] + recurrent
                i = _sigmoid(a[:, :h_size])
                f = _sigmoid(a[:, h_size : 2 * h_size])
                g = np.tanh(a[:, 2 * h_size : 3 * h_size])
                o = _sigmoid(a[:, 3 * h_size :])
                c = f * c + i * g
                h = o * np.tanh(c)
                pre = a
            else:
                xi, hh = projected[:, t], recurrent
                a_r = xi[:, :h_size] + hh[:, :h_size]
                a_z = xi[:, h_size : 2 * h_size] + hh[:, h_size : 2 * h_size]
                r = _sigmoid(a_r)
                z = _sigmoid(a_z)
                a_n = xi[:, 2 * h_size :] + r * hh[:, 2 * h_size :]
                cand = np.tanh(a_n)
                h = (1.0 - z) * cand + z * h
                pre = np.concatenate([a_r, a_z, a_n], axis=1)
            saturated += int(np.count_nonzero(np.abs(pre) > SATURATION_THRESHOLD))
            total += pre.size
            out[:, t] = h
        sequence = out
    outputs = sequence[:, -1] @ fixture.head_w.T + fixture.head_b
    return outputs, saturated, total


def run_pool(weights: bytes, samples: bytes, chunk: int = 2048):
    fixture = parse_fixture(weights)
    words = fixture.steps * fixture.features
    pool = np.frombuffer(samples, dtype="<f4")
    if pool.size % words:
        raise ValueError(f"sample pool size is not a multiple of T*F = {words}")
    pool = pool.reshape(-1, words)
    outputs, saturated, total = [], 0, 0
    for start in range(0, len(pool), chunk):
        out, sat, tot = forward(fixture, pool[start : start + chunk])
        outputs.append(out)
        saturated += sat
        total += tot
    return fixture, np.concatenate(outputs), saturated / total


def compare(reference: np.ndarray, torch_outputs: np.ndarray, task: str) -> dict:
    diff = np.abs(reference - torch_outputs.astype(np.float64))
    result = {
        "reference_max_abs_output_diff": float(diff.max()),
        "reference_mean_abs_output_diff": float(diff.mean()),
    }
    if task == "classification":
        result["reference_argmax_agreement"] = float(
            np.mean(reference.argmax(axis=1) == torch_outputs.argmax(axis=1))
        )
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("weights", type=Path)
    parser.add_argument("samples", type=Path)
    parser.add_argument("--torch-outputs", type=Path, help=".npy of PyTorch fp32 outputs")
    parser.add_argument("--task", default="classification")
    args = parser.parse_args()
    fixture, outputs, saturation = run_pool(args.weights.read_bytes(), args.samples.read_bytes())
    report = {
        "steps": fixture.steps,
        "features": fixture.features,
        "outputs": fixture.outputs,
        "layers": [(layer.kind, layer.hidden) for layer in fixture.layers],
        "samples": len(outputs),
        "gate_saturation_fraction": saturation,
    }
    if args.torch_outputs:
        report.update(compare(outputs, np.load(args.torch_outputs), args.task))
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
