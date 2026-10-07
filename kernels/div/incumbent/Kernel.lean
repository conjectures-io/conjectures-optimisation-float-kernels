import Contract

/-!
The division slot's incumbent as a submission: FloatLib's own certified binary32 division, and
its own theorem. What every miner in this slot starts from.
-/

namespace Submission

open FloatLib.Floats Competition

/-- The certified division FloatLib's planner selects for binary32. -/
def div (x y : Binary32) : Binary32 := ExecFloat.div x y

theorem div_spec : ∀ x y : Binary32, div x y = ExecFloat.Spec.div x y :=
  fun x y => ExecFloat.Proof.div_eq_spec x y

end Submission
