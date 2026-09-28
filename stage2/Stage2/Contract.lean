import NN.Runtime.Autograd.Engine.Cuda.KernelSpec

/-!
# Stage-2 contract draft: tensor kernels against TorchLean's Binary32 specs

The references are TorchLean's own: `reduceSumLeftSpec` (a left fold of `ExecFloat.add` from +0)
and `bmmSpec` (row-major batched matmul, each output a left fold of `add acc (mul a b)` from +0).
A kernel takes and returns `Vector Binary32`, the benchable form; the specs read buffers as
`Fin n → Binary32`, so the obligations compare through `Vector.get`. Exact equality, so the
accumulation order is part of the contract.
-/

namespace Stage2

open FloatLib.Floats
open Runtime.Autograd.Cuda.KernelSpec

/-- IEEE 754 binary32, as FloatLib spells it; TorchLean's `RefScalar` is this type. -/
abbrev Binary32 := ExecFloat.Binary (exponentBits := 8) (fractionBits := 23)

/-- The sum slot: a kernel agrees with `reduceSumLeftSpec` on every length and every input. -/
def SumObligation (kernel : {n : Nat} → Vector Binary32 n → Binary32) : Prop :=
  ∀ (n : Nat) (x : Vector Binary32 n), kernel x = reduceSumLeftSpec x.get

/-- The batched-matmul slot: a kernel agrees with `bmmSpec` on every shape, element by element. -/
def BmmObligation
    (kernel : (batch m n p : Nat) → Vector Binary32 (batch * m * n) →
      Vector Binary32 (batch * n * p) → Vector Binary32 (batch * m * p)) : Prop :=
  ∀ (batch m n p : Nat) (A : Vector Binary32 (batch * m * n)) (B : Vector Binary32 (batch * n * p)),
    (kernel batch m n p A B).get = bmmSpec batch m n p A.get B.get

end Stage2
