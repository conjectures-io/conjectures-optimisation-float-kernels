import Contract

/-! A kernel that is not addition. Its author states the right theorem and cannot prove it. -/

namespace Submission

open FloatLib.Floats Competition

def add (x y : Binary32) : Binary32 := ExecFloat.sub x y

theorem add_spec : ∀ x y : Binary32, add x y = ExecFloat.Spec.add x y :=
  fun x y => ExecFloat.Proof.add_eq_spec x y

end Submission
