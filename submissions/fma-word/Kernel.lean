import Contract

/-!
The planner's binary32 word fma called directly, skipping the capability dispatch. Same proof
family as the incumbent; it shows whether the dispatch layer costs anything.
-/

namespace Submission

open FloatLib.Floats Competition
open FloatLib.Floats.Formats.BinaryInterchange

def fma (x y z : Binary32) : Binary32 := Configured.Backend.wordFma x y z

theorem fma_spec : ∀ x y z : Binary32, fma x y z = ExecFloat.Spec.fma x y z :=
  fun x y z => Configured.Backend.wordFma_eq_spec x y z

end Submission
