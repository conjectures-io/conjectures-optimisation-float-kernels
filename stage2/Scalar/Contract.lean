import FloatLib

/-!
# The contract: what a submitted binary32 kernel must prove, one obligation per slot

Pinned by the operator, imported by every submission, never edited by a miner. The
obligation is the same equation FloatLib proves for its own certified kernels:
the executable result equals the reference definition on every pair of operands,
NaNs, infinities and signed zeros included.
-/

namespace Competition

open FloatLib.Floats

/-- IEEE 754 binary32, as FloatLib spells it. -/
abbrev Binary32 := ExecFloat.Binary (exponentBits := 8) (fractionBits := 23)

/-- The addition slot's obligation: a kernel agrees with `ExecFloat.Spec.add` everywhere. -/
def Obligation (kernel : Binary32 → Binary32 → Binary32) : Prop :=
  ∀ x y : Binary32, kernel x y = ExecFloat.Spec.add x y

/-- The multiplication slot's obligation: a kernel agrees with `ExecFloat.Spec.mul` everywhere. -/
def MulObligation (kernel : Binary32 → Binary32 → Binary32) : Prop :=
  ∀ x y : Binary32, kernel x y = ExecFloat.Spec.mul x y

/-- The fused multiply-add slot's obligation: `x * y + z`, rounded once (`ExecFloat.Spec.fma`). -/
def FmaObligation (kernel : Binary32 → Binary32 → Binary32 → Binary32) : Prop :=
  ∀ x y z : Binary32, kernel x y z = ExecFloat.Spec.fma x y z

end Competition
