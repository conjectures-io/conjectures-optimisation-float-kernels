import Contract

/-!
The planner's binary32 word kernel called directly, skipping the capability dispatch. Same
proof family as the incumbent; the question the benchmark answers is whether the dispatch
layer costs anything once the compiler has specialised it.
-/

namespace Submission

open FloatLib.Floats Competition
open FloatLib.Floats.Formats.BinaryInterchange

def add (x y : Binary32) : Binary32 := Configured.Backend.wordAdd x y

theorem add_spec : ∀ x y : Binary32, add x y = ExecFloat.Spec.add x y :=
  fun x y => Configured.Backend.wordAdd_eq_spec x y

end Submission
