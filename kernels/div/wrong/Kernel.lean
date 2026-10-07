import Contract

/-! A kernel that is not division. Its author states the right theorem and cannot prove it. -/

namespace Submission

open FloatLib.Floats Competition

def div (x y : Binary32) : Binary32 := ExecFloat.mul x y

theorem div_spec : ∀ x y : Binary32, div x y = ExecFloat.Spec.div x y :=
  fun x y => ExecFloat.Proof.div_eq_spec x y

end Submission
