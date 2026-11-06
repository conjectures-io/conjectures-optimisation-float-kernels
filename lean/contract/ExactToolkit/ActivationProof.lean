import ExactToolkit.Activation
import FloatLib.Floats.Formats.BinaryInterchange.Transcendentals.CorrectlyRounded.Proof
import Mathlib.Analysis.SpecialFunctions.Artanh
import Mathlib.Order.Compare

/-!
# `sigmoidCR` and `tanhCR` are correctly rounded

For every IEEE descriptor and every finite input, `tanhModel` returns the nearest-even rounding of
`Real.tanh x` and `sigmoidModel` that of `1 / (1 + e^(−x))` (`toReal_tanhModel`,
`toReal_sigmoidModel`), and both results are finite. The only arithmetic facts used are the
exactness of FloatLib's logarithm comparison (`ElementaryComparison.compareLog_eq_real`), its
exact-comparison rounding theorem (`AlgebraicRounding.toReal_round_eq_roundAt`) and Mathlib's
`artanh`:

* `compareTanh_eq_real`: `compareTanh a b = cmp (tanh a) b` for `a ≥ 0`, through
  `tanh a < b ⟺ a < artanh b = ½ log ((1 + b) / (1 − b))` on `(−1, 1)`;
* `compareSigmoid_eq_real`: `compareSigmoid x b = cmp (σ x) b`, through `σ (logit b) = b` and the
  strict monotonicity of `σ`.

The enclosure searches tried first are sound by FloatLib's `contains_exp` and
`toReal_eq_roundAt_of_round?_eq_some`: `contains_tanhEnclosure` and `contains_sigmoidEnclosure`
show that each enclosure holds the true value, through `tanh a = (1 − e^(−2a)) / (1 + e^(−2a))`
and the monotonicity of the maps. So both paths give the correct rounding, and the result does
not depend on whether the search settles.
-/

namespace ExactRuntime

open FloatLib.Floats.Formats.BinaryInterchange FloatLib.Floats FloatLib.Numerics
open FloatLib.Floats.Formats.BinaryInterchange.Model

/-- The real logistic sigmoid. -/
noncomputable def realSigmoid (x : ℝ) : ℝ := 1 / (1 + Real.exp (-x))

theorem realSigmoid_pos (x : ℝ) : 0 < realSigmoid x := by
  unfold realSigmoid
  have := Real.exp_pos (-x)
  positivity

theorem realSigmoid_lt_one (x : ℝ) : realSigmoid x < 1 := by
  unfold realSigmoid
  have h := Real.exp_pos (-x)
  rw [div_lt_one (by linarith)]
  linarith

theorem realSigmoid_strictMono : StrictMono realSigmoid := by
  intro x y hxy
  unfold realSigmoid
  have hx := Real.exp_pos (-x)
  have hy := Real.exp_pos (-y)
  have h : Real.exp (-y) < Real.exp (-x) := Real.exp_lt_exp.mpr (by linarith)
  exact one_div_lt_one_div_of_lt (by linarith) (by linarith)

/-- `σ (log (b / (1 − b))) = b` for `0 < b < 1`. -/
theorem realSigmoid_logit {b : ℝ} (h0 : 0 < b) (h1 : b < 1) :
    realSigmoid (Real.log (b / (1 - b))) = b := by
  unfold realSigmoid
  have hq : 0 < b / (1 - b) := div_pos h0 (by linarith)
  rw [Real.exp_neg, Real.exp_log hq]
  have h1' : (1 - b) ≠ 0 := by linarith
  field_simp
  ring

/-- `cmp x (c · y) = cmp (x / c) y` for `c > 0`, the scaling used for `artanh`. -/
private theorem cmp_two_mul (a l : ℝ) : cmp a (1 / 2 * l) = cmp (2 * a) l := by
  have hm : StrictMono fun t : ℝ => 2 * t := fun _ _ h => by linarith
  rw [← hm.cmp_map_eq a (1 / 2 * l)]
  congr 1
  ring

