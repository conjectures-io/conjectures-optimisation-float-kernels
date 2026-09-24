import Contract

/-!
FloatLib's certified binary32 word kernel called on the raw bits, skipping the carrier codec.

`ExecFloat.add` decodes both operands into the generic model, dispatches to the word kernel
`addFiniteImpl?`, and encodes the result back. This kernel calls `addFiniteImpl?` on the stored
`UInt32` bits directly and falls back to `ExecFloat.add` only when it declines (NaN or infinity).
The proof shows it equals the planner's `wordAdd` through the codec, then reuses `wordAdd_eq_spec`.
-/

namespace Submission

open FloatLib.Floats Competition
open FloatLib.Floats.Formats.BinaryInterchange

/-- The binary32 descriptor, storage plan and family that `Binary32` unfolds to. -/
abbrev Fmt := ExecFloat.Binary.format (exponentBits := 8) (fractionBits := 23)
abbrev Plan := Configured.StoragePlan.forKnownWidth Fmt (1 + 8 + 23) (by rfl)
abbrev Fam := Configured.Family Fmt (Configured.Code Plan) Plan

/-- The word kernel on raw bits; `none` (NaN or infinity) goes to the certified kernel. -/
def add (x y : Binary32) : Binary32 :=
  match Model.NativeBinary32.addFiniteImpl? (Model.NativeBinary32.ofUInt32 x.raw.1)
      (Model.NativeBinary32.ofUInt32 y.raw.1) with
  | some r => Configured.NativeFPU.ofBinary32Bits (Model.NativeBinary32.toUInt32 r)
  | none => ExecFloat.add x y

/-- The carrier's decode, with its codec instance pinned. -/
def dec (z : Binary32) : Model Fmt :=
  ExecFloat.ModelCodec.decode (F := Fam) (plan := Plan) (Model := Model Fmt) z

/-- Decoding a stored value is reading its bits. -/
theorem dec_eq (z : Binary32) : dec z = Model.NativeBinary32.ofUInt32 z.raw.1 := rfl

/-- On binary32 the dispatcher is the word kernel with the generic fallback. -/
theorem word32 (a b : Model Fmt) :
    Model.AddBackend.word a b =
      match Model.NativeBinary32.addFiniteImpl? a b with
      | some s => s
      | none => Model.AddBackend.generic a b := by
  unfold Model.AddBackend.word
  rw [dif_pos (by decide)]
  rfl

/-- The planner's `wordAdd` decodes to the dispatcher on the decoded operands. -/
theorem dec_word (x y : Binary32) :
    dec (Configured.Backend.wordAdd x y) = Model.AddBackend.word (dec x) (dec y) :=
  ExecFloat.ModelCodec.decode_liftBinary (F := Fam) (plan := Plan) (Model := Model Fmt) _ x y

/-- Reading a model's bits and rewrapping them gives the model back. -/
theorem ofUInt32_toUInt32 (r : Model.NativeBinary32.Value) :
    Model.NativeBinary32.ofUInt32 (Model.NativeBinary32.toUInt32 r) = r := by
  cases r
  simp [Model.NativeBinary32.ofUInt32, Model.NativeBinary32.toUInt32, Model.ofBits]

/-- The kernel equals the planner's word addition on every pair of operands. -/
theorem add_eq_word (x y : Binary32) : add x y = Configured.Backend.wordAdd x y := by
  apply ExecFloat.ModelCodec.decode_injective (F := Fam) (plan := Plan) (Model := Model Fmt)
  change dec (add x y) = dec (Configured.Backend.wordAdd x y)
  rw [dec_word, word32, dec_eq x, dec_eq y]
  unfold add
  split
  · rename_i r h
    change Model.NativeBinary32.ofUInt32 (Model.NativeBinary32.toUInt32 r) = _
    rw [h]
    exact ofUInt32_toUInt32 r
  · rename_i h
    rw [show ExecFloat.add x y = Configured.Backend.wordAdd x y from rfl, dec_word, word32,
      dec_eq x, dec_eq y, h]

theorem add_spec : ∀ x y : Binary32, add x y = ExecFloat.Spec.add x y := fun x y => by
  rw [add_eq_word]
  exact Configured.Backend.wordAdd_eq_spec x y

end Submission
