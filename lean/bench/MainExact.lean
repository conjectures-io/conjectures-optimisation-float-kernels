import ExactSuite
import Submission

/-! Experimental multi-seed batch protocol. Every model is prepared outside inference timing.
Each row is workload|cell true total_nanoseconds output_checksum.
BENCH_WORKLOADS: id:UInt32seed:profile,...; BENCH_REPS: id|cell=count,... -/

def parseReps (text : String) : List (String × Nat) :=
  (text.splitOn ",").filterMap fun pair => match pair.splitOn "=" with
    | [cell,n] => n.toNat?.map (cell, ·)
    | _ => none

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
      let prepared := ExactSuite.prepareProfile c seed profile
      let key := s!"{name}|{c.name}"
      let n := ((reps.find? (·.1 == key)).map (·.2)).getD 1
      if n == 0 then throw (IO.userError "repetitions must be positive")
      let mut best := 0
      let mut checksum : UInt32 := 0
      for _ in [0:inner] do
        let mut output : Array ExactRuntime.Word := #[]
        let t0 ← IO.monoNanosNow
        for r in [0:n] do
          output ← IO.lazyPure fun _ => ExactSuite.runPrepared Submission.kernels prepared r
        let t1 ← IO.monoNanosNow
        checksum := ExactSuite.hashArr output
        best := if best == 0 || t1 - t0 < best then t1 - t0 else best
      IO.println s!"{key} true {best} {checksum}"
  return 0
