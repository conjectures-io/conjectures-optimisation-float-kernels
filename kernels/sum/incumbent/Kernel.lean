import ContractTensor

/-!
The sum slot's incumbent as a submission: a left fold of FloatLib's certified binary32 addition,
in TorchLean's order. What every miner in this slot starts from.
-/

namespace Submission

open FloatLib.Floats Competition

/-- Add the elements left to right, from +0, with FloatLib's addition. -/
def sum {n : Nat} (x : Vector Binary32 n) : Binary32 := x.foldl ExecFloat.add posZero

theorem sum_spec : ∀ (n : Nat) (x : Vector Binary32 n), sum x = sumSpec x.get := by
  intro n x
  -- A vector's left fold is the spec's fold over `List.finRange`.
  conv_lhs => rw [sum, ← Vector.ofFn_getElem (xs := x)]
  rw [← Vector.foldl_toList, Vector.toList_ofFn, List.ofFn_eq_map, List.foldl_map]
  rfl

end Submission
