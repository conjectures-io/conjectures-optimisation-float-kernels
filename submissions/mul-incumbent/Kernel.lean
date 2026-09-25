import Contract

/-!
The multiplication slot's incumbent as a submission: FloatLib's own certified binary32
multiplication, and its own theorem. What every miner in this slot starts from.
-/

namespace Submission

open FloatLib.Floats Competition

/-- The certified multiplication FloatLib's planner selects for binary32. -/
def mul (x y : Binary32) : Binary32 := ExecFloat.mul x y

theorem mul_spec : ∀ x y : Binary32, mul x y = ExecFloat.Spec.mul x y :=
  fun x y => ExecFloat.Proof.mul_eq_spec x y

end Submission
