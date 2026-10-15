import ExactToolkit.Congr

/-!
# From raw binary32 bits to FloatLib's exact values

A kernel reads each input as a raw `UInt32`. This file proves what that word is worth in
FloatLib's terms, so kernels can decode with a few shifts and masks instead of FloatLib's generic
decoder.
-/

namespace ExactRuntime

open FloatLib.Floats.Formats.BinaryInterchange FloatLib.Floats

/-- The competition's binary32 format descriptor. -/
abbrev Fmt := ExecFloat.Binary.format (exponentBits := 8) (fractionBits := 23)

/-- Sign, biased exponent and fraction fields of a raw word. -/
@[inline] def signOf (w : UInt32) : Bool := (w &&& 0x80000000) != 0
@[inline] def expOf (w : UInt32) : UInt32 := (w >>> 23) &&& 0xFF
@[inline] def fracOf (w : UInt32) : UInt32 := w &&& 0x7FFFFF

example : Fmt.isIEEE = true := by decide

theorem ofUInt32_bits (w : UInt32) : (Model.ofUInt32 w : Model Fmt).bits = w.toBitVec := by
  show BitVec.ofNat 32 w.toNat = w.toBitVec
  rw [show w.toNat = w.toBitVec.toNat from rfl, BitVec.ofNat_toNat, BitVec.setWidth_eq]

/-- A raw word is the float built from its own fields. -/
theorem ofUInt32_eq_ofFields (w : UInt32) :
    (Model.ofUInt32 w : Model Fmt) =
      Model.ofFields Fmt (signOf w) (expOf w).toNat (fracOf w).toNat := by
  conv_lhs => rw [← Model.ofFields_signBit_expField_fracField (Model.ofUInt32 w : Model Fmt)]
  simp only [Model.signBit, Model.expField, Model.fracField, ofUInt32_bits]
  congr 1
  unfold signOf
  apply Bool.eq_iff_iff.mpr
  simp only [bne_iff_ne, ne_eq]
  rw [← UInt32.toBitVec_inj]
  exact Iff.rfl

end ExactRuntime

namespace ExactRuntime

open FloatLib.Floats.Formats.BinaryInterchange FloatLib.Floats

theorem expOf_toNat_lt (w : UInt32) : (expOf w).toNat < 256 := by
  have : (expOf w).toNat ≤ 255 := by
    simp only [expOf, UInt32.toNat_and]
    exact Nat.and_le_right
  omega

theorem fracOf_toNat_lt (w : UInt32) : (fracOf w).toNat < 2 ^ 23 := by
  have : (fracOf w).toNat ≤ 0x7FFFFF := by
    simp only [fracOf, UInt32.toNat_and]
    exact Nat.and_le_right
  omega

/-- The exact value of a finite word, read straight from its fields. -/
def decodeD (w : UInt32) : FloatLib.Numerics.Dyadic :=
  if expOf w = 0 then
    if fracOf w = 0 then ⟨signOf w, 0, 0⟩ else ⟨signOf w, (fracOf w).toNat, -149⟩
  else ⟨signOf w, 2 ^ 23 + (fracOf w).toNat, ((expOf w).toNat : Int) - 150⟩

theorem isFinite_ofUInt32 (w : UInt32) :
    Model.isFinite (Model.ofUInt32 w : Model Fmt) = (expOf w != 255) := by
  rw [ofUInt32_eq_ofFields,
    Model.isFinite_ofFields Fmt _ _ _ (expOf_toNat_lt w) (fracOf_toNat_lt w)]
  have : (expOf w).toNat = 255 ↔ expOf w = 255 := by
    constructor
    · intro h; exact UInt32.toNat_inj.mp (by simpa using h)
    · intro h; simp [h]
  simp only [show Fmt.encoding = .ieee from rfl, show Fmt.expAllOnesNat = 255 from rfl]
  rw [Bool.eq_iff_iff]
  simp only [bne_iff_ne, ne_eq, this]

theorem toDyadic_ofUInt32 (w : UInt32) (h : expOf w ≠ 255) :
    Model.toDyadic? (Model.ofUInt32 w : Model Fmt) = some (decodeD w) := by
  have hfin : Model.isFinite (Model.ofUInt32 w : Model Fmt) = true := by
    simp [isFinite_ofUInt32, h]
  rw [ofUInt32_eq_ofFields] at hfin ⊢
  rw [Model.toDyadic?_ofFields_of_isFinite Fmt _ _ _ (expOf_toNat_lt w) (fracOf_toNat_lt w) hfin]
  have he0 : (expOf w).toNat = 0 ↔ expOf w = 0 := by
    constructor
    · intro h; exact UInt32.toNat_inj.mp (by simpa using h)
    · intro h; simp [h]
  have hf0 : (fracOf w).toNat = 0 ↔ fracOf w = 0 := by
    constructor
    · intro h; exact UInt32.toNat_inj.mp (by simpa using h)
    · intro h; simp [h]
  simp only [decodeD, he0, hf0, show Fmt.minSubnormalExponent = -149 from rfl,
    show Fmt.exponentBias = 127 from rfl,
    show Fmt.fracWidth = 23 from rfl]
  simp only [show Model.pow2 23 = 2 ^ 23 from rfl]
  split_ifs <;> simp <;> omega

end ExactRuntime
