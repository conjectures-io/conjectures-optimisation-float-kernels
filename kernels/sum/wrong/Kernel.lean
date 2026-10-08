import ContractTensor

/-!
The right sum in the wrong order: the same additions, right to left. Equal as real numbers, not as
floats, because each addition rounds; the order is part of the rule, so this cannot be proved.
-/

namespace Submission

open FloatLib.Floats Competition

def sum {n : Nat} (x : Vector Binary32 n) : Binary32 :=
  x.foldr (fun a acc => ExecFloat.add acc a) posZero

theorem sum_spec : ∀ (n : Nat) (x : Vector Binary32 n), sum x = sumSpec x.get := by
  intro n x
  conv_lhs => rw [sum, ← Vector.ofFn_getElem (xs := x)]
  rw [← Vector.foldl_toList, Vector.toList_ofFn, List.ofFn_eq_map, List.foldl_map]
  rfl

end Submission
