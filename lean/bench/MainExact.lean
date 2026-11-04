import ExactSuite
import Submission

/-! Experimental multi-seed batch protocol. Every model is prepared outside inference timing.
Each row is workload|cell true total_nanoseconds output_checksum, flushed as it finishes, and
`workload|cell start` goes to stderr before each cell: a run stopped at its time limit still
shows the rows it finished and the cell it was on.
BENCH_WORKLOADS: id:UInt32seed:profile,...; BENCH_REPS: id|cell=count,... -/

def parseReps (text : String) : List (String × Nat) :=
  (text.splitOn ",").filterMap fun pair => match pair.splitOn "=" with
    | [cell,n] => n.toNat?.map (cell, ·)
    | _ => none

def middleTime (times : Array Nat) (statistic : String) : Nat := Id.run do
  if statistic == "best" then return times.foldl min (times[0]?.getD 0)
  let sorted := times.toList.mergeSort (· ≤ ·) |>.toArray
  let mid := sorted.size / 2
  if sorted.size % 2 == 1 then return sorted[mid]!
  return (sorted[mid - 1]! + sorted[mid]!) / 2

def main : IO UInt32 := do
  let inner := ((← IO.getEnv "BENCH_INNER").bind String.toNat?).getD 3
  if inner == 0 then throw (IO.userError "inner must be positive")
  let statistic := (← IO.getEnv "BENCH_STATISTIC").getD "median"
  if statistic != "median" && statistic != "best" then
    throw (IO.userError "BENCH_STATISTIC must be median or best")
  let inputMode := (← IO.getEnv "BENCH_INPUT_MODE").getD "distinct"
  if inputMode != "distinct" && inputMode != "repeat" then
    throw (IO.userError "BENCH_INPUT_MODE must be distinct or repeat")
  let inputSeedText ← do
    match (← IO.getEnv "BENCH_INPUT_SEED_CHUNKS") with
    | none => IO.getEnv "BENCH_INPUT_SEEDS"
    | some countText => do
      let some count := countText.toNat? | throw (IO.userError "invalid input-seed chunk count")
      if count == 0 || count > 8 then throw (IO.userError "invalid input-seed chunk count")
      let chunks ← (List.range count).mapM fun i => do
        let some chunk ← IO.getEnv s!"BENCH_INPUT_SEEDS_{i}" |
          throw (IO.userError "missing input-seed chunk")
        pure chunk
      pure (some (String.intercalate "," chunks))
  let inputSeedBank ← match inputSeedText with
    | none => pure none
    | some "" => throw (IO.userError "empty input-seed bank")
    | some text => do
      let parsed := (text.splitOn ",").map String.toNat?
      if parsed.any (fun value => value.isNone) then
        throw (IO.userError "invalid input seed")
      let values := parsed.filterMap id
      if values.any (fun value => value ≥ 4294967296) then
        throw (IO.userError "input seed exceeds UInt32")
      pure (some (values.map Nat.toUInt32 |>.toArray))
  if inputMode == "distinct" && inputSeedBank.isNone then
    throw (IO.userError "distinct input mode needs an input-seed bank")
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
  let mut inputSeedCursor := 0
  for (name,seed,profile) in workloads do
    for c in selected do
      let key := s!"{name}|{c.name}"
      IO.eprintln s!"{key} start"
      let prepared := ExactSuite.prepareProfile c seed profile
      let n := ((reps.find? (·.1 == key)).map (·.2)).getD 1
      if n == 0 then throw (IO.userError "repetitions must be positive")
      let mut times : Array Nat := #[]
      let mut checksums : Array UInt32 := #[]
      for _run in [0:inner] do
        let mut total := 0
        let mut runChecksum : UInt32 := 2166136261
        let mut completed := 0
        while completed < n do
          let count := min 16 (n - completed)
          let mut inputs : Array (Array ExactRuntime.Word) := #[]
          for _r in [0:count] do
            let input ← if inputMode == "repeat" then pure prepared.input else do
              let some bank := inputSeedBank | throw (IO.userError "missing input-seed bank")
              if inputSeedCursor >= bank.size then
                throw (IO.userError "input-seed bank is too short")
              let seed := bank[inputSeedCursor]!
              inputSeedCursor := inputSeedCursor + 1
              pure (ExactSuite.gen c.kind (prepared.samples * prepared.size) seed)
            inputs := inputs.push input
          let mut outputs : Array (Array ExactRuntime.Word) := #[]
          let t0 ← IO.monoNanosNow
          for j in [0:count] do
            let output ← IO.lazyPure fun _ =>
              ExactSuite.runPreparedInput Submission.kernels prepared inputs[j]!
            outputs := outputs.push output
          let t1 ← IO.monoNanosNow
          total := total + (t1 - t0)
          for output in outputs do
            runChecksum := (runChecksum ^^^ ExactSuite.hashArr output) * 16777619
          completed := completed + count
          inputs := #[]
          outputs := #[]
        times := times.push total
        checksums := checksums.push runChecksum
      let checksum := checksums.foldl (fun h value => (h ^^^ value) * 16777619) 2166136261
      IO.println s!"{key} true {middleTime times statistic} {checksum}"
      (← IO.getStdout).flush
  if let some bank := inputSeedBank then
    if inputSeedCursor != bank.size then
      throw (IO.userError "input-seed bank has unused entries")
  return 0
