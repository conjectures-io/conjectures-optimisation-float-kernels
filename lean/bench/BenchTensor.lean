import Bench

/-!
# The tensor slots' benchmark: vectors built from the shared core's generators

A module of its own, so `Bench.lean` stays byte for byte the calibrated one (see BenchDivSqrt). The
sum slot sums `BENCH_PAIRS / sumLen` vectors of `sumLen` elements per sweep, so one sweep is
`BENCH_PAIRS` additions, the same count as the scalar slots, and the gate's per-operation
nanoseconds read the same way. Every result is folded into the class checksum. The bmm slot's
bench is BenchBmm, a module of its own for the same reason.
-/

namespace Bench

open FloatLib.Floats Competition
open FloatLib.Floats.Formats.BinaryInterchange

/-- The length of every vector the sum slot is timed on. -/
def sumLen : Nat := 1000

/-- Values near one with random signs: a dense layer's sums. -/
def sumDense (_ : Nat) (w : UInt32) : UInt32 :=
  (w &&& (0x807FFFFF : UInt32)) ||| (((124 : UInt32) + (w >>> 23) % 7) <<< 23)

/-- About half +0, the rest positive near one: activations after a ReLU. -/
def sumRelu (_ : Nat) (w : UInt32) : UInt32 :=
  if w &&& 1 == 0 then 0 else (w &&& (0x7FFFFF : UInt32)) ||| (((124 : UInt32) + (w >>> 23) % 7) <<< 23)

/-- Random signs over 50 binades: additions that round, absorb small terms and cancel. -/
def sumWide (_ : Nat) (w : UInt32) : UInt32 :=
  (w &&& (0x807FFFFF : UInt32)) ||| (((102 : UInt32) + (w >>> 23) % 50) <<< 23)

/-- The sum slot's classes; `raw` adds NaNs, infinities, subnormals and zeros. -/
def sumClasses : Classes :=
  [("dense", sumDense, true, 0x51), ("relu", sumRelu, true, 0x52),
   ("wide", sumWide, true, 0x53), ("raw", raw, false, 0x54)]

/-- The class's words cut into vectors of `sumLen`. -/
def vectors (xs : Array UInt32) : Array (Vector Binary32 sumLen) := Id.run do
  let mut vs : Array (Vector Binary32 sumLen) := Array.mkEmpty (xs.size / sumLen)
  for v in [0:xs.size / sumLen] do
    vs := vs.push (Vector.ofFn fun (i : Fin sumLen) =>
      Configured.NativeFPU.ofBinary32Bits xs[v * sumLen + i.val]!)
  return vs

/-- One pass of `kernel` over every vector; the checksum folds every result. -/
def sweepSum (kernel : {n : Nat} → Vector Binary32 n → Binary32)
    (vs : Array (Vector Binary32 sumLen)) (seed : UInt32) : IO UInt32 := do
  let mut sink : UInt32 := seed
  for v in vs do
    sink := (sink ^^^ (kernel v).raw.1) * 16777619
  return sink

/-- The fastest of `reps` timed passes, in nanoseconds. -/
def bestSum (kernel : {n : Nat} → Vector Binary32 n → Binary32)
    (vs : Array (Vector Binary32 sumLen)) (reps : Nat) : IO Nat := do
  let mut b := 0
  for r in [0:reps] do
    let t0 ← IO.monoNanosNow
    let s ← sweepSum kernel vs (UInt32.ofNat (r + 1))
    let t1 ← IO.monoNanosNow
    if s == 0xFFFFFFFF then IO.println "unlikely"   -- keeps `s` observed
    b := if b == 0 || t1 - t0 < b then t1 - t0 else b
  return b

/-- The sum slot's bench process, in the scalar slots' shape: one line per class. -/
def runSum (kernel : {n : Nat} → Vector Binary32 n → Binary32) : IO UInt32 := do
  let pairs ← envNat "BENCH_PAIRS" 500000
  let seed := UInt32.ofNat (← envNat "BENCH_SEED" 0x9E3779B9)
  let inner ← envNat "BENCH_INNER" 3
  for (name, gen, timed, salt) in sumClasses do
    let vs := vectors ((lcg (pairs / sumLen * sumLen) (seed ^^^ salt * 0x9E3779B9)).mapIdx gen)
    let sum ← sweepSum kernel vs 0
    let ns ← if timed then bestSum kernel vs inner else pure 0
    IO.println s!"{name} {timed} {ns} {sum}"
  return 0

end Bench
