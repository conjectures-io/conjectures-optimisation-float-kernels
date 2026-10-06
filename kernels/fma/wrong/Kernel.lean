import Contract

/-!
The classic mistake: multiply, round, add, round. Two roundings are not one, so its author
states the right theorem and cannot prove it.
-/

namespace Submission

open FloatLib.Floats Competition

def fma (x y z : Binary32) : Binary32 := ExecFloat.add (ExecFloat.mul x y) z

theorem fma_spec : ∀ x y z : Binary32, fma x y z = ExecFloat.Spec.fma x y z :=
  fun x y z => ExecFloat.Proof.fma_eq_spec x y z

end Submission
