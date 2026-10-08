import Stage2.Kernels

/-!
# Fusion headroom probe (unproved)

How much a tensor kernel gains beyond composing stage 1's scalar winners, keeping the spec's
order and per-step rounding (so results stay bit-identical):

* `wordSum` / `wordDot`: raw `UInt32` loops calling the winners' word cores directly, the certified
  kernel only for special operands. Measures the `Binary32` wrapper and dispatch overhead.
* `fusedSum` / `fusedDot`: rounding returns fields instead of a packed word; the product flows
  into the add, and the accumulator stays unpacked across iterations.

`Fields` tag 0 is a finite nonzero value in `(sign, exponent field, full mantissa)`; any other tag
carries the packed bits in `mant` and takes the certified slow path.
-/

namespace Fused

open FloatLib.Floats FloatLib.Floats.Formats.BinaryInterchange Model.NativeBinary32
open Stage2.Kernels (Binary32 zero)

/-- Fields in one word, so nothing is allocated per step: bit 63 sign, bits 32..39 the exponent
field, the low 25 bits the full mantissa; bit 48 set means "packed bits in the low 32". -/
abbrev Fields := UInt64

@[inline] def Fields.tag (f : Fields) : UInt8 := if f &&& 0x1000000000000 == 0 then 0 else 1
@[inline] def Fields.sign (f : Fields) : Bool := f >>> 63 == 1
@[inline] def Fields.exp (f : Fields) : UInt32 := ((f >>> 32) &&& 0xff).toUInt32
@[inline] def Fields.mant (f : Fields) : UInt64 := f &&& 0x1ffffff
@[inline] def mk (sign : Bool) (e : UInt32) (m : UInt64) : Fields :=
  (if sign then (0x8000000000000000 : UInt64) else 0) ||| (e.toUInt64 <<< 32) ||| m

@[inline] def packed (w : UInt32) : Fields := (0x1000000000000 : UInt64) ||| w.toUInt64

/-- `roundP` from the winners, returning fields. -/
@[inline] def roundF (sign : Bool) (product scale : UInt64) : Fields :=
  if product == 0 then packed (if sign then 0x80000000 else 0)
  else
    let leading := TunedAdd.fastLog2 product
    let position := leading + scale
    if position < 172 then
      let fraction :=
        if scale < 149 then TunedAdd.rseAny product (149 - scale) else product <<< (scale - 149)
      if fraction == 0 then packed (if sign then 0x80000000 else 0)
      else if fraction == 0x800000 then mk sign 1 0x800000
      else mk sign 0 fraction
    else
      let rm := if leading < 23 then product <<< (23 - leading)
        else TunedAdd.rseAny product (leading - 23)
      let carry := rm == 0x1000000
      let np := if carry then position + 1 else position
      if np > 425 then packed (if sign then 0xff800000 else 0x7f800000)
      else mk sign (np - 171).toUInt32 (if carry then 0x800000 else rm)

@[inline] def pack (f : Fields) : UInt32 :=
  if f.tag == 0 then TunedAdd.mkW f.sign f.exp (f.mant &&& 0x7fffff).toUInt32 else f.toUInt32

/-- Bits to fields: finite nonzero values decode, NaN, infinity and zero stay packed. -/
@[inline] def unpack (w : UInt32) : Fields :=
  if TunedAdd.sp1 w then packed w
  else mk (signBit w) (expField w) (finiteMantissa (expField w) (fracField w))

/-- `TunedAdd.core` on fields. -/
@[inline] def addF (x y : Fields) : Fields :=
  let xScale := finiteScale x.exp
  let yScale := finiteScale y.exp
  if xScale ≤ yScale then
    let shift := yScale - xScale
    if shift ≤ 39 then
      let s := FloatLib.Numerics.FixedWord.addSignedMagnitudes x.sign y.sign x.mant (y.mant <<< shift)
      roundF s.1 s.2 (xScale + 149)
    else y
  else
    let shift := xScale - yScale
    if shift ≤ 39 then
      let s := FloatLib.Numerics.FixedWord.addSignedMagnitudes x.sign y.sign (x.mant <<< shift) y.mant
      roundF s.1 s.2 (yScale + 149)
    else x

/-- `FastMul.core` on bits, returning fields. -/
@[inline] def mulF (a b : UInt32) : Fields :=
  let ea := expField a
  let eb := expField b
  roundF (Bool.xor (signBit a) (signBit b))
    (finiteMantissa ea (fracField a) * finiteMantissa eb (fracField b))
    (finiteScale ea + finiteScale eb)

@[inline] def ofBits (w : UInt32) : Binary32 := Configured.NativeFPU.ofBinary32Bits w

/-- The certified step, for anything the fast path does not cover. -/
@[inline] def slowAdd (acc y : Fields) : Fields :=
  unpack (TunedAdd.add (ofBits (pack acc)) (ofBits (pack y))).raw.1

