import Contract

/-!
The incumbent as a submission: FloatLib's own certified binary32 addition, and its own
theorem. This is what every miner starts from, and what the score is measured against.
-/

namespace Submission

open FloatLib.Floats Competition

/-- The certified addition FloatLib's planner selects for binary32. -/
def add (x y : Binary32) : Binary32 := ExecFloat.add x y

theorem add_spec : ∀ x y : Binary32, add x y = ExecFloat.Spec.add x y :=
  fun x y => ExecFloat.Proof.add_eq_spec x y

end Submission
