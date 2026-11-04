import ExactSuite
import Submission

/-! Experimental multi-seed batch protocol. Every model is prepared outside inference timing.
Each row is workload|cell true total_nanoseconds output_checksum, flushed as it finishes, and
`workload|cell start` goes to stderr before each cell: a run stopped at its time limit still
shows the rows it finished and the cell it was on.
BENCH_WORKLOADS: id:UInt32seed:profile,...; BENCH_REPS: id|cell=count,...

Every timed call gets an input no earlier call in the process has seen (`ExactSuite.callInput`),
so a kernel that keeps results across calls gains nothing from them. Inputs are drawn in batches
before the clock starts; `total_nanoseconds` is the best, over `BENCH_INNER` passes, of the summed
batch times of one pass. `output_checksum` folds every call's output, so it covers every input. -/

def parseReps (text : String) : List (String × Nat) :=
  (text.splitOn ",").filterMap fun pair => match pair.splitOn "=" with
    | [cell,n] => n.toNat?.map (cell, ·)
    | _ => none

/-- Timed calls per batch. A batch's inputs are drawn before its clock starts, and a small batch
keeps the inputs held at once to a few hundred kilobytes. -/
def batchSize : Nat := 16

/-- The inputs of calls `first` to `first + count - 1`. Never inlined: the inputs are drawn when
this action runs, before the clock is read, not inside the timed loop. -/
@[noinline] def drawInputs (c : ExactSuite.Cell) (p : ExactSuite.PreparedCell) (seed : UInt32)
    (first count : Nat) : IO (Array (Array ExactRuntime.Word)) :=
  pure ((Array.range count).map fun j => ExactSuite.callInput c p seed (first + j))

def main : IO UInt32 := do
  let inner := ((← IO.getEnv "BENCH_INNER").bind String.toNat?).getD 3
  if inner == 0 then throw (IO.userError "inner must be positive")
  let text := (← IO.getEnv "BENCH_WORKLOADS").getD "s0:42:legacy"
  let mut workloads : List (String × UInt32 × String) := []
  for token in text.splitOn "," do
    match token.splitOn ":" with
    | [name,seed,profile] =>
      let some n := seed.toNat? | throw (IO.userError "invalid seed")
      if name.isEmpty || n ≥ 4294967296 ||
          !(["legacy","typical","scale","boundary"].contains profile) then
        throw (IO.userError "invalid workload")
      workloads := workloads ++ [(name,n.toUInt32,profile)]
    | _ => throw (IO.userError "invalid workload token")
  let names := workloads.map (fun (name,_,_) => name)
  if names.length != names.eraseDups.length then throw (IO.userError "duplicate workload")
  let reps := parseReps ((← IO.getEnv "BENCH_REPS").getD "")
  let selected := match (← IO.getEnv "BENCH_CELLS") with
    | none => ExactSuite.cells
    | some cells => ExactSuite.cells.filter fun c => (cells.splitOn ",").contains c.name
  if selected.isEmpty then throw (IO.userError "empty cell selection")
  for (name,seed,profile) in workloads do
    for c in selected do
      let key := s!"{name}|{c.name}"
      IO.eprintln s!"{key} start"
      let prepared := ExactSuite.prepareProfile c seed profile
      let n := ((reps.find? (·.1 == key)).map (·.2)).getD 1
      if n == 0 then throw (IO.userError "repetitions must be positive")
      let mut best := 0
      let mut checksum : UInt32 := 2166136261
      for pass in [0:inner] do
        let mut total := 0
        for batch in [0:(n + batchSize - 1) / batchSize] do
          let first := batch * batchSize
          let count := min batchSize (n - first)
          let inputs ← drawInputs c prepared seed (pass * n + first) count
          let mut outputs : Array (Array ExactRuntime.Word) := Array.mkEmpty count
          let t0 ← IO.monoNanosNow
          for x in inputs do
            let output ← IO.lazyPure fun _ => ExactSuite.runOn Submission.kernels prepared x
            outputs := outputs.push output
          let t1 ← IO.monoNanosNow
          total := total + (t1 - t0)
          for output in outputs do
            checksum := (checksum ^^^ ExactSuite.hashArr output) * 16777619
        best := if best == 0 || total < best then total else best
      IO.println s!"{key} true {best} {checksum}"
      (← IO.getStdout).flush
  return 0
