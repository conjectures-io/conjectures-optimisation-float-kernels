"""The gate's intake stage: what it refuses before any later stage reads the file as text."""

from __future__ import annotations

from pathlib import Path

import gate

REPO = Path(__file__).resolve().parents[1]


def kernel(tmp_path: Path, payload: bytes) -> Path:
    src = tmp_path / "Kernel.lean"
    src.write_bytes(payload)
    return src


def test_a_file_that_is_not_utf8_is_rejected_at_intake_not_crashed_at_policy(tmp_path):
    # Without the intake check, policy's read_text raised and the gate reported its own fault.
    src = kernel(tmp_path, b"import Contract\n-- caf\xe9\n")
    v = gate.Verdict(submission="latin1")
    gate.run_stages(src, v, [("intake", gate.intake), ("policy", gate.policy)])
    assert not v.passed and v.stage == "intake"
    assert v.reason == "not UTF-8: invalid byte at offset 22"


def test_utf8_passes_intake(tmp_path):
    src = kernel(tmp_path, "import Contract\n-- x ∀ y : Binary32\n".encode())
    assert gate.intake(src, gate.Verdict(submission="ok")) is None


def test_an_oversized_or_missing_file_is_rejected(tmp_path):
    v = gate.Verdict(submission="x")
    assert "missing" in gate.intake(tmp_path / "nope.lean", v)
    big = kernel(tmp_path, b"-" * (gate._MAX_BYTES + 1))
    assert "over" in gate.intake(big, v)


def test_each_slot_imports_only_its_own_contract(tmp_path):
    # Scalar slots import Contract, the tensor slot sum ContractTensor; never the other one.
    src = kernel(tmp_path, b"import ContractTensor\n")
    assert gate.policy(src, gate.Verdict(submission="k", slot="sum")) is None
    assert "other than Contract" in gate.policy(src, gate.Verdict(submission="k", slot="add"))
    src.write_bytes(b"import Contract\n")
    assert gate.policy(src, gate.Verdict(submission="k", slot="add")) is None
    assert "other than ContractTensor" in gate.policy(src, gate.Verdict(submission="k", slot="sum"))
