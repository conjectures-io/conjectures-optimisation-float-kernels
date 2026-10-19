import ExactSuite
import Submission

/-!
# The exact-runtime slot's bench: the model suite over `Submission.kernels`

Models drawn from `BENCH_SEED`, the same for champion and challenger. Each cell runs as often as
`BENCH_REPS` says (`cell=n,cell=n,…`, set by the gate's calibration; 1 if absent), and its time is
the best of `BENCH_INNER` totals. One line per cell, `name true ns checksum`, as the gate parses.
-/

/-- `cell=n` pairs from `BENCH_REPS`. -/
def parseReps (text : String) : List (String × Nat) :=
  (text.splitOn ",").filterMap fun pair =>
    match pair.splitOn "=" with
    | [cell, n] => n.toNat?.map (cell, ·)
    | _ => none

/-- Time every cell as often as `BENCH_REPS` and `BENCH_INNER` say; one line per cell. -/
def main : IO UInt32 := do
  let env (name : String) (default : Nat) : IO Nat :=
    return ((← IO.getEnv name).bind String.toNat?).getD default
  let seed := UInt32.ofNat (← env "BENCH_SEED" 42)
  let inner ← env "BENCH_INNER" 2
  let reps := parseReps ((← IO.getEnv "BENCH_REPS").getD "")
  for c in ExactSuite.cells do
    let n := ((reps.find? (·.1 == c.name)).map (·.2)).getD 1
    let mut best := 0
    let mut sum : UInt32 := 0
    for _ in [0:inner] do
      let t0 ← IO.monoNanosNow
      for r in [0:n] do
        sum ← IO.lazyPure fun _ => ExactSuite.runCell Submission.kernels c seed r
      let t1 ← IO.monoNanosNow
      best := if best == 0 || t1 - t0 < best then t1 - t0 else best
    IO.println s!"{c.name} true {best} {sum}"
  return 0
