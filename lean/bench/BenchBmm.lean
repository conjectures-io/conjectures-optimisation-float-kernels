import BenchTensor

/-!
# The bmm slot's benchmark: matrices built from the tensor core's generators

A module of its own: adding these lines to BenchTensor slowed `sum/tuned` (its `wide` class from
about 18 to 23 ns), so each slot's bench keeps the module it was calibrated with. One sweep
multiplies `BENCH_PAIRS / (m·n·p)` batches of `bmmM`×`bmmN` by `bmmN`×`bmmP` matrices: `BENCH_PAIRS`
multiply-adds, the same count of steps as every other slot. Every output is folded into the
class checksum.
-/

namespace Bench

open FloatLib.Floats Competition
open FloatLib.Floats.Formats.BinaryInterchange

/-- The bmm slot's matrix shapes: `(m × n) · (n × p)` per batch, 250,000 multiply-adds. -/
def bmmM : Nat := 50
def bmmN : Nat := 100
def bmmP : Nat := 50

/-- Values near one with random signs: a dense layer's weights and activations. -/
def bmmDense (_ : Nat) (w : UInt32) : UInt32 := sumDense 0 w

/-- About half +0, the rest positive near one: activations after a ReLU (used for `A`). -/
def bmmRelu (_ : Nat) (w : UInt32) : UInt32 := sumRelu 0 w

/-- Random signs over 50 binades: products that round, overflow nothing, and cancel in the sum. -/
def bmmWide (_ : Nat) (w : UInt32) : UInt32 :=
  (w &&& (0x807FFFFF : UInt32)) ||| (((102 : UInt32) + (w >>> 23) % 50) <<< 23)

/-- The bmm slot's classes: the generator for `A`, the one for `B`, timed or not, and a salt. -/
def bmmClasses : List (String × (Nat → UInt32 → UInt32) × (Nat → UInt32 → UInt32) × Bool × UInt32) :=
  [("dense", bmmDense, bmmDense, true, 0x61), ("relu", bmmRelu, bmmDense, true, 0x62),
   ("wide", bmmWide, bmmWide, true, 0x63), ("raw", raw, raw, false, 0x64)]

/-- A matrix buffer of `N` elements from the class's words. -/
def buffer (N : Nat) (gen : Nat → UInt32 → UInt32) (seed : UInt32) : Vector Binary32 N :=
  let ws := (lcg N seed).mapIdx gen
  Vector.ofFn fun (i : Fin N) => Configured.NativeFPU.ofBinary32Bits ws[i.val]!

/-- The bmm slot's bench process, in the scalar slots' shape: one line per class. -/
def runBmm
    (kernel : (batch m n p : Nat) → Vector Binary32 (batch * m * n) →
      Vector Binary32 (batch * n * p) → Vector Binary32 (batch * m * p)) : IO UInt32 := do
  let pairs ← envNat "BENCH_PAIRS" 500000
  let seed := UInt32.ofNat (← envNat "BENCH_SEED" 0x9E3779B9)
  let inner ← envNat "BENCH_INNER" 3
  let batch := pairs / (bmmM * bmmN * bmmP)
  for (name, genA, genB, timed, salt) in bmmClasses do
    let A := buffer (batch * bmmM * bmmN) genA (seed ^^^ salt * 0x9E3779B9)
    let B := buffer (batch * bmmN * bmmP) genB (seed ^^^ (salt + 0x80) * 0x9E3779B9)
    let pass (r : UInt32) : IO UInt32 := do
      let mut sink : UInt32 := r
      for c in (kernel batch bmmM bmmN bmmP A B).toArray do
        sink := (sink ^^^ c.raw.1) * 16777619
      return sink
    let sum ← pass 0
    let mut ns := 0
    if timed then
      for r in [0:inner] do
        let t0 ← IO.monoNanosNow
        let s ← pass (UInt32.ofNat (r + 1))
        let t1 ← IO.monoNanosNow
        if s == 0xFFFFFFFF then IO.println "unlikely"   -- keeps `s` observed
        ns := if ns == 0 || t1 - t0 < ns then t1 - t0 else ns
    IO.println s!"{name} {timed} {ns} {sum}"
  return 0

end Bench
