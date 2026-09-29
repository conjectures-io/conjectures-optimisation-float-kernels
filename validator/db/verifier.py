"""The current verifier fingerprint, from `identity.py` beside gate.py at the repository root."""

from __future__ import annotations

import importlib.util
from pathlib import Path

_IDENTITY = Path(__file__).resolve().parents[2] / "identity.py"


def _load():
    # Loaded by path, so the validator package need not put the repository root on sys.path.
    spec = importlib.util.spec_from_file_location("identity", _IDENTITY)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {_IDENTITY}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def required_fingerprint() -> str:
    # What scoring accepts: this checkout's verifier, or GATE_REQUIRED_FINGERPRINT.
    return str(_load().required_fingerprint())