def fusedSum (xs : Array UInt32) : UInt32 := Id.run do
  let mut acc : Fields := packed 0
  for w in xs do
    let y := unpack w
    acc := if acc.tag == 0 && y.tag == 0 then addF acc y else slowAdd acc y
  return pack acc

def fusedDot (as bs : Array UInt32) : UInt32 := Id.run do
  let mut acc : Fields := packed 0
  for k in [0:as.size] do
    let a := as[k]!
    let b := bs[k]!
    let p := if FastMul.special a b then packed (FastMul.mul (ofBits a) (ofBits b)).raw.1
      else mulF a b
    acc := if acc.tag == 0 && p.tag == 0 then addF acc p else slowAdd acc p
  return pack acc

def wordSum (xs : Array UInt32) : UInt32 := Id.run do
  let mut acc : UInt32 := 0
  for w in xs do
    acc := if TunedAdd.special acc w then (TunedAdd.add (ofBits acc) (ofBits w)).raw.1
      else TunedAdd.core acc w
  return acc

def wordDot (as bs : Array UInt32) : UInt32 := Id.run do
  let mut acc : UInt32 := 0
  for k in [0:as.size] do
    let a := as[k]!
    let b := bs[k]!
    let p := if FastMul.special a b then (FastMul.mul (ofBits a) (ofBits b)).raw.1
      else FastMul.core a b
    acc := if TunedAdd.special acc p then (TunedAdd.add (ofBits acc) (ofBits p)).raw.1
      else TunedAdd.core acc p
  return acc

/-- The proved composition, for the baseline: stage 1's winners over `Vector Binary32`. -/
def dotFast {n : Nat} (a b : Vector Binary32 n) : Binary32 := Id.run do
  let mut acc := zero
  for k in [0:n] do
    acc := TunedAdd.add acc (FastMul.mul (a.toArray.getD k zero) (b.toArray.getD k zero))
  return acc

def dotRef {n : Nat} (a b : Vector Binary32 n) : Binary32 := Id.run do
  let mut acc := zero
  for k in [0:n] do
    acc := ExecFloat.add acc (ExecFloat.mul (a.toArray.getD k zero) (b.toArray.getD k zero))
  return acc

/-- One word-level `add acc (mul a b)` step, certified kernels on special operands. -/
@[inline] def step (acc a b : UInt32) : UInt32 :=
  let p := if FastMul.special a b then (FastMul.mul (ofBits a) (ofBits b)).raw.1 else FastMul.core a b
  if TunedAdd.special acc p then (TunedAdd.add (ofBits acc) (ofBits p)).raw.1 else TunedAdd.core acc p

/-- Word-level matmul, one output at a time: each `C[i,j]` a left fold over `k` from +0. -/
def mm1 (s : Nat) (A B : Array UInt32) : Array UInt32 := Id.run do
  let mut C : Array UInt32 := Array.mkEmpty (s * s)
  for i in [0:s] do
    for j in [0:s] do
      let mut acc : UInt32 := 0
      for k in [0:s] do
        acc := step acc A[i * s + k]! B[k * s + j]!
      C := C.push acc
  return C

/-- The same outputs, four columns at a time: four independent chains, each in the spec's order. -/
def mm4 (s : Nat) (A B : Array UInt32) : Array UInt32 := Id.run do
  let mut C : Array UInt32 := Array.mkEmpty (s * s)
  for i in [0:s] do
    for jb in [0:s / 4] do
      let j := 4 * jb
      let mut c0 : UInt32 := 0
      let mut c1 : UInt32 := 0
      let mut c2 : UInt32 := 0
      let mut c3 : UInt32 := 0
      for k in [0:s] do
        let a := A[i * s + k]!
        let r := k * s + j
        c0 := step c0 a B[r]!
        c1 := step c1 a B[r + 1]!
        c2 := step c2 a B[r + 2]!
        c3 := step c3 a B[r + 3]!
      C := ((C.push c0).push c1 |>.push c2).push c3
  return C

def hashW (xs : Array UInt32) : UInt32 := xs.foldl (fun h x => (h ^^^ x) * 16777619) 0

/-- `mm1` skipping the step when `a` is ±0, `b` is finite and `acc` is finite nonzero, since the
product is then a signed zero that leaves `acc` unchanged (unproved, so checksums must still
agree). -/
def mmSkip (s : Nat) (A B : Array UInt32) : Array UInt32 := Id.run do
  let mut C : Array UInt32 := Array.mkEmpty (s * s)
  for i in [0:s] do
    for j in [0:s] do
      let mut acc : UInt32 := 0
      for k in [0:s] do
        let a := A[i * s + k]!
        let b := B[k * s + j]!
        let finiteB := (b &&& 0x7f800000) != 0x7f800000
        let accPlain := !TunedAdd.sp1 acc
        acc := if (a <<< 1) == 0 && finiteB && accPlain then acc else step acc a b
      C := C.push acc
  return C

end Fused

open Fused Stage2.Kernels

def lcg (n : Nat) (seed : UInt32) : Array UInt32 := Id.run do
  let mut a : Array UInt32 := Array.mkEmpty n
  let mut s := seed
  for _ in [0:n] do
    s := s * 1664525 + 1013904223
    a := a.push s
  return a

