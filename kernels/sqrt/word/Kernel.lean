import Contract

/-!
The planner's binary32 word square root called directly, skipping the capability dispatch.
Same proof family as the incumbent; it shows whether the dispatch layer costs anything.
-/

namespace Submission

open FloatLib.Floats Competition
open FloatLib.Floats.Formats.BinaryInterchange

def sqrt (x : Binary32) : Binary32 := Configured.Backend.wordSqrt x

theorem sqrt_spec : ∀ x : Binary32, sqrt x = ExecFloat.Spec.sqrt x :=
  fun x => Configured.Backend.wordSqrt_eq_spec x

end Submission
