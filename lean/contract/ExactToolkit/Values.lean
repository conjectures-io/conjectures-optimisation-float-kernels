import ExactToolkit.Decode
import ExactToolkit.Limbs

/-!
# Exact-runtime toolkit: specs, word values, the final rounding, and FloatLib bridges

Pinned, operator-provided and proved. Any exact `sum` or `dot` kernel can use it:

1. the specs: `dotSpec`, FloatLib's correctly rounded dot product, and `sumSpec`, the same with a
   vector of ones (the exact sum, rounded once);
2. the value of a raw finite word, `±mantOf · 2^(idxOf - 149)`;
3. `finishWord`: round an exact integer total `S · 2^e` once (`finishWord_of_ne`, `finishWord_zero`),
   and `finishLimbs`: the same from limb arrays, cheaply (`finishLimbs_eq`);
4. what FloatLib's dot product computes over finite words: its exact total and its zero-sign flags
   (`finiteDotSlice_eq`, `dotStateLoop_flags`).
-/

namespace ExactRuntime

open FloatLib.Floats.Formats.BinaryInterchange FloatLib.Floats

/-! ## 1. Specs -/

/-- A FloatLib binary32 value as a raw word. -/
@[inline] def toWord (z : Model Fmt) : UInt32 := UInt32.ofNat z.toNatBits

/-- FloatLib's correctly rounded dot product of the words (0 for unequal lengths). -/
def dotSpec (a b : Array UInt32) : UInt32 :=
  match Model.dot Fmt (a.map (Model.ofUInt32 (fmt := Fmt))) (b.map (Model.ofUInt32 (fmt := Fmt)))
      .nearestEven with
  | .ok z => toWord z
  | .error _ => 0

/-- binary32 one. -/
abbrev oneW : UInt32 := 0x3F800000

/-- The correctly rounded sum: the exact `x₁·1 + … + xₙ·1`, rounded once. -/
def sumSpec (x : Array UInt32) : UInt32 := dotSpec x (Array.replicate x.size oneW)

/-! ## 2. Word values -/

/-- Significand and binade index of a finite word: its value is `±mantOf · 2^(idxOf - 149)`. -/
@[inline] def mantOf (w : UInt32) : Nat :=
  if expOf w = 0 then (fracOf w).toNat else 2 ^ 23 + (fracOf w).toNat
@[inline] def idxOf (w : UInt32) : Nat := if expOf w = 0 then 0 else (expOf w).toNat - 1

/-- Signed significand. -/
def sv (w : UInt32) : Int := if signOf w then -(mantOf w : Int) else mantOf w

theorem idxOf_le (w : UInt32) (h : expOf w ≠ 255) : idxOf w ≤ 253 := by
  have hlt := expOf_toNat_lt w
  have hne : (expOf w).toNat ≠ 255 := fun h' => h (UInt32.toNat_inj.mp (by simpa using h'))
  unfold idxOf; split_ifs <;> omega

theorem mantOf_lt_24 (w : UInt32) : mantOf w < 2 ^ 24 := by
  have := fracOf_toNat_lt w
  unfold mantOf; split_ifs <;> omega

theorem mul_mant_lt (wa wb : UInt32) : mantOf wa * mantOf wb < 2 ^ 64 :=
  calc mantOf wa * mantOf wb < 2 ^ 24 * 2 ^ 24 :=
        Nat.mul_lt_mul'' (mantOf_lt_24 wa) (mantOf_lt_24 wb)
    _ ≤ 2 ^ 64 := by norm_num

theorem decodeD_toRat (w : UInt32) :
    (decodeD w).toRat = (sv w : ℚ) * 2 ^ ((idxOf w : ℤ) - 149) := by
  unfold decodeD sv mantOf idxOf
  by_cases he : expOf w = 0
  · by_cases hf : fracOf w = 0
    · cases signOf w <;>
        simp [he, hf, FloatLib.Numerics.Dyadic.toRat, FloatLib.Numerics.Dyadic.signedSignificand]
    · by_cases hs : signOf w = true <;>
        simp [he, hf, hs, FloatLib.Numerics.Dyadic.toRat, FloatLib.Numerics.Dyadic.signedSignificand]
  · have hpos : 1 ≤ (expOf w).toNat := by
      rcases Nat.eq_zero_or_pos (expOf w).toNat with h | h
      · exact absurd (UInt32.toNat_inj.mp (by simpa using h)) he
      · exact h
    have hexp : ((((expOf w).toNat - 1 : Nat) : ℤ) - 149) = ((expOf w).toNat : ℤ) - 150 := by
      omega
    by_cases hs : signOf w = true <;>
      simp [he, hs, FloatLib.Numerics.Dyadic.toRat, FloatLib.Numerics.Dyadic.signedSignificand,
        hexp]

