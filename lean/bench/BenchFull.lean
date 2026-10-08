import BenchBmm

/-!
# The full entry's benchmark: all four kernels in one process

Times an entry's `add` and `mul` on the scalar slots' classes, its `sum` on the sum slot's and its
`bmm` on the bmm slot's: the same generators and salts, so the operands are the practice boards'.
Every class name carries its kernel (`add:near-one`, `bmm:relu`), since `add` and `mul` share class
names. Each class is `BENCH_PAIRS` scalar steps, and the gate's ratio is the total time over all
twelve timed classes, so every kernel counts by the time it takes.

The loops are copies of the slots' runners with the prefix added, not changes to them: each
slot's bench module stays byte for byte the one it was calibrated with.
-/

namespace Bench

open FloatLib.Floats Competition
open FloatLib.Floats.Formats.BinaryInterchange

/-- One scalar kernel over its classes, every line prefixed with the kernel's name. -/
def runPairsAs (kernelName : String) (classes : Classes) (kernel : Binary32 → Binary32 → Binary32) :
    IO Unit := do
  let pairs ← envNat "BENCH_PAIRS" 500000
  let seed := UInt32.ofNat (← envNat "BENCH_SEED" 0x9E3779B9)
  let inner ← envNat "BENCH_INNER" 3
  for (name, gen, timed, salt) in classes do
    let xs := (lcg (pairs + 1) (seed ^^^ salt * 0x9E3779B9)).mapIdx gen
    let sum ← sweep kernel xs 0
    let ns ← if timed then best kernel xs inner else pure 0
    IO.println s!"{kernelName}:{name} {timed} {ns} {sum}"

/-- The sum kernel over its classes, prefixed. -/
def runSumAs (kernel : {n : Nat} → Vector Binary32 n → Binary32) : IO Unit := do
  let pairs ← envNat "BENCH_PAIRS" 500000
  let seed := UInt32.ofNat (← envNat "BENCH_SEED" 0x9E3779B9)
  let inner ← envNat "BENCH_INNER" 3
  for (name, gen, timed, salt) in sumClasses do
    let vs := vectors ((lcg (pairs / sumLen * sumLen) (seed ^^^ salt * 0x9E3779B9)).mapIdx gen)
    let sum ← sweepSum kernel vs 0
    let ns ← if timed then bestSum kernel vs inner else pure 0
    IO.println s!"sum:{name} {timed} {ns} {sum}"

/-- The bmm kernel over its classes, prefixed. -/
def runBmmAs
    (kernel : (batch m n p : Nat) → Vector Binary32 (batch * m * n) →
      Vector Binary32 (batch * n * p) → Vector Binary32 (batch * m * p)) : IO Unit := do
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
    IO.println s!"bmm:{name} {timed} {ns} {sum}"

/-- The full entry's bench process: its four kernels, one prefixed line per class. -/
def runFull (add mul : Binary32 → Binary32 → Binary32)
    (sum : {n : Nat} → Vector Binary32 n → Binary32)
    (bmm : (batch m n p : Nat) → Vector Binary32 (batch * m * n) →
      Vector Binary32 (batch * n * p) → Vector Binary32 (batch * m * p)) : IO UInt32 := do
  runPairsAs "add" addClasses add
  runPairsAs "mul" mulClasses mul
  runSumAs sum
  runBmmAs bmm
  return 0

end Bench
