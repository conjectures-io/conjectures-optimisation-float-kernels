import Contract

/-!
The square-root slot's incumbent as a submission: FloatLib's own certified binary32 square
root, and its own theorem. What every miner in this slot starts from.
-/

namespace Submission

open FloatLib.Floats Competition

/-- The certified square root FloatLib's planner selects for binary32. -/
def sqrt (x : Binary32) : Binary32 := ExecFloat.sqrt x

theorem sqrt_spec : ∀ x : Binary32, sqrt x = ExecFloat.Spec.sqrt x :=
  fun x => ExecFloat.Proof.sqrt_eq_spec x

end Submission