theorem decodeD_negative (w : UInt32) : (decodeD w).negative = signOf w := by
  unfold decodeD; split_ifs <;> rfl

theorem decodeD_sig_zero (w : UInt32) :
    ((decodeD w).significand == 0) = decide (mantOf w = 0) := by
  unfold decodeD mantOf
  by_cases he : expOf w = 0
  · by_cases hf : fracOf w = 0
    · simp [he, hf]
    · have : (fracOf w).toNat ≠ 0 := fun h => hf (UInt32.toNat_inj.mp (by simpa using h))
      simp [he, hf, this]
  · simp [he]

/-! ## 3. The final rounding -/

/-- The exact value `S · 2^e` as a dyadic. -/
def ofIntDyadic (S e : Int) : FloatLib.Numerics.Dyadic := ⟨decide (S < 0), S.natAbs, e⟩

theorem toRat_ofIntDyadic (S e : Int) : (ofIntDyadic S e).toRat = (S : ℚ) * 2 ^ e := by
  have hval : (ofIntDyadic S e).signedSignificand = S := by
    unfold ofIntDyadic FloatLib.Numerics.Dyadic.signedSignificand
    by_cases hneg : S < 0
    · have hd : decide (S < 0) = true := by simpa using hneg
      simp only [hd, ite_true]
      rw [Int.ofNat_eq_natCast, Int.natCast_natAbs, abs_of_neg hneg, neg_neg]
    · have hd : decide (S < 0) = false := by simpa using hneg
      simp only [hd, Bool.false_eq_true, ite_false]
      rw [Int.ofNat_eq_natCast, Int.natCast_natAbs, abs_of_nonneg (not_lt.mp hneg)]
  simp only [FloatLib.Numerics.Dyadic.toRat, hval, Rat.ofInt_eq_cast]
  rfl

/-- A kernel's last step: round the exact total `S · 2^e` once, or, when it is zero, return the
signed zero `negZero` selects. -/
@[inline] def finishWord (S e : Int) (negZero : Bool) : UInt32 :=
  if S = 0 then (if negZero then 0x80000000 else 0)
  else toWord (Model.roundDyadic Fmt (ofIntDyadic S e))

/-- A nonzero total rounds as any other exact value with the same rational value. -/
theorem finishWord_of_ne (d : FloatLib.Numerics.Dyadic) (S e : Int) (negZero : Bool)
    (hS : S ≠ 0) (hd : d.toRat = (S : ℚ) * 2 ^ e) :
    finishWord S e negZero = toWord (Model.roundDyadic Fmt d) := by
  have hne : (S : ℚ) * 2 ^ e ≠ 0 := mul_ne_zero (by exact_mod_cast hS) (zpow_ne_zero _ two_ne_zero)
  simp only [finishWord, hS, ite_false]
  congr 1
  apply roundDyadic_congr Fmt (by decide)
  · simpa [ofIntDyadic] using hS
  · intro h0
    rw [← FloatLib.Numerics.Dyadic.toRat_eq_zero_iff, hd] at h0
    exact hne h0
  · rw [toRat_ofIntDyadic, hd]

theorem finishWord_zero (e : Int) (negZero : Bool) :
    finishWord 0 e negZero = toWord (Model.zero Fmt negZero) := by
  cases negZero <;> rfl

