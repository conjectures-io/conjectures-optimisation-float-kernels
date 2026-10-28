"""Fraud corpus for the policy stage: each entry tries one way to get paid without a correct, faster
kernel, and the policy must refuse it before any build. The proof cannot catch these: they change
what runs, or what the operator's bench and audit report, not what is proved."""

from __future__ import annotations

from pathlib import Path

import pytest
import sandbox

HEADER = "import ContractExact\n\nnamespace Submission\nopen ExactRuntime\n\n"
KERNELS = "def kernels : Kernels := specKernels\n"


def policy_of(tmp_path: Path, body: str) -> str | None:
    src = tmp_path / "Kernel.lean"
    src.write_text(HEADER + KERNELS + body + "\n\nend Submission\n")
    return sandbox.policy(src, sandbox.Verdict(submission="fraud"))


REFUSED = {
    # Run different code than was proved: the compiler swaps in another implementation.
    "implemented_by": "@[implemented_by fast] def slow (x : Nat) : Nat := x",
    "implemented_by quoted": "@[«implemented_by» fast] def slow (x : Nat) : Nat := x",
    "attribute command": "attribute [implemented_by fast] slow",
    "extern": '@[extern "lean_nat_add"] def add2 (a b : Nat) : Nat := a + b',
    "export": "@[export my_add] def add2 (a b : Nat) : Nat := a + b",
    "csimp": "@[csimp] theorem swap : @slow = @fast := rfl",
    "macro_inline": "@[macro_inline] def f (x : Nat) : Nat := x",
    "never_extract": "@[never_extract] def f (x : Nat) : Nat := x",
    "instance attribute": "@[instance] def i : Inhabited Nat := ⟨1⟩",
    "default_instance": "@[default_instance] instance : Inhabited Nat := ⟨1⟩",
    # Escape the proof.
    "sorry": "theorem t : False := sorry",
    "axiom": "axiom cheat : False",
    "native_decide": "theorem t : 2 + 2 = 4 := by native_decide",
    "skip kernel check": "set_option debug.skipKernelTC true in\ntheorem t : True := trivial",
    "unlimited heartbeats": "set_option maxHeartbeats 0 in\ntheorem t : True := trivial",
    # Run the miner's own code while the gate builds, or reach the host.
    "eval": "#eval (1 : Nat)",
    "print": "#print axioms kernels",
    "run_cmd": "run_cmd pure ()",
    "macro": 'macro "cheat" : term => `(0)',
    "syntax": 'syntax "cheat" : term',
    "elab": 'elab "cheat" : term => pure (Lean.mkNatLit 0)',
    "initialize": "initialize pure ()",
    "IO": "def f : IO Unit := pure ()",
    "unsafe": "unsafe def f : Nat := 0",
    "unsafeBaseIO": "def f : Nat := unsafeBaseIO (pure 0)",
    "dbg_trace": 'def f (x : Nat) : Nat := dbg_trace "x"; x',
    "include_str": 'def f : String := include_str "/etc/passwd"',
    "other import": "import Lean",
    # Change what the operator's bench or audit computes or prints: they import this module, and a
    # global instance or notation applies to their code too.
    "global instance": 'instance (priority := high) : ToString Nat := ⟨fun _ => "1"⟩',
    "default-priority instance": "instance : HSub Nat Nat Nat := ⟨fun _ _ => 1⟩",
    "named instance": 'instance fakeClock : ToString Nat := ⟨fun _ => "1"⟩',
    "scoped instance": 'scoped instance : ToString Nat := ⟨fun _ => "1"⟩',
    "private instance": 'private instance : ToString Nat := ⟨fun _ => "1"⟩',
    "deriving instance": "deriving instance BEq for Nat",
    "global notation": 'notation "elapsed" => (1 : Nat)',
    "infix": 'infixl:65 " - " => fun (_ _ : Nat) => (1 : Nat)',
    "prefix": 'prefix:max "√" => Nat.sqrt',
    "unification hint": "unif_hint (n : Nat) where n =?= 1 ⊢ n =?= 0",
}

ALLOWED = {
    "local instance": "local instance : Inhabited Nat := ⟨1⟩",
    "local notation": 'local notation "ℕ" => Nat',
    "local infix": 'local infixl:65 " +' + "'" + ' " => Nat.add',
    "attributes": "@[inline] def f (x : Nat) : Nat := x\n@[simp] theorem f_eq : f 1 = 1 := rfl",
    "deriving on own type": "structure P where\n  x : Nat\nderiving Repr, BEq",
    "instance in a word": "def useInstance : Inhabited Nat := inferInstance",
}


@pytest.mark.parametrize("body", REFUSED.values(), ids=REFUSED.keys())
def test_fraud_is_refused_before_any_build(tmp_path, body):
    assert sandbox.policy is not None and policy_of(tmp_path, body) is not None


@pytest.mark.parametrize("body", ALLOWED.values(), ids=ALLOWED.keys())
def test_ordinary_lean_is_allowed(tmp_path, body):
    assert policy_of(tmp_path, body) is None


@pytest.mark.parametrize("fixture", ["fakeclock", "fakeaudit"])
def test_the_fraud_fixtures_are_refused(fixture):
    src = sandbox.HERE / "kernels/exact" / fixture / "Kernel.lean"
    assert "instance" in (sandbox.policy(src, sandbox.Verdict(submission=fixture)) or "")


def test_a_benchmark_worker_refuses_the_fake_clock_before_building(tmp_path, monkeypatch):
    # The workers re-run the policy before every build; without it this entry reports 1 ns per
    # cell with correct checksums (shown with real Lean on 2026-10-28).
    import hashlib

    import benchmark

    monkeypatch.setattr(sandbox, "HERE", tmp_path)
    monkeypatch.setattr(sandbox, "LAYER", tmp_path / "layer")
    monkeypatch.setattr(benchmark, "machine_identity", lambda: "test-machine")
    source = (
        Path(__file__).resolve().parents[1] / "kernels/exact/fakeclock/Kernel.lean"
    ).read_bytes()
    with pytest.raises(benchmark.MeasurementFailure, match="instance without `local`") as failure:
        benchmark.build(
            source, hashlib.sha256(source).hexdigest(), "candidate", benchmark.Deadline("build", 60)
        )
    assert failure.value.kind == "candidate"
