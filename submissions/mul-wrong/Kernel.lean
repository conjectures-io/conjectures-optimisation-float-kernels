import Contract

/-! A kernel that is not multiplication. Its author states the right theorem and cannot prove it. -/

namespace Submission

open FloatLib.Floats Competition

def mul (x y : Binary32) : Binary32 := ExecFloat.add x y

theorem mul_spec : ∀ x y : Binary32, mul x y = ExecFloat.Spec.mul x y :=
  fun x y => ExecFloat.Proof.mul_eq_spec x y

end Submission
