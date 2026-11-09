import ExactToolkit.Spec

/-!
# Rounding a word by bit operations

`roundW neg m e` is the binary32 word of `±m · 2^e`, rounded to nearest even: by bit operations
when the result is a normal number below `2^127`, otherwise through FloatLib. `roundW_eq` proves it
equals FloatLib's `roundDyadic`. `bracket_spec` is the bridge used by fast paths: if both ends of
an enclosure round to the same finite nonzero word, every value between them rounds to it.
-/

namespace ExactRuntime.FastRound

open ExactRuntime FloatLib.Floats.Formats.BinaryInterchange FloatLib.Floats FloatLib.Numerics

theorem toWord_ofW' (w : UInt32) : toWord (ofW w) = w := by
  apply UInt32.toNat_inj.mp
  simp only [toWord, ofW, Model.toNatBits, ofUInt32_bits, UInt32.toNat_ofNat']
  exact Nat.mod_eq_of_lt w.toBitVec.isLt

theorem ofW_toWord' (z : Model Fmt) : ofW (toWord z) = z := by
  have h : z.toNatBits < 2 ^ 32 := Model.toNatBits_lt_two_pow z
  simp only [ofW, toWord, Model.ofUInt32, UInt32.toNat_ofNat', Nat.mod_eq_of_lt h]
  exact Model.ofNatBits_toNatBits z

/-! ### Rounding a word by bit operations -/

def p23 : Nat := 2 ^ 23
def p24 : Nat := 2 ^ 24

/-- The binary32 word of `±m · 2^e` rounded to nearest even: by bit operations when the result is
a normal number below `2^127`, otherwise through FloatLib. -/
@[inline] def roundW (neg : Bool) (m : Nat) (e : Int) : UInt32 :=
  let L := m.log2
  let T : Int := L + e
  let r := roundShiftRightEven m (L - 23)
  if 23 ≤ L ∧ -126 ≤ T ∧ T ≤ 126 ∧ p23 ≤ r ∧ r ≤ p24 then
    let E : Nat := if r = p24 then (T + 128).toNat else (T + 127).toNat
    let F : Nat := if r = p24 then 0 else r - p23
    (if neg then (0x80000000 : UInt32) else 0) + (E.toUInt32 <<< 23) + F.toUInt32
  else toWord (Model.roundDyadic Fmt ⟨neg, m, e⟩)

/-- A word assembled from fields below their widths has those fields. -/
theorem fields_of_word (s : Bool) (E F : Nat) (hE : E < 256) (hF : F < 2 ^ 23) :
    let w : UInt32 := (if s then (0x80000000 : UInt32) else 0) + (E.toUInt32 <<< 23) + F.toUInt32
    signOf w = s ∧ (expOf w).toNat = E ∧ (fracOf w).toNat = F := by
  intro w
  have hw : w.toNat = (if s then 2 ^ 31 else 0) + E * 2 ^ 23 + F := by
    simp only [w, UInt32.toNat_add, UInt32.toNat_shiftLeft, Nat.toUInt32, UInt32.toNat_ofNat']
    have h23 : (23 : UInt32).toNat % 32 = 23 := by decide
    rw [h23, Nat.shiftLeft_eq]
    cases s <;> simp <;> omega
  refine ⟨?_, ?_, ?_⟩
  · unfold signOf
    have hbit : (w &&& 0x80000000).toNat = (if s then 2 ^ 31 else 0) := by
      rw [UInt32.toNat_and]
      have : (0x80000000 : UInt32).toNat = 2 ^ 31 := by decide
      rw [this, Nat.and_two_pow, Nat.testBit_eq_decide_div_mod_eq, hw]
      cases s <;> simp <;> omega
    have hz : ∀ x : UInt32, (x != 0) = decide (x.toNat ≠ 0) := by
      intro x
      have : x.toNat ≠ 0 ↔ x ≠ 0 := by
        rw [ne_eq, ne_eq, ← UInt32.toNat_inj]; simp
      simp [this]
      rfl
    rw [hz, hbit]
    cases s <;> simp
  · unfold expOf
    rw [UInt32.toNat_and, UInt32.toNat_shiftRight]
    have h23 : (23 : UInt32).toNat % 32 = 23 := by decide
    have hff : (0xFF : UInt32).toNat = 2 ^ 8 - 1 := by decide
    rw [h23, hff, Nat.and_two_pow_sub_one_eq_mod, Nat.shiftRight_eq_div_pow, hw]
    cases s <;> simp <;> omega
  · unfold fracOf
    rw [UInt32.toNat_and]
    have hm : (0x7FFFFF : UInt32).toNat = 2 ^ 23 - 1 := by decide
    rw [hm, Nat.and_two_pow_sub_one_eq_mod, hw]
    cases s <;> simp <;> omega

/-- FloatLib's rounding in the normal range, as fields. -/
theorem roundDyadic_normal (neg : Bool) (m : Nat) (e : Int) (hL : 23 ≤ m.log2)
    (hT1 : -126 ≤ (m.log2 : Int) + e) (hT2 : (m.log2 : Int) + e ≤ 126)
    (hr1 : 2 ^ 23 ≤ roundShiftRightEven m (m.log2 - 23))
    (hr2 : roundShiftRightEven m (m.log2 - 23) ≤ 2 ^ 24) :
    Model.roundDyadic Fmt ⟨neg, m, e⟩ =
      if roundShiftRightEven m (m.log2 - 23) = 2 ^ 24 then
        Model.ofFields Fmt neg ((m.log2 : Int) + e + 128).toNat 0
      else Model.ofFields Fmt neg ((m.log2 : Int) + e + 127).toNat
        (roundShiftRightEven m (m.log2 - 23) - 2 ^ 23) := by
  have hm0 : m ≠ 0 := by rintro rfl; simp at hL
  rw [← Model.roundDyadicGeneral_eq_roundDyadic]
  unfold Model.roundDyadicGeneral Model.packRoundedNormal
  have hp : ∀ k, Model.pow2 k = 2 ^ k := fun k => by simp [Model.pow2, Nat.shiftLeft_eq]
  have hfw : Fmt.fracWidth = 23 := rfl
  have hmax : Fmt.maxNormalExponent = 127 := rfl
  have hmin : Fmt.minNormalExponent = -126 := rfl
  have hbias : Fmt.exponentBias = 127 := rfl
  have hmef : Fmt.maxFiniteExpField = 254 := rfl
  have hmff : Fmt.maxFiniteFracField = 2 ^ 23 - 1 := rfl
  simp only [hfw, hmax, hmin, hbias, hmef, hmff, hp, beq_iff_eq, hm0, ite_false]
  have h1 : ¬ ((m.log2 : Int) + e > 127) := by omega
  have h2 : ¬ ((m.log2 : Int) + e < -126) := by omega
  simp only [h1, h2, hL, ite_false, ite_true]
  generalize roundShiftRightEven m (m.log2 - 23) = r at *
  by_cases hc : r = 2 ^ 24
  · subst hc
    have h3 : ¬ ((m.log2 : Int) + e + 1 > 127) := by omega
    have h4 : ¬ (((m.log2 : Int) + e + 1 + Int.ofNat 127).toNat > 254 ∨
        ((m.log2 : Int) + e + 1 + Int.ofNat 127).toNat = 254 ∧ 2 ^ 23 - 2 ^ 23 > 2 ^ 23 - 1) := by
      simp; omega
    simp only [ite_true, h3, ite_false]
    simp only [Bool.or_eq_true, decide_eq_true_eq, Bool.and_eq_true, beq_iff_eq, h4, ite_false]
    congr 1
    have : (Int.ofNat 127 : Int) = 127 := rfl
    omega
  · have h3 : ¬ ((m.log2 : Int) + e > 127) := by omega
    have h4 : ¬ (((m.log2 : Int) + e + Int.ofNat 127).toNat > 254 ∨
        ((m.log2 : Int) + e + Int.ofNat 127).toNat = 254 ∧ r - 2 ^ 23 > 2 ^ 23 - 1) := by
      simp; omega
    simp only [hc, ite_false, h3]
    simp only [Bool.or_eq_true, decide_eq_true_eq, Bool.and_eq_true, beq_iff_eq, h4, ite_false]
    congr 1

theorem roundW_eq (neg : Bool) (m : Nat) (e : Int) :
    roundW neg m e = toWord (Model.roundDyadic Fmt ⟨neg, m, e⟩) := by
  unfold roundW
  dsimp only
  split
  · rename_i h
    obtain ⟨hL, hT1, hT2, hr1, hr2⟩ := h
    simp only [p23, p24] at hr1 hr2 ⊢
    rw [roundDyadic_normal neg m e hL hT1 hT2 hr1 hr2]
    generalize roundShiftRightEven m (m.log2 - 23) = r at *
    have key : ∀ E F : Nat, E < 256 → F < 2 ^ 23 →
        (if neg then (0x80000000 : UInt32) else 0) + (E.toUInt32 <<< 23) + F.toUInt32 =
          toWord (Model.ofFields Fmt neg E F) := by
      intro E F hE hF
      obtain ⟨hs, he, hf⟩ := fields_of_word neg E F hE hF
      have hw := ofUInt32_eq_ofFields
        ((if neg then (0x80000000 : UInt32) else 0) + (E.toUInt32 <<< 23) + F.toUInt32)
      rw [hs, he, hf] at hw
      rw [← hw]
      exact (toWord_ofW' _).symm
    by_cases hc : r = 2 ^ 24
    · simp only [hc, ite_true]; exact key _ _ (by omega) (by norm_num)
    · simp only [hc, ite_false]; exact key _ _ (by omega) (by omega)
  · rfl

/-- `roundW` rounds like `finishWord`. -/
theorem roundW_signed (neg : Bool) (a : Nat) (e : Int) (ha : a ≠ 0) :
    roundW neg a e = finishWord (if neg then -(a : Int) else a) e false := by
  rw [roundW_eq]
  have hpos : (0 : Int) < a := by exact_mod_cast Nat.pos_of_ne_zero ha
  cases neg <;> simp [finishWord, ofIntDyadic, ha, not_lt.mpr hpos.le, Nat.pos_of_ne_zero ha]

/-- The word both ends of `±[a, b] · 2^(−62)` round to, when they agree on a finite nonzero
word (`settleInt`, by bit operations). -/

theorem toReal_of_finishWord {S e : Int} {a : UInt32} (hS : S ≠ 0)
    (ha : finishWord S e false = a) (hfin : expOf a ≠ 255) :
    Model.toReal (ofW a) = Model.roundAt Fmt ((S : ℝ) * 2 ^ e) := by
  have hw : ofW a = Model.roundDyadic Fmt (ofIntDyadic S e) := by
    rw [← ha]; simp only [finishWord, hS, ite_false]; exact ofW_toWord' _
  have hf : Model.isFinite (ofW a) = true := by
    simp [ofW, isFinite_ofUInt32, hfin]
  rw [hw] at hf ⊢
  rw [Model.toReal_roundDyadic_eq_roundAt Fmt (by decide) _ hf]
  congr 1
  rw [← FloatLib.Numerics.Dyadic.cast_toRat, toRat_ofIntDyadic]
  push_cast; rfl

/-- If both ends of `[lo, hi] · 2^e` round to the same finite nonzero word, every value between
them rounds to that word: a model `z` carrying that rounding is the word. -/
theorem bracket_spec {lo hi e : Int} {a : UInt32} (hlo : lo ≠ 0) (hhi : hi ≠ 0)
    (h1 : finishWord lo e false = a) (h2 : finishWord hi e false = a) (hfin : expOf a ≠ 255)
    (hsig : (decodeD a).significand ≠ 0) {v : ℝ}
    (hl : (lo : ℝ) * 2 ^ e ≤ v) (hh : v ≤ (hi : ℝ) * 2 ^ e) {z : Model Fmt}
    (hz : Model.toReal z = Model.roundAt Fmt v) : toWord z = a := by
  have r1 := toReal_of_finishWord hlo h1 hfin
  have r2 := toReal_of_finishWord hhi h2 hfin
  have hv : Model.roundAt Fmt v = Model.toReal (ofW a) := by
    apply le_antisymm
    · rw [r2]; exact Model.roundAt_mono Fmt hh
    · rw [r1]; exact Model.roundAt_mono Fmt hl
  have hnz : Model.toReal (ofW a) ≠ 0 := by
    rw [Model.toReal_eq, ofW, toDyadic_ofUInt32 _ hfin]
    simp only
    rw [← FloatLib.Numerics.Dyadic.cast_toRat]
    exact_mod_cast (mt (FloatLib.Numerics.Dyadic.toRat_eq_zero_iff _).mp hsig)
  have := Model.eq_of_toReal_eq_of_nonzero (hz.trans hv) (by rwa [hz, hv])
  rw [this, toWord_ofW']


end ExactRuntime.FastRound
