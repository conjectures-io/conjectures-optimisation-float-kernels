import Contract

/-!
# The benchmark core, shared by the incumbent and the submission binaries

Each kernel is timed in its own executable, so nothing a submission compiles (a `csimp` lemma,
an `initialize` block) can reach the incumbent. One process prints one line per operand class:
`name timed best_ns checksum`. Operands come from `BENCH_SEED`, chosen per round by the gate.
-/

namespace Bench

open FloatLib.Floats Competition
open FloatLib.Floats.Formats.BinaryInterchange

/-- Deterministic words from a 32-bit LCG. -/
def lcg (n : Nat) (seed : UInt32) : Array UInt32 := Id.run do
  let mut a : Array UInt32 := Array.mkEmpty n
  let mut s := seed
  for _ in [0:n] do
    s := s * 1664525 + 1013904223
    a := a.push s
  return a

/-- Random sign and fraction, exponent within three of one: where real additions live. -/
def nearOne (_ : Nat) (w : UInt32) : UInt32 :=
  (w &&& (0x807FFFFF : UInt32)) ||| (((124 : UInt32) + (w >>> 23) % 7) <<< 23)

/-- Even slots near one, odd slots 20 to 30 binades lower: the sticky-bit and early-return edge. -/
def wideGap (i : Nat) (w : UInt32) : UInt32 :=
  let e : UInt32 := if i % 2 == 0 then 127 else (107 : UInt32) - (w >>> 23) % 11
  (w &&& (0x807FFFFF : UInt32)) ||| (e <<< 23)

/-- Same exponent, alternating signs, fractions close: cancellation and exact ties. -/
def cancel (i : Nat) (w : UInt32) : UInt32 :=
  let frac : UInt32 :=
    if i % 2 == 0 then w &&& (0x7FFFFF : UInt32) else (w &&& (0x7FFFF8 : UInt32)) ||| ((w >>> 27) &&& 7)
  let sign : UInt32 := if i % 2 == 0 then 0 else 0x80000000
  sign ||| ((127 : UInt32) <<< 23) ||| frac

/-- Raw bit patterns: NaNs, infinities, subnormals and zeros; checked, not timed. -/
def raw (_ : Nat) (w : UInt32) : UInt32 := w

/-- One operand class: name, generator, whether it is timed, and a salt for its LCG seed. -/
abbrev Classes := List (String × (Nat → UInt32 → UInt32) × Bool × UInt32)

/-- The addition slot's classes. -/
def addClasses : Classes :=
  [("near-one", nearOne, true, 0x1), ("wide-gap", wideGap, true, 0x2),
   ("cancel", cancel, true, 0x3), ("raw", raw, false, 0x4)]

/-- Both factors within three binades of one: products stay normal, as most real ones do. -/
def mulNearOne (_ : Nat) (w : UInt32) : UInt32 :=
  (w &&& (0x807FFFFF : UInt32)) ||| (((124 : UInt32) + (w >>> 23) % 7) <<< 23)

/-- Exponents across the whole normal range: products overflow, underflow and everything between. -/
def mulSpread (_ : Nat) (w : UInt32) : UInt32 :=
  (w &&& (0x807FFFFF : UInt32)) ||| (((1 : UInt32) + (w >>> 23) % 254) <<< 23)

/-- Pairs whose product lands in the subnormal range: the denormalising shift and its rounding. -/
def mulTiny (i : Nat) (w : UInt32) : UInt32 :=
  let e : UInt32 :=
    if i % 2 == 0 then (55 : UInt32) + (w >>> 23) % 11 else (60 : UInt32) - (w >>> 23) % 11
  (w &&& (0x807FFFFF : UInt32)) ||| (e <<< 23)

/-- Fused multiply-add operands come in triples, slot `i % 3`: 0 and 1 the factors, 2 the addend. -/
def role (i : Nat) : Nat := i % 3

/-- All three near one: the accumulate step of a dot product. -/
def fmaNearOne (_ : Nat) (w : UInt32) : UInt32 :=
  (w &&& (0x807FFFFF : UInt32)) ||| (((124 : UInt32) + (w >>> 23) % 7) <<< 23)

/-- Factors near one, a negative addend of the product's size: cancellation, where fma matters. -/
def fmaCancel (i : Nat) (w : UInt32) : UInt32 :=
  if role i == 2 then
    (0x80000000 : UInt32) ||| (w &&& (0x7FFFFF : UInt32)) ||| (((127 : UInt32) + (w >>> 31)) <<< 23)
  else (w &&& (0x7FFFFF : UInt32)) ||| ((127 : UInt32) <<< 23)

/-- An addend 20 to 40 binades from the product, either side: alignment shifts and sticky bits. -/
def fmaWide (i : Nat) (w : UInt32) : UInt32 :=
  let e : UInt32 :=
    if role i == 2 then
      (if w &&& 1 == 0 then (87 : UInt32) + (w >>> 23) % 21 else (147 : UInt32) + (w >>> 23) % 21)
    else 127
  (w &&& (0x807FFFFF : UInt32)) ||| (e <<< 23)

/-- The fused multiply-add slot's classes. -/
def fmaClasses : Classes :=
  [("near-one", fmaNearOne, true, 0x21), ("cancel", fmaCancel, true, 0x22),
   ("wide", fmaWide, true, 0x23), ("raw", raw, false, 0x24)]

