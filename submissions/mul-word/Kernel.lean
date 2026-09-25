import Contract

/-!
The planner's binary32 word multiplication called directly, skipping the capability dispatch.
Same proof family as the incumbent; it shows whether the dispatch layer costs anything.
-/

namespace Submission

open FloatLib.Floats Competition
open FloatLib.Floats.Formats.BinaryInterchange

def mul (x y : Binary32) : Binary32 := Configured.Backend.wordMul x y

theorem mul_spec : ∀ x y : Binary32, mul x y = ExecFloat.Spec.mul x y :=
  fun x y => Configured.Backend.wordMul_eq_spec x y

end Submission
