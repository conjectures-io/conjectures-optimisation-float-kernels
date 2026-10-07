import Contract

/-! A kernel that is not a square root. Its author states the right theorem and cannot prove it. -/

namespace Submission

open FloatLib.Floats Competition

def sqrt (x : Binary32) : Binary32 := x

theorem sqrt_spec : ∀ x : Binary32, sqrt x = ExecFloat.Spec.sqrt x :=
  fun x => ExecFloat.Proof.sqrt_eq_spec x

end Submission
