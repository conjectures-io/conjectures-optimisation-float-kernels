import Contract

/-!
The fused multiply-add slot's incumbent as a submission: FloatLib's own certified binary32
fma, and its own theorem. What every miner in this slot starts from.
-/

namespace Submission

open FloatLib.Floats Competition

/-- The certified fused multiply-add FloatLib's planner selects for binary32. -/
def fma (x y z : Binary32) : Binary32 := ExecFloat.fma x y z

theorem fma_spec : ∀ x y z : Binary32, fma x y z = ExecFloat.Spec.fma x y z :=
  fun x y z => ExecFloat.Proof.fma_eq_spec x y z

end Submission
