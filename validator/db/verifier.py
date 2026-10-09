"""The current verifier fingerprint, from `fingerprint.py` beside gate.py at the repository root."""

from __future__ import annotations

import importlib.util
from pathlib import Path

_FINGERPRINT = Path(__file__).resolve().parents[2] / "fingerprint.py"


def _load():
    # Loaded by path, so the validator package need not put the repository root on sys.path.
    spec = importlib.util.spec_from_file_location("fingerprint", _FINGERPRINT)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {_FINGERPRINT}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def required_fingerprint() -> str:
    # Informational reference, or GATE_REQUIRED_FINGERPRINT for a remote API host.
    return str(_load().required())
