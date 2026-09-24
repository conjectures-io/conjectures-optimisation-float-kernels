import FloatLib

/-!
# The contract: what a submitted binary32 addition kernel must prove

Pinned by the operator, imported by every submission, never edited by a miner. The
obligation is the same equation FloatLib proves for its own certified kernels:
the executable result equals the reference definition on every pair of operands,
NaNs, infinities and signed zeros included.
-/

namespace Competition

open FloatLib.Floats

/-- IEEE 754 binary32, as FloatLib spells it. -/
abbrev Binary32 := ExecFloat.Binary (exponentBits := 8) (fractionBits := 23)

/-- The obligation: a kernel agrees with `ExecFloat.Spec.add` everywhere. -/
def Obligation (kernel : Binary32 → Binary32 → Binary32) : Prop :=
  ∀ x y : Binary32, kernel x y = ExecFloat.Spec.add x y

end Competition