/-- Scaling the integer total by `2^k` and lowering the exponent by `k` changes nothing. -/
theorem finishWord_scale (S e : Int) (k : Nat) (negZero : Bool) :
    finishWord S (e + k) negZero = finishWord (S * 2 ^ k) e negZero := by
  by_cases hS : S = 0
  · simp [finishWord, hS]
  · have hS' : S * 2 ^ k ≠ 0 := mul_ne_zero hS (pow_ne_zero _ two_ne_zero)
    rw [finishWord_of_ne (ofIntDyadic S (e + k)) (S * 2 ^ k) e negZero hS' (by
      rw [toRat_ofIntDyadic, zpow_add₀ two_ne_zero, zpow_natCast]; push_cast; ring)]
    simp [finishWord, hS]

/-- Finish from limb arrays: the exact total `(pos - neg) · 2^e`, computed from the lowest
nonzero limb up, so the integers stay small. -/
@[inline] def finishLimbs (pos neg : Array Nat) (e : Int) (negZero : Bool) : UInt32 :=
  let lo := lowLimb pos neg pos.size 0
  finishWord ((limbValA (pos.extract lo pos.size) : Int) - limbValA (neg.extract lo neg.size))
    (e + ((32 * lo : Nat) : Int)) negZero

theorem finishLimbs_eq (pos neg : Array Nat) (e : Int) (negZero : Bool) :
    finishLimbs pos neg e negZero =
      finishWord ((limbVal pos.toList : Int) - limbVal neg.toList) e negZero := by
  unfold finishLimbs
  set lo := lowLimb pos neg pos.size 0
  have hz := lowLimb_zero pos neg pos.size 0
  have hp : limbVal pos.toList = 2 ^ (32 * lo) * limbVal (pos.toList.drop lo) :=
    limbVal_drop _ _ fun i hi => by rw [getD_toList]; exact (hz i (by omega) hi).1
  have hn : limbVal neg.toList = 2 ^ (32 * lo) * limbVal (neg.toList.drop lo) :=
    limbVal_drop _ _ fun i hi => by rw [getD_toList]; exact (hz i (by omega) hi).2
  rw [finishWord_scale, limbValA_eq, limbValA_eq, Array.toList_extract, Array.toList_extract,
    List.extract_eq_drop_take, List.extract_eq_drop_take,
    List.take_of_length_le (by simp), List.take_of_length_le (by simp), hp, hn]
  push_cast; ring_nf

/-! ## 4. FloatLib's dot product over finite words -/

/-- Signed product of the scaled significands of `a[k]` and `b[k]` over a slice. -/
def dsum (a b : Array UInt32) (index : Nat) : Nat → Int
  | 0 => 0
  | count + 1 => sv a[index]! * sv b[index]! * 2 ^ (idxOf a[index]! + idxOf b[index]!) +
      dsum a b (index + 1) count

/-- "Every product in the slice is a zero of negative sign." -/
def dallNZ (a b : Array UInt32) (index : Nat) : Nat → Bool
  | 0 => true
  | count + 1 => (decide (mantOf a[index]! * mantOf b[index]! = 0) &&
      Bool.xor (signOf a[index]!) (signOf b[index]!)) && dallNZ a b (index + 1) count

open Model.Reduction in
theorem finiteDotSlice_eq (a b : Array UInt32) (index remaining : Nat)
    (ha : index + remaining ≤ (a.map (Model.ofUInt32 (fmt := Fmt))).size)
    (hb : index + remaining ≤ (b.map (Model.ofUInt32 (fmt := Fmt))).size)
    (hfin : ∀ k, index ≤ k → k < index + remaining → expOf a[k]! ≠ 255 ∧ expOf b[k]! ≠ 255) :
    Semantics.finiteDotSlice (a.map (Model.ofUInt32 (fmt := Fmt)))
        (b.map (Model.ofUInt32 (fmt := Fmt))) index remaining ha hb =
      (dsum a b index remaining : ℚ) * 2 ^ (-298 : ℤ) := by
  induction remaining generalizing index with
  | zero => simp [Semantics.finiteDotSlice, dsum]
  | succ count ih =>
    rw [Semantics.finiteDotSlice, ih (index + 1) (by omega) (by omega)
      (fun k h1 h2 => hfin k (by omega) (by omega)), dsum]
    have hsa : index < a.size := by simp at ha; omega
    have hsb : index < b.size := by simp at hb; omega
    have hga : a[index]! = a[index] := getElem!_pos a index hsa
    have hgb : b[index]! = b[index] := getElem!_pos b index hsb
    have hf := hfin index le_rfl (by omega)
    rw [hga, hgb] at hf
    simp only [Array.getElem_map, Semantics.finiteContribution,
      toDyadic_ofUInt32 _ hf.1, toDyadic_ofUInt32 _ hf.2, decodeD_toRat, hga, hgb]
    push_cast
    rw [zpow_sub₀ (by norm_num : (2 : ℚ) ≠ 0), zpow_sub₀ (by norm_num : (2 : ℚ) ≠ 0),
      zpow_natCast, zpow_natCast]
    rw [show (-298 : ℤ) = -149 + -149 by norm_num, zpow_add₀ (by norm_num : (2 : ℚ) ≠ 0)]
    rw [zpow_neg, zpow_ofNat]
    field_simp
    ring

