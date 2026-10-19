"""The verifier fingerprint covers every file and setting that decides a verdict."""

from __future__ import annotations

import pytest

import fingerprint

DEV_TOOLS = {"Micro.lean", "ExactDev.lean"}


def test_every_contract_verify_and_bench_file_is_pinned():
    # Subfolders too: the exact slot's toolkit lives in contract/ExactToolkit/.
    lean = fingerprint.LIB
    files = {
        str(p.relative_to(fingerprint.HERE))
        for d in ("contract", "verify", "bench")
        for p in (lean / d).rglob("*.lean")
        if p.name not in DEV_TOOLS
    }
    assert "lean/contract/ExactToolkit/Values.lean" in files
    assert files <= set(fingerprint.PINNED)


@pytest.mark.parametrize("knob, value", [("GATE_CELL_MS", "20"), ("GATE_GUARD", "1.5")])
def test_the_exact_slots_settings_change_the_fingerprint(monkeypatch, knob, value):
    # Both settings change what a score means, so both change the fingerprint.
    monkeypatch.delenv(knob, raising=False)
    before = fingerprint.current()
    monkeypatch.setenv(knob, value)
    assert fingerprint.current() != before
