"""The exact model the Lean benchmark runs: a 1-2 layer LSTM/GRU stack, last step, linear head.

Each layer is its own single-layer, single-direction ``nn.LSTM`` or ``nn.GRU`` (batch_first,
bias=True, no projection, zero initial state, no dropout), so LSTM and GRU layers can be mixed.
The head is ``nn.Linear(H_last, outputs)`` applied to the last time step's hidden state, with no
activation. PyTorch's GRU computes ``n = tanh(W_in x + b_in + r * (W_hn h + b_hn))``.
"""

from __future__ import annotations

import torch
from torch import nn

KINDS = {"lstm": 0, "gru": 1}
GATES = {"lstm": 4, "gru": 3}
MAX_STEPS, MAX_FEATURES, MAX_OUTPUTS, MAX_LAYERS, MAX_HIDDEN = 256, 1024, 256, 2, 64


class RecurrentModel(nn.Module):
    def __init__(self, features: int, layers: list[tuple[str, int]], outputs: int):
        super().__init__()
        if not 1 <= len(layers) <= MAX_LAYERS:
            raise ValueError(f"1..{MAX_LAYERS} recurrent layers supported")
        if not 1 <= features <= MAX_FEATURES or not 1 <= outputs <= MAX_OUTPUTS:
            raise ValueError("features/outputs exceed the recurrent-v1 caps")
        self.features = features
        self.layer_specs = [(kind, int(hidden)) for kind, hidden in layers]
        self.outputs = outputs
        modules = []
        width = features
        for kind, hidden in self.layer_specs:
            if kind not in KINDS or not 1 <= hidden <= MAX_HIDDEN:
                raise ValueError(f"unsupported layer {kind} H={hidden}")
            cls = nn.LSTM if kind == "lstm" else nn.GRU
            modules.append(cls(width, hidden, num_layers=1, bias=True, batch_first=True))
            width = hidden
        self.recurrent = nn.ModuleList(modules)
        self.head = nn.Linear(width, outputs)

    def sequence(self, x: torch.Tensor) -> torch.Tensor:
        """Hidden states of the last layer for every step, [N, T, H_last]."""
        for layer in self.recurrent:
            x, _ = layer(x)
        return x

    def forward(self, x: torch.Tensor, all_steps: bool = False) -> torch.Tensor:
        hidden = self.sequence(x)
        if all_steps:
            return self.head(hidden)
        return self.head(hidden[:, -1, :])

    def config(self) -> dict:
        return {"features": self.features, "layers": self.layer_specs, "outputs": self.outputs}
