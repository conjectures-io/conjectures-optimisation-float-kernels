import ContractTensor
import Stage2.Contract

/-!
# The gate's sum rule is TorchLean's

The gate checks the sum slot against `Competition.sumSpec` (lean/contract/ContractTensor.lean) so
that it needs no TorchLean (which
needs a C compiler even to import). These theorems are the bridge: `sumSpec` is TorchLean's own
`reduceSumLeftSpec`, and `bmmSpec` is its `bmmSpec`, so a kernel the gate accepts meets TorchLean's specification, stated against
TorchLean's definitions. tests/test_torchlean_link.py builds this file.
-/

namespace Stage2.Link

open Runtime.Autograd.Cuda.KernelSpec

/-- The contract's restated sum is TorchLean's, by definition. -/
theorem sumSpec_eq_reduceSumLeftSpec {n : Nat} (x : Fin n → Competition.Binary32) :
    Competition.sumSpec x = reduceSumLeftSpec x := rfl

/-- Passing the gate's sum check is meeting TorchLean's sum obligation, and the other way round. -/
theorem sumObligation_iff (kernel : {n : Nat} → Vector Competition.Binary32 n → Competition.Binary32) :
    Competition.SumObligation kernel ↔ Stage2.SumObligation kernel :=
  Iff.rfl

/-- The contract's restated batched matmul is TorchLean's, by definition. -/
theorem bmmSpec_eq_torchlean (batch m n p : Nat) (A : Fin (batch * m * n) → Competition.Binary32)
    (B : Fin (batch * n * p) → Competition.Binary32) :
    Competition.bmmSpec batch m n p A B = Runtime.Autograd.Cuda.KernelSpec.bmmSpec batch m n p A B :=
  rfl

/-- Passing the gate's bmm check is meeting TorchLean's bmm obligation, and the other way round. -/
theorem bmmObligation_iff
    (kernel : (batch m n p : Nat) → Vector Competition.Binary32 (batch * m * n) →
      Vector Competition.Binary32 (batch * n * p) → Vector Competition.Binary32 (batch * m * p)) :
    Competition.BmmObligation kernel ↔ Stage2.BmmObligation kernel :=
  Iff.rfl

end Stage2.Link
