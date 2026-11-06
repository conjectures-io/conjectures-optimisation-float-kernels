"""Export a trained :class:`models.RecurrentModel` to the ``recurrent-v1`` weights format.

The file is a little-endian stream of tensors, each ``u32 count`` followed by ``count`` u32
words. Tensor 0 is a header of raw unsigned integers
``[1, T, F, outputs, L, kind_0, H_0, ..., kind_{L-1}, H_{L-1}]`` (kind 0 = LSTM, 1 = GRU). Then,
per layer k, PyTorch's ``weight_ih_l0``, ``weight_hh_l0``, ``bias_ih_l0``, ``bias_hh_l0`` of that
layer's module as float32 bit patterns in PyTorch's row-major layout and native gate order
(LSTM i, f, g, o; GRU r, z, n), then the head weight (outputs x H) and bias.
"""

from __future__ import annotations

import argparse
import struct
from pathlib import Path

import numpy as np
from models import GATES, KINDS, MAX_STEPS, RecurrentModel

FORMAT_VERSION = 1


def _tensor(words: np.ndarray) -> bytes:
    words = np.ascontiguousarray(words).reshape(-1)
    return struct.pack("<I", len(words)) + words.astype("<u4", copy=False).tobytes()


def _floats(array) -> bytes:
    values = np.ascontiguousarray(np.asarray(array, dtype=np.float32))
    if not np.isfinite(values).all():
        raise ValueError("non-finite weight")
    return _tensor(values.astype("<f4").view("<u4"))


def export_weights(model: RecurrentModel, steps: int) -> bytes:
    if not 1 <= steps <= MAX_STEPS:
        raise ValueError(f"T must be in 1..{MAX_STEPS}")
    header = [FORMAT_VERSION, steps, model.features, model.outputs, len(model.layer_specs)]
    for kind, hidden in model.layer_specs:
        header += [KINDS[kind], hidden]
    chunks = [_tensor(np.asarray(header, dtype=np.uint32))]
    width = model.features
    for (kind, hidden), layer in zip(model.layer_specs, model.recurrent, strict=True):
        gates = GATES[kind]
        params = {name: p.detach().cpu().numpy() for name, p in layer.named_parameters()}
        expected = {
            "weight_ih_l0": (gates * hidden, width),
            "weight_hh_l0": (gates * hidden, hidden),
            "bias_ih_l0": (gates * hidden,),
            "bias_hh_l0": (gates * hidden,),
        }
        if set(params) != set(expected):
            raise ValueError(f"unexpected parameters {sorted(params)}")
        for name, shape in expected.items():
            if params[name].shape != shape:
                raise ValueError(f"{name}: shape {params[name].shape} != {shape}")
            chunks.append(_floats(params[name]))
        width = hidden
    chunks.append(_floats(model.head.weight.detach().cpu().numpy()))
    chunks.append(_floats(model.head.bias.detach().cpu().numpy()))
    return b"".join(chunks)


def tensor_count(layers: int) -> int:
    return 1 + 4 * layers + 2


def main():
    import torch

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("checkpoint", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    state = torch.load(args.checkpoint, map_location="cpu", weights_only=True)
    config = state["config"]
    model = RecurrentModel(
        config["features"], [tuple(x) for x in config["layers"]], config["outputs"]
    )
    model.load_state_dict(state["model"])
    args.output.write_bytes(export_weights(model, config["steps"]))


if __name__ == "__main__":
    main()
