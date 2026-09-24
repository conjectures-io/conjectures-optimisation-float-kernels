import Contract
import FloatLib.Floats.Formats.BinaryInterchange.Configured.NativeFPU.Unchecked

/-! The host FPU: fast, and outside every FloatLib certificate. No proof can connect it to the spec. -/

namespace Submission

open FloatLib.Floats Competition

def add (x y : Binary32) : Binary32 := Formats.BinaryInterchange.Configured.NativeFPU.Unchecked.add32 x y

theorem add_spec : ∀ x y : Binary32, add x y = ExecFloat.Spec.add x y := by
  intro x y
  native_decide

end Submission
