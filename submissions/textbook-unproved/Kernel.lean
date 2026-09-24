import Contract

/-!
A textbook binary32 adder in plain words: guard, round and sticky bits, nearest-even. Normal
operands only; zeros, subnormals, infinities, NaNs, subnormal results and overflow fall back to
the certified kernel. The proof is missing: `gate.py --bench` measures it, `gate.py` rejects it.
The fallback branch and the reduction to the spec are proved; the fast-path lemma is the open task.
-/

namespace Submission

open FloatLib.Floats Competition
open FloatLib.Floats.Formats.BinaryInterchange

/-- Rounds a guard/round/sticky-extended significand to nearest-even, carrying into the exponent. -/
@[inline] def round (e : UInt32) (m : UInt64) : UInt64 × UInt32 :=
  let r := m &&& 7
  let q := m >>> 3
  let q := if r > 4 || (r == 4 && (q &&& 1) == 1) then q + 1 else q
  if q == 0x1000000 then (q >>> 1, e + 1) else (q, e)

/-- Packs sign, biased exponent and rounded significand into binary32 bits. -/
@[inline] def pack (s e : UInt32) (q : UInt64) : Binary32 :=
  Configured.NativeFPU.ofBinary32Bits ((s <<< 31) ||| (e <<< 23) ||| (q.toUInt32 &&& 0x7fffff))

/-- The adder: a fast path for normal operands, the certified kernel for everything else. -/
@[inline] def add (x y : Binary32) : Binary32 :=
  let a : UInt32 := x.raw.1
  let b : UInt32 := y.raw.1
  let ea := (a >>> 23) &&& 0xff
  let eb := (b >>> 23) &&& 0xff
  if ea == 0 || ea == 0xff || eb == 0 || eb == 0xff then ExecFloat.add x y else
  let swap := ea < eb || (ea == eb && (a &&& 0x7fffff) < (b &&& 0x7fffff))
  let (hi, lo) := if swap then (b, a) else (a, b)
  let eh := (hi >>> 23) &&& 0xff
  let el := (lo >>> 23) &&& 0xff
  let sh := hi >>> 31
  let sl := lo >>> 31
  let mh : UInt64 := (((hi &&& 0x7fffff) ||| 0x800000).toUInt64) <<< 3
  let ml0 : UInt64 := (((lo &&& 0x7fffff) ||| 0x800000).toUInt64) <<< 3
  let d := (eh - el).toUInt64
  if d > 27 then Configured.NativeFPU.ofBinary32Bits hi else
  let ml := (ml0 >>> d) ||| (if (ml0 &&& ((1 <<< d) - 1)) != 0 then 1 else 0)
  if sh == sl then
    let m := mh + ml
    let (m, e) := if m >= 0x8000000 then ((m >>> 1) ||| (m &&& 1), eh + 1) else (m, eh)
    let (q, e) := round e m
    if e >= 255 then ExecFloat.add x y else pack sh e q
  else
    let m := mh - ml
    if m == 0 then Configured.NativeFPU.ofBinary32Bits 0 else
    let lz := 26 - m.log2
    if lz.toUInt32 >= eh then ExecFloat.add x y else
    let (q, e) := round (eh - lz.toUInt32) (m <<< lz)
    pack sh e q

/-- The only lemma left to prove: on the fast path the adder agrees with the certified kernel. -/
theorem add_eq_incumbent (x y : Binary32) : add x y = ExecFloat.add x y := by
  unfold add
  dsimp only
  split
  · rfl
  · sorry

theorem add_spec : ∀ x y : Binary32, add x y = ExecFloat.Spec.add x y := fun x y => by
  rw [add_eq_incumbent, ExecFloat.Proof.add_eq_spec]

end Submission