open Model.Reduction in
theorem dotStateLoop_flags (a b : Array UInt32) (st : Internal.State Fmt) (index remaining : Nat)
    (ha : index + remaining ≤ (a.map (Model.ofUInt32 (fmt := Fmt))).size)
    (hb : index + remaining ≤ (b.map (Model.ofUInt32 (fmt := Fmt))).size)
    (hfin : ∀ k, index ≤ k → k < index + remaining → expOf a[k]! ≠ 255 ∧ expOf b[k]! ≠ 255) :
    (Internal.dotStateLoop (a.map (Model.ofUInt32 (fmt := Fmt)))
      (b.map (Model.ofUInt32 (fmt := Fmt))) st index remaining ha hb).sawFiniteTerm =
        (st.sawFiniteTerm || decide (remaining ≠ 0)) ∧
    (Internal.dotStateLoop (a.map (Model.ofUInt32 (fmt := Fmt)))
      (b.map (Model.ofUInt32 (fmt := Fmt))) st index remaining ha hb).allTermsNegativeZero =
        (st.allTermsNegativeZero && dallNZ a b index remaining) := by
  induction remaining generalizing st index with
  | zero => simp [Internal.dotStateLoop, dallNZ]
  | succ count ih =>
    have hsa : index < a.size := by simp at ha; omega
    have hsb : index < b.size := by simp at hb; omega
    have hga : a[index]! = a[index] := getElem!_pos a index hsa
    have hgb : b[index]! = b[index] := getElem!_pos b index hsb
    have hf := hfin index le_rfl (by omega)
    rw [hga, hgb] at hf
    have hfa : Model.isFinite (Model.ofUInt32 a[index] : Model Fmt) = true := by
      simp [isFinite_ofUInt32, hf.1]
    have hfb : Model.isFinite (Model.ofUInt32 b[index] : Model Fmt) = true := by
      simp [isFinite_ofUInt32, hf.2]
    rw [Internal.dotStateLoop]
    simp only [Array.getElem_map]
    rw [Internal.State.pushProduct_eq_pushExact _ _ _ hfa hfb,
      Model.finiteDyadic_eq_of_toDyadic hfa (toDyadic_ofUInt32 _ hf.1),
      Model.finiteDyadic_eq_of_toDyadic hfb (toDyadic_ofUInt32 _ hf.2)]
    obtain ⟨h1, h2⟩ := ih _ (index + 1) (by omega) (by omega)
      (fun k k1 k2 => hfin k (by omega) (by omega))
    refine ⟨?_, ?_⟩
    · rw [h1]; simp [Internal.State.pushExact]
    · rw [h2, dallNZ, hga, hgb]
      have h1' := Bool.eq_iff_iff.mp (decodeD_sig_zero a[index])
      have h2' := Bool.eq_iff_iff.mp (decodeD_sig_zero b[index])
      simp only [beq_iff_eq, decide_eq_true_eq] at h1' h2'
      have hz : ((decodeD a[index]).significand * (decodeD b[index]).significand == 0) =
          decide (mantOf a[index] * mantOf b[index] = 0) := by
        rw [Bool.eq_iff_iff]
        simp only [beq_iff_eq, decide_eq_true_eq, Nat.mul_eq_zero, h1', h2']
      simp only [Internal.State.pushExact, FloatLib.Numerics.Dyadic.mul,
        FloatLib.Numerics.Dyadic.mulFields, decodeD_negative, hz]
      simp [Bool.and_assoc]

end ExactRuntime
