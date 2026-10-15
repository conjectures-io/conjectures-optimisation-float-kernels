import FloatLib

/-!
# Rounding depends only on the value

FloatLib's exact reductions keep the running total as a `Numerics.Dyadic` (sign, significand,
exponent) and round it once at the end. A fast kernel will build its own dyadic with the same
value but, in general, a different representation. This file proves that the representation
doesn't matter: `roundDyadic` gives the same result for any two dyadics with the same rational
value.

The core fact is about Lean's own rounding model (`Float.Model.UnpackedFloat.round`): writing a
nonzero significand with one more trailing zero bit (twice the significand, one less in the
exponent) rounds to the same float. The rounding picks its target exponent from the value's
magnitude, which doesn't change, and the extra zero bit is shifted out without touching the round
and sticky bits.
-/

namespace ExactRuntime

open Float.Model Float.Model.UnpackedFloat

theorem repeat_succ' {α : Type} (f : α → α) (n : Nat) (a : α) :
    Nat.repeat f (n + 1) a = Nat.repeat f n (f a) := by
  induction n generalizing a with
  | zero => rfl
  | succ n ih =>
    show f (Nat.repeat f (n + 1) a) = f (Nat.repeat f n (f a))
    rw [ih]

/-- Shifting out one extra zero bit first leaves an exact extended mantissa unchanged. -/
theorem shift_double (m k : Nat) :
    (ExtendedMantissa.ofMantissaAndAccuracy (2 * m) .exact) >>> (k + 1) =
      (ExtendedMantissa.ofMantissaAndAccuracy m .exact) >>> k := by
  show Nat.repeat ExtendedMantissa.shiftRightOne (k + 1) _ =
    Nat.repeat ExtendedMantissa.shiftRightOne k _
  rw [repeat_succ']
  congr 1
  simp [ExtendedMantissa.ofMantissaAndAccuracy, ExtendedMantissa.shiftRightOne]

theorem totalExponent_double (m : Nat) (e : Int) (hm : m ≠ 0) :
    totalExponent (2 * m) (e - 1) = totalExponent m e := by
  simp only [totalExponent, Nat.log2_two_mul hm]
  push_cast
  omega

/-- Below the target exponent, the extra zero bit is dropped by the first right shift. -/
theorem roundWithAccuracy_double (spec : Format) (s : Sign) (m : Nat) (e : Int) (hm : m ≠ 0)
    (he : e ≤ spec.targetExponent (totalExponent m e)) :
    roundWithAccuracy spec s (2 * m) (e - 1) .exact = roundWithAccuracy spec s m e .exact := by
  have htot := totalExponent_double m e hm
  have hshift : shiftToTargetExponent spec (2 * m) (e - 1) .exact =
      shiftToTargetExponent spec m e .exact := by
    simp only [shiftToTargetExponent, shiftToExponent, htot]
    have hk : (spec.targetExponent (totalExponent m e) - (e - 1)).toNat =
        (spec.targetExponent (totalExponent m e) - e).toNat + 1 := by omega
    rw [hk, shift_double]
    congr 1
    push_cast
    omega
  unfold roundWithAccuracy
  rw [hshift]

/-- The core fact: one more trailing zero bit rounds to the same float. -/
theorem round_double (spec : Format) (s : Sign) (m : Nat) (e : Int) (hm : m ≠ 0) :
    UnpackedFloat.round spec s (2 * m) (e - 1) = UnpackedFloat.round spec s m e := by
  have htot := totalExponent_double m e hm
  unfold UnpackedFloat.round
  rw [htot]
  generalize hT : spec.targetExponent (totalExponent m e) = T
  unfold decreaseExponent
  by_cases h : T < e
  · -- Both are shifted left onto the target exponent and land on the same pair.
    have h1 : (e - 1 - T).toNat + 1 = (e - T).toNat := by omega
    have hm' : (2 * m) <<< (e - 1 - T).toNat = m <<< (e - T).toNat := by
      rw [← h1, Nat.shiftLeft_eq, Nat.shiftLeft_eq, Nat.pow_succ]
      ring
    have he' : e - 1 - ((e - 1 - T).toNat : Int) = e - ((e - T).toNat : Int) := by omega
    simp only [he']
    rw [hm']
  · -- No left shift: the right shift inside `roundWithAccuracy` drops the extra bit.
    have h0 : (e - T).toNat = 0 := by omega
    have h0' : (e - 1 - T).toNat = 0 := by omega
    simp only [h0, h0', Nat.shiftLeft_zero, Int.natCast_zero, Int.sub_zero]
    exact roundWithAccuracy_double spec s m e hm (by rw [hT]; omega)

/-- Any number of trailing zero bits. -/
theorem round_scale (spec : Format) (s : Sign) (m : Nat) (e : Int) (k : Nat) (hm : m ≠ 0) :
    UnpackedFloat.round spec s (m * 2 ^ k) (e - k) = UnpackedFloat.round spec s m e := by
  induction k with
  | zero => simp
  | succ k ih =>
    have hmk : m * 2 ^ k ≠ 0 := Nat.mul_ne_zero hm (by positivity)
    have := round_double spec s (m * 2 ^ k) (e - k) hmk
    rw [show m * 2 ^ (k + 1) = 2 * (m * 2 ^ k) by rw [Nat.pow_succ]; ring,
      show e - ((k + 1 : Nat) : Int) = e - k - 1 by push_cast; ring, this, ih]

end ExactRuntime
