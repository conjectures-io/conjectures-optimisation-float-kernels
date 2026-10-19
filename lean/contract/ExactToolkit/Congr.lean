import ExactToolkit.Round

/-!
# Equal exact values round to the same float

Lifts `round_scale` to FloatLib's `Model.roundDyadic`: two dyadics with nonzero significands and
the same rational value round to the same `Model`, in every IEEE format.
-/

namespace ExactRuntime

open FloatLib.Floats.Formats.BinaryInterchange FloatLib.Numerics

/-- Equal nonzero values: the same sign, and one significand is the other with trailing zeros. -/
theorem dyadic_scale_of_toRat_eq (n1 n2 : Bool) (m1 m2 : Nat) (e1 e2 : Int)
    (hle : e1 ≤ e2) (hm2 : m2 ≠ 0)
    (h : (FloatLib.Numerics.Dyadic.mk n1 m1 e1).toRat = (FloatLib.Numerics.Dyadic.mk n2 m2 e2).toRat) :
    n1 = n2 ∧ m1 = m2 * 2 ^ (e2 - e1).toNat := by
  obtain ⟨k, hk⟩ : ∃ k : Nat, e2 = e1 + k := ⟨(e2 - e1).toNat, by omega⟩
  have hkt : (e2 - e1).toNat = k := by omega
  rw [hkt]
  simp only [FloatLib.Numerics.Dyadic.toRat, FloatLib.Numerics.Dyadic.signedSignificand] at h
  rw [hk, zpow_add₀ (by norm_num : (2 : ℚ) ≠ 0), zpow_natCast] at h
  have hpos : (2 : ℚ) ^ e1 ≠ 0 := zpow_ne_zero _ (by norm_num)
  have hm2pos : (0 : ℚ) < m2 * 2 ^ k := by
    have : 0 < m2 * 2 ^ k := Nat.mul_pos (Nat.pos_of_ne_zero hm2) (by positivity)
    exact_mod_cast this
  have hm1nn : (0 : ℚ) ≤ m1 := by positivity
  cases n1 <;> cases n2 <;>
    simp only [Bool.false_eq_true, ite_false, ite_true, Rat.ofInt_eq_cast, Int.ofNat_eq_natCast,
      Int.cast_neg, Int.cast_natCast] at h
  · refine ⟨rfl, ?_⟩
    have : (m1 : ℚ) = m2 * 2 ^ k := by
      apply mul_right_cancel₀ hpos; rw [h]; ring
    exact_mod_cast this
  · exfalso
    have : (m1 : ℚ) = -(m2 * 2 ^ k) := by
      apply mul_right_cancel₀ hpos; rw [h]; ring
    linarith
  · exfalso
    have : -(m1 : ℚ) = m2 * 2 ^ k := by
      apply mul_right_cancel₀ hpos; rw [h]; ring
    linarith
  · refine ⟨rfl, ?_⟩
    have : (m1 : ℚ) = m2 * 2 ^ k := by
      have h2 : -(m1 : ℚ) = -(m2 * 2 ^ k) := by
        apply mul_right_cancel₀ hpos; rw [h]; ring
      linarith
    exact_mod_cast this

/-- Two dyadics with the same nonzero value round to the same float. -/
theorem roundDyadic_congr (fmt : FloatFormat) (hfmt : fmt.isIEEE = true) (d1 d2 : FloatLib.Numerics.Dyadic)
    (h1 : d1.significand ≠ 0) (h2 : d2.significand ≠ 0) (h : d1.toRat = d2.toRat) :
    Model.roundDyadic fmt d1 = Model.roundDyadic fmt d2 := by
  suffices key : ∀ a b : FloatLib.Numerics.Dyadic, b.significand ≠ 0 → a.toRat = b.toRat →
      a.exponent ≤ b.exponent → Model.roundDyadic fmt a = Model.roundDyadic fmt b by
    rcases le_total d1.exponent d2.exponent with hle | hle
    · exact key d1 d2 h2 h hle
    · exact (key d2 d1 h1 h.symm hle).symm
  intro a b hb hab hle
  obtain ⟨na, ma, ea⟩ := a
  obtain ⟨nb, mb, eb⟩ := b
  simp only at hb hle
  obtain ⟨hn, hm⟩ := dyadic_scale_of_toRat_eq na nb ma mb ea eb hle hb hab
  subst hn hm
  simp only [Model.roundDyadic, hfmt, ite_true, Model.ieeeRoundDyadic]
  congr 1
  have := round_scale (FloatFormat.toModel fmt) (Model.modelSign na) mb eb (eb - ea).toNat hb
  rwa [show eb - (((eb - ea).toNat : Nat) : ℤ) = ea by omega] at this

end ExactRuntime