/-- Near one, as the scalar bench; `raw` mixes in NaNs, infinities, zeros and subnormals. -/
def nearOne (w : UInt32) : UInt32 :=
  (w &&& (0x807FFFFF : UInt32)) ||| (((124 : UInt32) + (w >>> 23) % 7) <<< 23)

def best (reps : Nat) (f : Unit → IO UInt32) : IO (Nat × UInt32) := do
  let mut b := 0
  let mut last := 0
  for _ in [0:reps] do
    let t0 ← IO.monoNanosNow
    last ← f ()
    let t1 ← IO.monoNanosNow
    b := if b == 0 || t1 - t0 < b then t1 - t0 else b
  return (b, last)

def report (name : String) (ops base : Nat) (r : Nat × UInt32) : IO Unit := do
  let ratio := if base == 0 then "" else s!"  ratio {(r.1 * 1000 / base).toFloat / 1000}"
  IO.println s!"{name}: {r.1 / ops} ns/op  checksum {r.2}{ratio}"

def main (args : List String) : IO Unit := do
  let reps := (args.head? >>= String.toNat?).getD 7
  let n := 262144
  for (label, gen) in [("near-one", nearOne), ("raw", id)] do
    let aw := (lcg n 0x1234).map gen
    let bw := (lcg n 0x5678).map gen
    let a : Vector Binary32 n := Vector.ofFn fun i => Fused.ofBits (aw.getD i 0)
    let b : Vector Binary32 n := Vector.ofFn fun i => Fused.ofBits (bw.getD i 0)
    IO.println s!"-- {label}, n={n}"
    let r0 ← best reps fun _ => pure (sumRef a).raw.1
    report "sum ref           " n 0 r0
    let r1 ← best reps fun _ => pure (sumFast a).raw.1
    report "sum fast (proved) " n r0.1 r1
    report "sum word          " n r1.1 (← best reps fun _ => pure (wordSum aw))
    report "sum fused         " n r1.1 (← best reps fun _ => pure (fusedSum aw))
    let d0 ← best reps fun _ => pure (dotRef a b).raw.1
    report "dot ref           " n 0 d0
    let d1 ← best reps fun _ => pure (dotFast a b).raw.1
    report "dot fast (proved) " n d0.1 d1
    report "dot word          " n d1.1 (← best reps fun _ => pure (wordDot aw bw))
    report "dot fused         " n d1.1 (← best reps fun _ => pure (fusedDot aw bw))

  for sz in [64, 128] do
    let Aw := (lcg (sz * sz) 0x9abc).map nearOne
    let Bw := (lcg (sz * sz) 0xdef0).map nearOne
    let A : Vector Binary32 (1 * sz * sz) := Vector.ofFn fun i => Fused.ofBits (Aw.getD i 0)
    let B : Vector Binary32 (1 * sz * sz) := Vector.ofFn fun i => Fused.ofBits (Bw.getD i 0)
    let ops := sz * sz * sz
    IO.println s!"-- matmul {sz}³, near-one"
    let m0 ← best reps fun _ => pure ((bmmFast 1 sz sz sz A B).foldl (fun h x => (h ^^^ x.raw.1) * 16777619) 0)
    report "bmm fast (proved) " ops 0 m0
    report "mm word, 1 chain  " ops m0.1 (← best reps fun _ => pure (hashW (mm1 sz Aw Bw)))
    report "mm word, 4 chains " ops m0.1 (← best reps fun _ => pure (hashW (mm4 sz Aw Bw)))
  for sz in [64] do
    -- ReLU-like activations: half of A is +0.
    let Aw := (lcg (sz * sz) 0x9abc).map fun (w : UInt32) => if w >>> 31 == 1 then (0 : UInt32) else nearOne w
    let Bw := (lcg (sz * sz) 0xdef0).map nearOne
    let A : Vector Binary32 (1 * sz * sz) := Vector.ofFn fun i => Fused.ofBits (Aw.getD i 0)
    let B : Vector Binary32 (1 * sz * sz) := Vector.ofFn fun i => Fused.ofBits (Bw.getD i 0)
    let ops := sz * sz * sz
    IO.println s!"-- matmul {sz}³, A half zeros (post-ReLU)"
    let m0 ← best reps fun _ => pure ((bmmRef 1 sz sz sz A B).foldl (fun h x => (h ^^^ x.raw.1) * 16777619) 0)
    report "bmm ref           " ops 0 m0
    report "bmm fast (proved) " ops m0.1 (← best reps fun _ => pure ((bmmFast 1 sz sz sz A B).foldl (fun h x => (h ^^^ x.raw.1) * 16777619) 0))
    report "mm word, 1 chain  " ops m0.1 (← best reps fun _ => pure (hashW (mm1 sz Aw Bw)))
    report "mm zero-skip      " ops m0.1 (← best reps fun _ => pure (hashW (mmSkip sz Aw Bw)))
