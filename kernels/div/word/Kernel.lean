import Contract

/-!
The planner's binary32 word division called directly, skipping the capability dispatch.
Same proof family as the incumbent; it shows whether the dispatch layer costs anything.
-/

namespace Submission

open FloatLib.Floats Competition
open FloatLib.Floats.Formats.BinaryInterchange

def div (x y : Binary32) : Binary32 := Configured.Backend.wordDiv x y

theorem div_spec : ∀ x y : Binary32, div x y = ExecFloat.Spec.div x y :=
  fun x y => Configured.Backend.wordDiv_eq_spec x y

end Submission
