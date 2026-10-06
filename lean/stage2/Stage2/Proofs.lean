import Stage2.Contract
import Stage2.Kernels

/-!
# The spike kernels meet the stage-2 contract

Composition only: the scalar kernels equal `ExecFloat.add`/`mul` (stage 1's theorems plus
FloatLib's `*_eq_spec`), and the loops are the specs' folds. No new floating-point reasoning.
-/

namespace Stage2.Proofs

open FloatLib.Floats Stage2.Kernels
open Runtime.Autograd.Cuda.KernelSpec

theorem tunedAdd_eq : TunedAdd.add = ExecFloat.add := by
  funext x y
  rw [TunedAdd.add_spec, ExecFloat.Proof.add_eq_spec]

theorem fastMul_eq : FastMul.mul = ExecFloat.mul := by
  funext x y
  rw [FastMul.mul_spec, ExecFloat.Proof.mul_eq_spec]

/-- A vector's left fold is the spec's fold over `List.finRange`. -/
theorem vector_foldl_eq {α β : Type} {n : Nat} (f : β → α → β) (z : β) (x : Vector α n) :
    x.foldl f z = (List.finRange n).foldl (fun acc i => f acc (x.get i)) z := by
  conv_lhs => rw [← Vector.ofFn_getElem (xs := x)]
  rw [← Vector.foldl_toList, Vector.toList_ofFn, List.ofFn_eq_map, List.foldl_map]
  rfl

theorem sumFast_spec : Stage2.SumObligation sumFast := by
  intro n x
  show sumWith TunedAdd.add x = _
  rw [tunedAdd_eq]
  exact vector_foldl_eq _ _ x

/-- The kernel's bounded read is the spec's `getD`: same element in bounds, +0 outside. -/
theorem toArray_getD {N : Nat} (A : Vector Stage2.Binary32 N) (i : Nat) :
    A.toArray.getD i zero = getD A.get i := by
  unfold getD
  split <;> simp_all [Array.getD, Vector.get]

theorem bmmFast_spec : Stage2.BmmObligation bmmFast := by
  intro batch m n p A B
  show (bmmWith TunedAdd.add FastMul.mul batch m n p A B).get = _
  rw [tunedAdd_eq, fastMul_eq]
  funext q
  simp only [bmmWith, Vector.get, Vector.toArray_ofFn, Array.getElem_ofFn,
    Fin.foldl_eq_finRange_foldl, toArray_getD]
  rfl

/-- info: 'Stage2.Proofs.sumFast_spec' depends on axioms: [propext, Classical.choice, Quot.sound] -/
#guard_msgs in #print axioms sumFast_spec

/-- info: 'Stage2.Proofs.bmmFast_spec' depends on axioms: [propext, Classical.choice, Quot.sound] -/
#guard_msgs in #print axioms bmmFast_spec

end Stage2.Proofs
