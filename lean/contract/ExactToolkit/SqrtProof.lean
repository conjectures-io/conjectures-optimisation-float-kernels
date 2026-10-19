import ExactToolkit.StableNorm
import FloatLib.Floats.Formats.BinaryInterchange.Arithmetic.SqrtSemantics

namespace ExactRuntime

set_option maxRecDepth 4096
set_option maxHeartbeats 400000

open FloatLib.Floats.Formats.BinaryInterchange FloatLib.Floats
open Float.Model.UnpackedFloat

/-- The certificate describes the actual real square root, not an approximation to its input. -/
theorem sqrtAccuracy_correct (num den : Nat) (hd : 0 < den) :
    Model.accuracyRepresents (sqrtLower num den) (sqrtAccuracy num den)
      (Real.sqrt ((num : ℝ) / den)) := by
  let root := sqrtLower num den
  have bounds := sqrtLower_bounds num den hd
  have hdR : (0 : ℝ) < den := by exact_mod_cast hd
  have hlo : (root : ℝ) ^ 2 * den ≤ num := by exact_mod_cast bounds.1
  have hhi : (num : ℝ) < ((root : ℝ) + 1) ^ 2 * den := by exact_mod_cast bounds.2
  have hlo' : (root : ℝ) ^ 2 ≤ (num : ℝ) / den := (le_div_iff₀ hdR).mpr hlo
  have hhi' : (num : ℝ) / den < ((root : ℝ) + 1) ^ 2 := (div_lt_iff₀ hdR).mpr hhi
  have hsq : Real.sqrt ((num : ℝ) / den) ^ 2 = (num : ℝ) / den :=
    Real.sq_sqrt (by positivity)
  have hnonneg := Real.sqrt_nonneg ((num : ℝ) / den)
  have hrootnonneg : (0 : ℝ) ≤ root := by positivity
  have hq : (num : ℝ) / den * den = num := by field_simp
  unfold sqrtAccuracy
  change Model.accuracyRepresents root
    (if num = root ^ 2 * den then .exact else .inexact
      (if 4 * num < den * (2 * root + 1) ^ 2 then .lt else
       if 4 * num = den * (2 * root + 1) ^ 2 then .eq else .gt)) _
  split_ifs with he hl heq
  · have heR : (num : ℝ) = (root : ℝ) ^ 2 * den := by exact_mod_cast he
    have heR' : (num : ℝ) / den = (root : ℝ) ^ 2 := (div_eq_iff hdR.ne').mpr heR
    simp only [Model.accuracyRepresents]
    nlinarith
  · have hneR : (num : ℝ) ≠ (root : ℝ) ^ 2 * den := by exact_mod_cast he
    have hneR' : (num : ℝ) / den ≠ (root : ℝ) ^ 2 := by
      intro h
      exact hneR ((div_eq_iff hdR.ne').mp h)
    have hstrict : (root : ℝ) ^ 2 < (num : ℝ) / den := lt_of_le_of_ne hlo' (Ne.symm hneR')
    have hlR : 4 * (num : ℝ) < den * (2 * (root : ℝ) + 1) ^ 2 := by exact_mod_cast hl
    have hlR' : (num : ℝ) / den < ((root : ℝ) + 1 / 2) ^ 2 := by
      apply (div_lt_iff₀ hdR).mpr
      nlinarith [hlR]
    simp only [Model.accuracyRepresents]
    constructor <;> nlinarith
  · have heqR : 4 * (num : ℝ) = den * (2 * (root : ℝ) + 1) ^ 2 := by exact_mod_cast heq
    have heqR' : (num : ℝ) / den = ((root : ℝ) + 1 / 2) ^ 2 := by
      apply (div_eq_iff hdR.ne').mpr
      nlinarith [heqR]
    simp only [Model.accuracyRepresents]
    nlinarith
  · have hlR : den * (2 * (root : ℝ) + 1) ^ 2 < 4 * num := by
      exact_mod_cast (lt_of_le_of_ne (Nat.le_of_not_lt hl) (Ne.symm heq))
    simp only [Model.accuracyRepresents]
    have hlR' : ((root : ℝ) + 1 / 2) ^ 2 < (num : ℝ) / den := by
      apply (lt_div_iff₀ hdR).mpr
      nlinarith [hlR]
    constructor <;> nlinarith

theorem sqrtRoot_pos (q : ℚ) (hq : 0 < q) :
    0 < sqrtLower (sqrtNumerator q) q.den := by
  have hn : 0 < q.num.natAbs := Int.natAbs_pos.mpr (ne_of_gt (Rat.num_pos.mpr hq))
  have hd : q.den < 2 ^ (q.den.log2 + 1) := Nat.lt_log2_self
  have hp : 2 ^ (q.den.log2 + 1) ≤ 2 ^ (2 * sqrtShift q) :=
    Nat.pow_le_pow_right (by decide) (by unfold sqrtShift; omega)
  have hmul : 2 ^ (2 * sqrtShift q) ≤ sqrtNumerator q := by
    unfold sqrtNumerator
    exact Nat.le_mul_of_pos_left _ hn
  exact Nat.sqrt_pos.mpr (Nat.div_pos (by omega) q.pos)

theorem sqrtStep_le_target (q : ℚ) :
    -(sqrtShift q : Int) ≤ (FloatFormat.toModel Fmt).targetExponent
      (Float.Model.totalExponent (sqrtLower (sqrtNumerator q) q.den) (-(sqrtShift q : Int))) := by
  change -(sqrtShift q : Int) ≤ max _ (-149)
  exact le_trans (by unfold sqrtShift; omega) (le_max_right _ _)

open FloatLib.Floats.Formats.Flocq in
theorem sqrtScale_correct (q : ℚ) (hq : 0 < q) :
    Real.sqrt ((sqrtNumerator q : ℝ) / q.den) *
      bpow FloatLib.Numerics.binaryRadix (-(sqrtShift q : Int)) = Real.sqrt (q : ℝ) := by
  have hn : 0 < q.num := Rat.num_pos.mpr hq
  have hsource : (sqrtNumerator q : ℝ) / q.den =
      (q : ℝ) * ((2 : ℝ) ^ sqrtShift q) ^ 2 := by
    simp only [sqrtNumerator, Nat.cast_mul, Nat.cast_pow, Nat.cast_ofNat, Nat.cast_natAbs,
      abs_of_pos hn, Rat.cast_def]
    rw [show 2 * sqrtShift q = sqrtShift q * 2 by omega, pow_mul]
    ring
  rw [hsource, Real.sqrt_mul (by exact_mod_cast hq.le), Real.sqrt_sq_eq_abs,
    abs_of_pos (pow_pos (by norm_num : (0 : ℝ) < 2) _)]
  simp [bpow, FloatLib.Numerics.binaryRadix, FloatLib.Numerics.Radix.toReal,
    zpow_neg, zpow_natCast, mul_assoc]

-- Keep elaboration from expanding a symbolic power with a large constant suffix while
-- matching the already-proved certificate against FloatLib's generic theorem.
attribute [local irreducible] sqrtLower sqrtNumerator sqrtAccuracy sqrtShift

/-- Exact nearest-even rounded-real meaning, for every positive rational whose IEEE result
is finite (including underflow to zero). Overflow is handled by the same FloatLib rounder. -/
theorem sqrtModel_correct (q : ℚ) (hq : 0 < q)
    (hf : Model.isFinite (sqrtModel q) = true) :
    Model.toReal (sqrtModel q) = Model.roundAt Fmt (Real.sqrt (q : ℝ)) := by
  have hroot : sqrtLower (sqrtNumerator q) q.den ≠ 0 := ne_of_gt (sqrtRoot_pos q hq)
  have hacc := sqrtAccuracy_correct (sqrtNumerator q) q.den q.pos
  have hstep := sqrtStep_le_target q
  have hfinite : Model.isFinite (Model.ofModel Fmt
      (roundWithAccuracy (FloatFormat.toModel Fmt) .positive
        (sqrtLower (sqrtNumerator q) q.den) (-(sqrtShift q : Int))
        (sqrtAccuracy (sqrtNumerator q) q.den))) = true := hf
  have hr := Model.toReal_ofModel_roundWithAccuracy_eq_roundAt Fmt (by decide)
    .positive (sqrtLower (sqrtNumerator q) q.den) (-(sqrtShift q : Int))
    (sqrtAccuracy (sqrtNumerator q) q.den) (Real.sqrt ((sqrtNumerator q : ℝ) / q.den))
    hroot hacc hstep hfinite
  simpa only [sqrtModel, Model.modelSignBit, Bool.false_eq_true, ite_false, one_mul,
    sqrtScale_correct q hq] using hr

/-- Independent mathematical specification: nearest-even rounding of the real square root
on the binary32 gradual-underflow grid, followed by FloatLib's IEEE encoding. This has no
integer-square-root algorithm, denominator scaling, or certificate in its definition. -/
noncomputable def sqrtMathWord (q : ℚ) : UInt32 :=
  if q ≤ 0 then 0 else
    let value := Real.sqrt (q : ℝ)
    let exponent := FloatLib.Floats.Formats.Flocq.cexp FloatLib.Numerics.binaryRadix
      (Model.fexpOf Fmt) value
    let rounded := FloatLib.Floats.Formats.Flocq.nearestEven
      (FloatLib.Floats.Formats.Flocq.scaledMantissa FloatLib.Numerics.binaryRadix
        (Model.fexpOf Fmt) value)
    toWord (Model.ofModel Fmt (Model.finishRoundedMantissa (FloatFormat.toModel Fmt)
      .positive (rounded.toNat, exponent)))

/-- Complete bitwise rounding theorem, including overflow, underflow and ties.
The only packing operation on the mathematical side is FloatLib's standard IEEE encoder. -/
theorem sqrtRational_eq_math (q : ℚ) : sqrtRational q = sqrtMathWord q := by
  by_cases hq : q ≤ 0
  · simp only [sqrtRational, sqrtMathWord, hq, ite_true]
  have hpos : 0 < q := lt_of_not_ge hq
  let root := sqrtLower (sqrtNumerator q) q.den
  let exponent := -(sqrtShift q : Int)
  let accuracy := sqrtAccuracy (sqrtNumerator q) q.den
  let value := Real.sqrt ((sqrtNumerator q : ℝ) / q.den)
  let target := (FloatFormat.toModel Fmt).targetExponent (Float.Model.totalExponent root exponent)
  have hroot : root ≠ 0 := ne_of_gt (sqrtRoot_pos q hpos)
  have hacc : Model.accuracyRepresents root accuracy value := sqrtAccuracy_correct _ _ q.pos
  have hstep : exponent ≤ target := sqrtStep_le_target q
  have he := Model.cexp_accuracy_mul_bpow_eq_targetExponent Fmt root exponent accuracy value
    (by decide) hroot hacc
  have hs := Model.scaledMantissa_accuracy_mul_bpow Fmt root exponent accuracy value
    (by decide) hroot hacc
  have hn := Model.roundedMantissa_shiftToTargetExponent_eq_nearestEven Fmt root exponent
    accuracy value hacc hstep
  have hscale : value * FloatLib.Floats.Formats.Flocq.bpow FloatLib.Numerics.binaryRadix
      exponent = Real.sqrt (q : ℝ) := sqrtScale_correct q hpos
  rw [hscale] at he hs
  have hround : (FloatLib.Floats.Formats.Flocq.nearestEven
      (FloatLib.Floats.Formats.Flocq.scaledMantissa FloatLib.Numerics.binaryRadix
        (Model.fexpOf Fmt) (Real.sqrt (q : ℝ)))).toNat =
      (shiftToTargetExponent (FloatFormat.toModel Fmt) root exponent accuracy).1.roundedMantissa := by
    rw [hs, ← hn]
    simp
  have htarget : (shiftToTargetExponent (FloatFormat.toModel Fmt) root exponent accuracy).2 =
      target := by
    rw [Model.shiftToTargetExponent_eq_of_le_targetExponent Fmt root exponent accuracy hstep]
  simp only [sqrtRational, sqrtMathWord, hq, ite_false, he, hround]
  unfold sqrtModel
  rw [Model.roundWithAccuracy_eq_finishRoundedMantissa]
  change toWord (Model.ofModel Fmt (Model.finishRoundedMantissa (FloatFormat.toModel Fmt)
    .positive (_, (shiftToTargetExponent (FloatFormat.toModel Fmt) root exponent accuracy).2))) = _
  rw [htarget]

end ExactRuntime
