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


def test_an_entry_out_of_time_is_rejected_and_marked_exhausted(tmp_path, monkeypatch):
    # Every command the entry controls shares one budget; running out marks the verdict charged.
    calls: list[float] = []

    def slow(cmd, timeout, **kw):
        calls.append(timeout)
        return 124, ""

    monkeypatch.setattr(gate, "run", slow)
    monkeypatch.setattr(gate, "WORK", tmp_path / "work" / "Submission.lean")
    gate._budget.reset()
    v = gate.Verdict(submission="slow", slot="full")
    src = kernel(tmp_path, b"import ContractTensor\n")
    gate.run_stages(src, v, [("statement", gate.statement)])
    assert not v.passed and v.stage == "statement"
    assert "time budget" in v.reason and gate._budget.exhausted
    assert calls == [gate._BUDGET_SECONDS]


def test_an_ordinary_rejection_is_not_exhausted(tmp_path, monkeypatch):
    # A proof that fails to typecheck in time is a free retry, as before.
    monkeypatch.setattr(gate, "run", lambda cmd, timeout, **kw: (1, "error: type mismatch"))
    monkeypatch.setattr(gate, "WORK", tmp_path / "work" / "Submission.lean")
    gate._budget.reset()
    v = gate.Verdict(submission="wrong", slot="full")
    src = kernel(tmp_path, b"import ContractTensor\n")
    gate.run_stages(src, v, [("statement", gate.statement)])
    assert not v.passed and "type mismatch" in v.reason and not gate._budget.exhausted


def test_the_budget_caps_each_command_at_what_is_left(monkeypatch):
    # Time already spent shrinks the next command's timeout, never below a second.
    seen: list[float] = []
    monkeypatch.setattr(gate, "run", lambda cmd, timeout, **kw: (seen.append(timeout), (0, ""))[1])
    gate._budget.reset()
    gate._budget.left = 42.0
    gate.own(["true"], gate._BUILD_TIMEOUT)
    gate._budget.left = -5.0
    gate.own(["true"], gate._BUILD_TIMEOUT)
    assert seen[0] <= 42.0 and seen[1] == 1.0