/-- The multiplication slot's classes. -/
def mulClasses : Classes :=
  [("near-one", mulNearOne, true, 0x11), ("spread", mulSpread, true, 0x12),
   ("tiny", mulTiny, true, 0x13), ("raw", raw, false, 0x14)]

/-- One pass of `kernel` over every consecutive pair, in `IO` so reps cannot be shared; the
checksum folds every result so no call can be dropped. -/
def sweep (kernel : Binary32 → Binary32 → Binary32) (xs : Array UInt32) (seed : UInt32) :
    IO UInt32 := do
  let mut sink : UInt32 := seed
  for i in [0:xs.size - 1] do
    let r := kernel (Configured.NativeFPU.ofBinary32Bits xs[i]!)
      (Configured.NativeFPU.ofBinary32Bits xs[i + 1]!)
    sink := (sink ^^^ r.raw.1) * 16777619
  return sink

/-- The fastest of `reps` timed sweeps, in nanoseconds. -/
def best (kernel : Binary32 → Binary32 → Binary32) (xs : Array UInt32) (reps : Nat) : IO Nat := do
  let mut b := 0
  for r in [0:reps] do
    let t0 ← IO.monoNanosNow
    let s ← sweep kernel xs (UInt32.ofNat (r + 1))
    let t1 ← IO.monoNanosNow
    if s == 0xFFFFFFFF then IO.println "unlikely"   -- keeps `s` observed
    b := if b == 0 || t1 - t0 < b then t1 - t0 else b
  return b

/-- `sweep` for a three-operand kernel, over consecutive disjoint triples. -/
def sweep3 (kernel : Binary32 → Binary32 → Binary32 → Binary32) (xs : Array UInt32)
    (seed : UInt32) : IO UInt32 := do
  let mut sink : UInt32 := seed
  for t in [0:xs.size / 3] do
    let r := kernel (Configured.NativeFPU.ofBinary32Bits xs[3 * t]!)
      (Configured.NativeFPU.ofBinary32Bits xs[3 * t + 1]!)
      (Configured.NativeFPU.ofBinary32Bits xs[3 * t + 2]!)
    sink := (sink ^^^ r.raw.1) * 16777619
  return sink

/-- `best` for a three-operand kernel. -/
def best3 (kernel : Binary32 → Binary32 → Binary32 → Binary32) (xs : Array UInt32)
    (reps : Nat) : IO Nat := do
  let mut b := 0
  for r in [0:reps] do
    let t0 ← IO.monoNanosNow
    let s ← sweep3 kernel xs (UInt32.ofNat (r + 1))
    let t1 ← IO.monoNanosNow
    if s == 0xFFFFFFFF then IO.println "unlikely"   -- keeps `s` observed
    b := if b == 0 || t1 - t0 < b then t1 - t0 else b
  return b

/-- A natural from the environment, or `default`. -/
def envNat (name : String) (default : Nat) : IO Nat := do
  return ((← IO.getEnv name).bind String.toNat?).getD default

/-- One bench process: per class, a checksum sweep that doubles as warm-up, then the timing. -/
def runWith (classes : Classes) (kernel : Binary32 → Binary32 → Binary32) : IO UInt32 := do
  let pairs ← envNat "BENCH_PAIRS" 500000
  let seed := UInt32.ofNat (← envNat "BENCH_SEED" 0x9E3779B9)
  let inner ← envNat "BENCH_INNER" 3
  for (name, gen, timed, salt) in classes do
    let xs := (lcg (pairs + 1) (seed ^^^ salt * 0x9E3779B9)).mapIdx gen
    let sum ← sweep kernel xs 0
    let ns ← if timed then best kernel xs inner else pure 0
    IO.println s!"{name} {timed} {ns} {sum}"
  return 0

/-- The addition slot's bench process; this fixed shape is what its calibration was measured on. -/
def run (kernel : Binary32 → Binary32 → Binary32) : IO UInt32 := runWith addClasses kernel

/-- The multiplication slot's bench process, in the same shape. -/
def runMul (kernel : Binary32 → Binary32 → Binary32) : IO UInt32 := runWith mulClasses kernel

/-- `runWith` for a three-operand kernel: `BENCH_PAIRS` calls per class, as for the others. -/
def runWith3 (classes : Classes) (kernel : Binary32 → Binary32 → Binary32 → Binary32) :
    IO UInt32 := do
  let calls ← envNat "BENCH_PAIRS" 500000
  let seed := UInt32.ofNat (← envNat "BENCH_SEED" 0x9E3779B9)
  let inner ← envNat "BENCH_INNER" 3
  for (name, gen, timed, salt) in classes do
    let xs := (lcg (3 * calls) (seed ^^^ salt * 0x9E3779B9)).mapIdx gen
    let sum ← sweep3 kernel xs 0
    let ns ← if timed then best3 kernel xs inner else pure 0
    IO.println s!"{name} {timed} {ns} {sum}"
  return 0

/-- The fused multiply-add slot's bench process, in the same shape. -/
def runFma (kernel : Binary32 → Binary32 → Binary32 → Binary32) : IO UInt32 :=
  runWith3 fmaClasses kernel

end Bench
