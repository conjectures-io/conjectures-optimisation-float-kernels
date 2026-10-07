import Bench

/-!
# The division and square-root slots' benchmark classes, beside the shared core

A module of its own, not more lines in `Bench.lean`: the add, mul and fma bench binaries are
compiled from `Bench`, and growing that module moved their hot loops enough to slow `add/mirror`
from 0.78x to 0.96x of the incumbent. `Bench.lean` stays byte for byte the calibrated one.
-/

namespace Bench

open FloatLib.Floats Competition
open FloatLib.Floats.Formats.BinaryInterchange

/-- Both operands within three binades of one: quotients stay normal, as most real ones do. -/
def divNearOne (_ : Nat) (w : UInt32) : UInt32 :=
  (w &&& (0x807FFFFF : UInt32)) ||| (((124 : UInt32) + (w >>> 23) % 7) <<< 23)

/-- Exponents across the whole normal range: quotients overflow, underflow and everything between. -/
def divSpread (_ : Nat) (w : UInt32) : UInt32 :=
  (w &&& (0x807FFFFF : UInt32)) ||| (((1 : UInt32) + (w >>> 23) % 254) <<< 23)

/-- Even slots tiny, odd slots huge: consecutive pairs divide small by large (a subnormal
quotient: the denormalising shift) and large by small (just past overflow), alternately. -/
def divExtremes (i : Nat) (w : UInt32) : UInt32 :=
  let e : UInt32 :=
    if i % 2 == 0 then (25 : UInt32) + (w >>> 23) % 11 else (160 : UInt32) + (w >>> 23) % 11
  (w &&& (0x807FFFFF : UInt32)) ||| (e <<< 23)

/-- The division slot's classes. -/
def divClasses : Classes :=
  [("near-one", divNearOne, true, 0x31), ("spread", divSpread, true, 0x32),
   ("extremes", divExtremes, true, 0x33), ("raw", raw, false, 0x34)]

/-- Positive values within three binades of one: where most square roots are taken. -/
def sqrtNearOne (_ : Nat) (w : UInt32) : UInt32 :=
  (w &&& (0x7FFFFF : UInt32)) ||| (((124 : UInt32) + (w >>> 23) % 7) <<< 23)

/-- Positive values across the whole normal range, odd and even exponents alike. -/
def sqrtSpread (_ : Nat) (w : UInt32) : UInt32 :=
  (w &&& (0x7FFFFF : UInt32)) ||| (((1 : UInt32) + (w >>> 23) % 254) <<< 23)

/-- Positive subnormals: the input normalisation a square root needs before anything else. -/
def sqrtSubnormal (_ : Nat) (w : UInt32) : UInt32 :=
  (w &&& (0x7FFFFF : UInt32)) ||| 1

/-- The square-root slot's classes; `raw` adds negatives, NaNs, infinities and zeros. -/
def sqrtClasses : Classes :=
  [("near-one", sqrtNearOne, true, 0x41), ("spread", sqrtSpread, true, 0x42),
   ("subnormal", sqrtSubnormal, true, 0x43), ("raw", raw, false, 0x44)]

/-- The division slot's bench process, in the same shape as multiplication's. -/
def runDiv (kernel : Binary32 → Binary32 → Binary32) : IO UInt32 := runWith divClasses kernel

/-- `sweep` for a one-operand kernel, over every element. -/
def sweep1 (kernel : Binary32 → Binary32) (xs : Array UInt32) (seed : UInt32) : IO UInt32 := do
  let mut sink : UInt32 := seed
  for i in [0:xs.size] do
    let r := kernel (Configured.NativeFPU.ofBinary32Bits xs[i]!)
    sink := (sink ^^^ r.raw.1) * 16777619
  return sink

/-- `best` for a one-operand kernel. -/
def best1 (kernel : Binary32 → Binary32) (xs : Array UInt32) (reps : Nat) : IO Nat := do
  let mut b := 0
  for r in [0:reps] do
    let t0 ← IO.monoNanosNow
    let s ← sweep1 kernel xs (UInt32.ofNat (r + 1))
    let t1 ← IO.monoNanosNow
    if s == 0xFFFFFFFF then IO.println "unlikely"   -- keeps `s` observed
    b := if b == 0 || t1 - t0 < b then t1 - t0 else b
  return b

/-- `runWith` for a one-operand kernel: `BENCH_PAIRS` calls per class, as for the others. -/
def runWith1 (classes : Classes) (kernel : Binary32 → Binary32) : IO UInt32 := do
  let calls ← envNat "BENCH_PAIRS" 500000
  let seed := UInt32.ofNat (← envNat "BENCH_SEED" 0x9E3779B9)
  let inner ← envNat "BENCH_INNER" 3
  for (name, gen, timed, salt) in classes do
    let xs := (lcg calls (seed ^^^ salt * 0x9E3779B9)).mapIdx gen
    let sum ← sweep1 kernel xs 0
    let ns ← if timed then best1 kernel xs inner else pure 0
    IO.println s!"{name} {timed} {ns} {sum}"
  return 0

/-- The square-root slot's bench process, in the same shape. -/
def runSqrt (kernel : Binary32 → Binary32) : IO UInt32 := runWith1 sqrtClasses kernel

end Bench