theorem compareTanh_eq_real (a b : ℚ) (ha : 0 ≤ a) :
    compareTanh a b = cmp (Real.tanh (a : ℝ)) (b : ℝ) := by
  unfold compareTanh
  split
  · rename_i hb
    have hb' : (b : ℝ) ≤ 0 := by exact_mod_cast hb
    split
    · rename_i h0
      subst h0
      simp only [Rat.cast_zero, Real.tanh_zero]
      rcases lt_trichotomy (0 : ℚ) b with h | h | h
      · exact absurd (lt_of_lt_of_le h hb) (lt_irrefl 0)
      · subst h; simp
      · rw [(cmp_eq_gt_iff _ _).mpr h, (cmp_eq_gt_iff _ _).mpr (by exact_mod_cast h)]
    · rename_i h0
      have hpos : (0 : ℝ) < a := by exact_mod_cast lt_of_le_of_ne ha (Ne.symm h0)
      have htanh : 0 < Real.tanh (a : ℝ) := by
        rw [Real.tanh_eq_sinh_div_cosh]
        exact div_pos (Real.sinh_pos_iff.mpr hpos) (Real.cosh_pos _)
      exact ((cmp_eq_gt_iff _ _).mpr (by linarith)).symm
  · rename_i hb
    split
    · rename_i h1
      have h1' : (1 : ℝ) ≤ b := by exact_mod_cast h1
      exact ((cmp_eq_lt_iff _ _).mpr (lt_of_lt_of_le (Real.tanh_lt_one _) h1')).symm
    · rename_i h1
      have hb0 : (0 : ℝ) < b := by exact_mod_cast not_le.mp hb
      have hb1 : (b : ℝ) < 1 := by exact_mod_cast not_le.mp h1
      have hbI : (b : ℝ) ∈ Set.Ioo (-1) 1 := ⟨by linarith, hb1⟩
      have htI : Real.tanh (a : ℝ) ∈ Set.Ioo (-1) 1 :=
        ⟨Real.neg_one_lt_tanh _, Real.tanh_lt_one _⟩
      rw [ElementaryComparison.compareLog_eq_real, cmp_swap,
        ← Real.strictMonoOn_artanh.cmp_map_eq htI hbI, Real.artanh_tanh,
        Real.artanh_eq_half_log (Set.Ioo_subset_Icc_self hbI), cmp_two_mul]
      push_cast
      rfl

theorem compareSigmoid_eq_real (x b : ℚ) :
    compareSigmoid x b = cmp (realSigmoid (x : ℝ)) (b : ℝ) := by
  unfold compareSigmoid
  split
  · rename_i hb
    have hb' : (b : ℝ) ≤ 0 := by exact_mod_cast hb
    exact ((cmp_eq_gt_iff _ _).mpr (lt_of_le_of_lt hb' (realSigmoid_pos _))).symm
  · rename_i hb
    split
    · rename_i h1
      have h1' : (1 : ℝ) ≤ b := by exact_mod_cast h1
      exact ((cmp_eq_lt_iff _ _).mpr (lt_of_lt_of_le (realSigmoid_lt_one _) h1')).symm
    · rename_i h1
      have hb0 : (0 : ℝ) < b := by exact_mod_cast not_le.mp hb
      have hb1 : (b : ℝ) < 1 := by exact_mod_cast not_le.mp h1
      rw [ElementaryComparison.compareLog_eq_real, cmp_swap]
      conv_rhs => rw [← realSigmoid_logit hb0 hb1]
      rw [realSigmoid_strictMono.cmp_map_eq]
      push_cast
      rfl

variable {fmt : FloatFormat}

/-- For an IEEE descriptor, `1` lies below the overflow bound of exact-comparison rounding. -/
private theorem one_lt_overflowBound (hfmt : fmt.isIEEE = true) :
    (1 : ℝ) < FloatLib.Floats.Formats.Flocq.bpow FloatLib.Numerics.binaryRadix (fmt.maxNormalExponent + 1) -
      FloatLib.Floats.Formats.Flocq.bpow FloatLib.Numerics.binaryRadix
        (fmt.maxNormalExponent - fmt.fracWidth - 1) := by
  rw [← Transcendentals.CorrectlyRounded.cast_overflowThreshold]
  exact Transcendentals.CorrectlyRounded.one_lt_overflowThreshold hfmt

/-! ## The enclosure searches -/

/-- `tanh a = (1 − e^(−2a)) / (1 + e^(−2a))`. -/
theorem tanh_eq_exp_neg_two (a : ℝ) :
    Real.tanh a = (1 - Real.exp (-2 * a)) / (1 + Real.exp (-2 * a)) := by
  have h1 : Real.exp (-2 * a) = Real.exp (-a) * Real.exp (-a) := by
    rw [← Real.exp_add]; ring_nf
  have hpos := Real.exp_pos a
  rw [Real.tanh_eq a, h1, Real.exp_neg]
  field_simp

private theorem tanhMap_anti {p q : ℝ} (hp : 0 ≤ p) (hpq : p ≤ q) :
    (1 - q) / (1 + q) ≤ (1 - p) / (1 + p) := by
  rw [div_le_div_iff₀ (by linarith) (by linarith)]
  nlinarith

private theorem sigmoidMap_anti {p q : ℝ} (hp : 0 ≤ p) (hpq : p ≤ q) :
    1 / (1 + q) ≤ 1 / (1 + p) :=
  one_div_le_one_div_of_le (by linarith) (by linarith)

/-- `tanhOfExp` maps an enclosure of `e > 0` to one of `(1 − e) / (1 + e)`. -/
theorem contains_tanhOfExp {interval : RationalInterval} {e : ℝ} (he : 0 < e)
    (h : interval.Contains e) : (tanhOfExp interval).Contains ((1 - e) / (1 + e)) := by
  obtain ⟨hlo, hhi⟩ := h
  unfold tanhOfExp RationalInterval.Contains
  push_cast
  exact ⟨tanhMap_anti he.le (le_max_of_le_right hhi),
    tanhMap_anti (le_max_left _ _) (max_le he.le hlo)⟩

/-- `sigmoidOfExp` maps an enclosure of `e > 0` to one of `1 / (1 + e)`. -/
theorem contains_sigmoidOfExp {interval : RationalInterval} {e : ℝ} (he : 0 < e)
    (h : interval.Contains e) : (sigmoidOfExp interval).Contains (1 / (1 + e)) := by
  obtain ⟨hlo, hhi⟩ := h
  unfold sigmoidOfExp RationalInterval.Contains
  push_cast
  exact ⟨sigmoidMap_anti he.le (le_max_of_le_right hhi),
    sigmoidMap_anti (le_max_left _ _) (max_le he.le hlo)⟩

/-- `e^(−t) ≤ 2^(−n)` once `t ≥ n · 0.6931471808 > n · log 2`. -/
theorem exp_neg_le_inv_two_pow {t : ℝ} (n : ℕ) (h : (n : ℝ) * 0.6931471808 ≤ t) :
    Real.exp (-t) ≤ 1 / 2 ^ n := by
  have hlog := Real.log_two_lt_d9
  have hn : (0 : ℝ) ≤ n := Nat.cast_nonneg n
  have hle : (n : ℝ) * Real.log 2 ≤ t := by nlinarith
  calc Real.exp (-t) ≤ Real.exp (-((n : ℝ) * Real.log 2)) := Real.exp_le_exp.mpr (by linarith)
    _ = 1 / 2 ^ n := by
      rw [Real.exp_neg, Real.exp_nat_mul, Real.exp_log (by norm_num), one_div]

/-- The enclosure `[0, 2^(−46)]` of `e^(−t)` for `t ≥ 32`. -/
theorem contains_exp_small {t : ℝ} (h : 32 ≤ t) :
    (⟨0, 1 / 2 ^ 46⟩ : RationalInterval).Contains (Real.exp (-t)) := by
  unfold RationalInterval.Contains
  push_cast
  exact ⟨(Real.exp_pos _).le, exp_neg_le_inv_two_pow 46 (by norm_num; linarith)⟩

theorem contains_tanhEnclosure (x : ℚ) (degree precision : Nat) :
    (tanhEnclosure x degree precision).Contains (Real.tanh (x : ℝ)) := by
  have hexp : (if 16 < |x| then (⟨0, 1 / 2 ^ 46⟩ : RationalInterval)
      else Enclosure.BinaryGrid.exp (-2 * |x|) degree precision).Contains
        (Real.exp (-2 * |(x : ℝ)|)) := by
    split
    · rename_i hbig
      have h : (32 : ℝ) ≤ 2 * |(x : ℝ)| := by
        have : (16 : ℝ) < |(x : ℝ)| := by exact_mod_cast hbig
        linarith
      rw [show (-2 : ℝ) * |(x : ℝ)| = -(2 * |(x : ℝ)|) by ring]
      exact contains_exp_small h
    · have h := Enclosure.BinaryGrid.contains_exp (-2 * |x|) degree precision
      push_cast at h
      exact h
  have hpos := contains_tanhOfExp (Real.exp_pos _) hexp
  rw [← tanh_eq_exp_neg_two] at hpos
  unfold tanhEnclosure
  dsimp only
  by_cases hneg : x < 0
  · rw [if_pos hneg]
    have h := RationalInterval.contains_neg hpos
    rwa [abs_of_neg (by exact_mod_cast hneg : (x : ℝ) < 0), Real.tanh_neg, _root_.neg_neg] at h
  · rw [if_neg hneg]
    rwa [abs_of_nonneg (by exact_mod_cast not_lt.mp hneg : (0 : ℝ) ≤ x)] at hpos

theorem contains_sigmoidEnclosure (x : ℚ) (degree precision : Nat) :
    (sigmoidEnclosure x degree precision).Contains (realSigmoid (x : ℝ)) := by
  unfold sigmoidEnclosure
  split
  · rename_i hbig
    have h : (32 : ℝ) ≤ x := by exact_mod_cast hbig.le
    exact contains_sigmoidOfExp (Real.exp_pos _) (contains_exp_small h)
  · split
    · rename_i _ hsmall
      have hx : (128 : ℝ) < -(x : ℝ) := by
        have : (x : ℝ) < -128 := by exact_mod_cast hsmall
        linarith
      have hle : realSigmoid x ≤ Real.exp (x : ℝ) := by
        unfold realSigmoid
        have hprod : Real.exp (x : ℝ) * Real.exp (-(x : ℝ)) = 1 := by
          rw [← Real.exp_add]; simp
        rw [div_le_iff₀ (by positivity)]
        nlinarith [Real.exp_pos (x : ℝ), Real.exp_pos (-(x : ℝ))]
      have hbound : Real.exp (x : ℝ) ≤ 1 / 2 ^ 184 := by
        simpa using exp_neg_le_inv_two_pow 184 (t := -(x : ℝ)) (by norm_num; linarith)
      unfold RationalInterval.Contains
      push_cast
      exact ⟨(realSigmoid_pos _).le, hle.trans hbound⟩
    · have hexp := Enclosure.BinaryGrid.contains_exp (-x) degree precision
      push_cast at hexp
      exact contains_sigmoidOfExp (Real.exp_pos _) hexp

/-- A settled `tanh` search is finite and the nearest-even rounding of `tanh x`. -/
theorem tanhSearch_spec (x : ℚ) {result : Model fmt} (h : tanhSearch fmt x = some result) :
    isFinite result = true ∧ toReal result = roundAt fmt (Real.tanh (x : ℝ)) := by
  obtain ⟨degree, precision, hround⟩ := RationalInterval.exists_of_refine_eq_some h
  exact ⟨Transcendentals.Enclosure.isFinite_of_round?_eq_some hround,
    Transcendentals.Enclosure.toReal_eq_roundAt_of_round?_eq_some
      (contains_tanhEnclosure x degree precision) hround⟩

/-- A settled sigmoid search is finite and the nearest-even rounding of `σ x`. -/
theorem sigmoidSearch_spec (x : ℚ) {result : Model fmt} (h : sigmoidSearch fmt x = some result) :
    isFinite result = true ∧ toReal result = roundAt fmt (realSigmoid (x : ℝ)) := by
  obtain ⟨degree, precision, hround⟩ := RationalInterval.exists_of_refine_eq_some h
  exact ⟨Transcendentals.Enclosure.isFinite_of_round?_eq_some hround,
    Transcendentals.Enclosure.toReal_eq_roundAt_of_round?_eq_some
      (contains_sigmoidEnclosure x degree precision) hround⟩

/-! ## The exact comparisons -/

/-- The comparison path rounds `tanh x` correctly, to a finite value. -/
theorem tanhRound_spec (hfmt : fmt.isIEEE = true) (x : ℚ) :
    isFinite (AlgebraicRounding.round fmt (decide (x < 0)) (compareTanh |x|)) = true ∧
      toReal (AlgebraicRounding.round fmt (decide (x < 0)) (compareTanh |x|)) =
        roundAt fmt (Real.tanh (x : ℝ)) := by
  have hcmp : ∀ c : ℚ, compareTanh |x| c = cmp (Real.tanh ((|x| : ℚ) : ℝ)) (c : ℝ) :=
    fun c => compareTanh_eq_real _ c (abs_nonneg x)
  have hfinite : isFinite (AlgebraicRounding.round fmt (decide (x < 0)) (compareTanh |x|)) =
      true :=
    AlgebraicRounding.isFinite_round_of_lt_overflow fmt hfmt _ _ _ hcmp
      (lt_trans (Real.tanh_lt_one _) (one_lt_overflowBound hfmt))
  refine ⟨hfinite, ?_⟩
  have hnonneg : 0 ≤ Real.tanh ((|x| : ℚ) : ℝ) := by
    rw [Real.tanh_eq_sinh_div_cosh]
    exact div_nonneg (Real.sinh_nonneg_iff.mpr (by positivity)) (Real.cosh_pos _).le
  rw [AlgebraicRounding.toReal_round_eq_roundAt fmt hfmt _ _ _ hnonneg hcmp hfinite]
  congr 1
  by_cases hneg : x < 0
  · rw [decide_eq_true hneg, ite_eq_left rfl, Rat.cast_abs, abs_of_neg (by exact_mod_cast hneg),
      Real.tanh_neg, _root_.neg_neg]
  · rw [decide_eq_false hneg, ite_eq_right Bool.false_ne_true, Rat.cast_abs,
      abs_of_nonneg (by exact_mod_cast not_lt.mp hneg)]

/-- The comparison path rounds `σ x` correctly, to a finite value. -/
theorem sigmoidRound_spec (hfmt : fmt.isIEEE = true) (x : ℚ) :
    isFinite (AlgebraicRounding.round fmt false (compareSigmoid x)) = true ∧
      toReal (AlgebraicRounding.round fmt false (compareSigmoid x)) =
        roundAt fmt (realSigmoid (x : ℝ)) := by
  have hcmp : ∀ c : ℚ, compareSigmoid x c = cmp (realSigmoid (x : ℝ)) (c : ℝ) :=
    fun c => compareSigmoid_eq_real x c
  have hfinite : isFinite (AlgebraicRounding.round fmt false (compareSigmoid x)) = true :=
    AlgebraicRounding.isFinite_round_of_lt_overflow fmt hfmt _ _ _ hcmp
      (lt_trans (realSigmoid_lt_one _) (one_lt_overflowBound hfmt))
  refine ⟨hfinite, ?_⟩
  rw [AlgebraicRounding.toReal_round_eq_roundAt fmt hfmt _ _ _ (realSigmoid_pos _).le hcmp hfinite]
  rfl

/-! ## The activations -/

/-- `tanh` of a finite input is the nearest-even rounding of the real `tanh`, and finite. -/
theorem toReal_tanhModel (hfmt : fmt.isIEEE = true) {input : Model fmt}
    (hinput : isFinite input = true) :
    isFinite (tanhModel input) = true ∧
      toReal (tanhModel input) = roundAt fmt (Real.tanh (toReal input)) := by
  obtain ⟨x, hdecode, hreal⟩ := Algebraic.exists_toRat?_of_isFinite hinput
  have hnan := isNaN_eq_false_of_isFinite_eq_true input hinput
  have hinf := isInf_eq_false_of_isFinite_eq_true input hinput
  by_cases hzero : isZero input = true
  · have h0 := toReal_eq_zero_of_isZero input hzero
    simp only [tanhModel, hnan, hinf, hzero, ite_true, Bool.false_eq_true, ite_false]
    refine ⟨hinput, ?_⟩
    rw [h0, Real.tanh_zero, roundAt_zero]
  · rw [hreal]
    cases hs : tanhSearch fmt x with
    | some result =>
      have h : tanhModel input = result := by simp [tanhModel, hnan, hinf, hzero, hdecode, hs]
      rw [h]
      exact tanhSearch_spec x hs
    | none =>
      have h : tanhModel input =
          AlgebraicRounding.round fmt (decide (x < 0)) (compareTanh |x|) := by
        simp [tanhModel, hnan, hinf, hzero, hdecode, hs]
      rw [h]
      exact tanhRound_spec hfmt x

/-- `sigmoid` of a finite input is the nearest-even rounding of the real sigmoid, and finite. -/
theorem toReal_sigmoidModel (hfmt : fmt.isIEEE = true) {input : Model fmt}
    (hinput : isFinite input = true) :
    isFinite (sigmoidModel input) = true ∧
      toReal (sigmoidModel input) = roundAt fmt (realSigmoid (toReal input)) := by
  obtain ⟨x, hdecode, hreal⟩ := Algebraic.exists_toRat?_of_isFinite hinput
  have hnan := isNaN_eq_false_of_isFinite_eq_true input hinput
  have hinf := isInf_eq_false_of_isFinite_eq_true input hinput
  rw [hreal]
  cases hs : sigmoidSearch fmt x with
  | some result =>
    have h : sigmoidModel input = result := by simp [sigmoidModel, hnan, hinf, hdecode, hs]
    rw [h]
    exact sigmoidSearch_spec x hs
  | none =>
    have h : sigmoidModel input = AlgebraicRounding.round fmt false (compareSigmoid x) := by
      simp [sigmoidModel, hnan, hinf, hdecode, hs]
    rw [h]
    exact sigmoidRound_spec hfmt x

end ExactRuntime
