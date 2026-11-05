import ExactSuite
import RealSuite
import Submission

/-! One-inference-per-process benchmark protocol. Every model/input is prepared outside inference
timing, then each selected cell is evaluated once in this process. Independent repetitions are
launched by the Python harness as separate child processes. Each row is workload|cell true
total_nanoseconds output_checksum, flushed as it finishes, and
`workload|cell start` goes to stderr before each cell: a run stopped at its time limit still
shows the rows it finished and the cell it was on.
BENCH_WORKLOADS: id:UInt32seed:profile,...; BENCH_REPS: id|cell=count,... -/

def parseReps (text : String) : List (String × Nat) :=
  (text.splitOn ",").filterMap fun pair => match pair.splitOn "=" with
    | [cell,n] => n.toNat?.map (cell, ·)
    | _ => none

def medianNat (values : Array Nat) : Nat :=
  let sorted := (values.toList.mergeSort (· <= ·)).toArray
  if values.isEmpty then 0
  else if values.size % 2 == 1 then sorted[values.size / 2]!
  else (sorted[values.size / 2 - 1]! + sorted[values.size / 2]!) / 2

def hasDuplicateKeys (keys : List String) : Bool :=
  match keys with
  | [] => false
  | key :: rest => rest.contains key || hasDuplicateKeys rest

def parseInputSeedMap (text : String) : List (String × List UInt32) :=
  (text.splitOn "\n").filterMap fun line => match line.splitOn "=" with
    | [key, values] =>
      let parsed := (values.splitOn ";").map fun value => do
        let n ← value.toNat?
        if n < 4294967296 then some n.toUInt32 else none
      if key.isEmpty || parsed.any Option.isNone then none
      else some (key, parsed.filterMap id)
    | _ => none

def parseRealWorkloads (text : String) : List (String × String × String × String × String × Nat × Nat × Nat) :=
  (text.splitOn ",").filterMap fun token => match token.splitOn "|" with
    | [id, adapter, family, weightsPath, inputsPath, samples, sampleCount, sampleWords] => do
      let n ← samples.toNat?
      let count ← sampleCount.toNat?
      let words ← sampleWords.toNat?
      some (id, adapter, family, weightsPath, inputsPath, n, count, words)
    | _ => none

def realCellModel (family : String) : String :=
  match family with
  | "resnet" => "cnn"
  | "mobilenet" => "mnet"
  | "normalized_mlp" => "nmlp"
  | _ => family

def main : IO UInt32 := do
  let inner := ((← IO.getEnv "BENCH_INNER").bind String.toNat?).getD 1
  if inner != 1 then throw (IO.userError "a benchmark process must execute exactly one inference")
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
  let inputSeedFile ← IO.getEnv "BENCH_INPUT_SEED_FILE"
  let inputSeedText ← match inputSeedFile with
    | some path => pure (some (← IO.FS.readFile path))
    | none => pure none
  let inputSeedMap := parseInputSeedMap (inputSeedText.getD "")
  let selected := match (← IO.getEnv "BENCH_CELLS") with
    | none => ExactSuite.cells
    | some cells => ExactSuite.cells.filter fun c => (cells.splitOn ",").contains c.name
  if selected.isEmpty then throw (IO.userError "empty cell selection")
  let realWorkloads := parseRealWorkloads ((← IO.getEnv "BENCH_REAL_WORKLOADS").getD "")
  if inputSeedFile.isSome then
    let keys := inputSeedMap.map Prod.fst
    let expectedKeys := workloads.flatMap fun (name, _, _) =>
      selected.map fun cell => s!"{name}|{cell.name}"
    let expectedKeys := expectedKeys ++ realWorkloads.map fun (id, _, family, _, _, _, _, _) =>
      let cell := selected.find? fun cell =>
        cell.model == realCellModel family && cell.batch == 1 && cell.kind == "normal"
      match cell with
      | some c => s!"{id}|{c.name}"
      | none => s!"{id}|<missing-family-cell>"
    let nonemptyLines := (inputSeedText.getD "").splitOn "\n" |>.filter fun line => !line.isEmpty
    if keys.length != nonemptyLines.length || hasDuplicateKeys keys ||
        (keys.mergeSort (· <= ·)) != (expectedKeys.mergeSort (· <= ·)) then
      throw (IO.userError "benchmark input-seed file does not match selected workloads")
  let mut workloadIndex := 0
  for (name,seed,profile) in workloads do
    for c in selected do
      let key := s!"{name}|{c.name}"
      IO.eprintln s!"{key} start"
      let prepared := ExactSuite.prepareProfile c seed profile
      let n := ((reps.find? (·.1 == key)).map (·.2)).getD 1
      if n != 1 then throw (IO.userError "a benchmark process must execute exactly one inference")
      let inputCount := n * inner
      let seeds := match inputSeedMap.find? (fun row => row.1 == key) with
        | some (_, values) => values
        | none => (List.range inputCount).map fun i =>
            ((seed.toNat + workloadIndex * 2654435761 + i * 2246822519) % 4294967296).toUInt32
      if seeds.length != inputCount then throw (IO.userError s!"input seed count mismatch for {key}")
      let inputs := seeds.toArray.map fun inputSeed =>
        ExactSuite.gen c.kind (prepared.samples * prepared.size) inputSeed
      let mut durations : Array Nat := #[]
      let mut outputs : Array (Array ExactRuntime.Word) := #[]
      for group in [0:inner] do
        let t0 ← IO.monoNanosNow
        for r in [0:n] do
          let input := inputs[group * n + r]!
          let output ← IO.lazyPure fun _ => ExactSuite.runPreparedInput Submission.kernels prepared input
          outputs := outputs.push output
        let t1 ← IO.monoNanosNow
        durations := durations.push (t1 - t0)
      let checksum := outputs.foldl (fun h output =>
        (h ^^^ ExactSuite.hashArr output) * 16777619) 2166136261
      IO.println s!"{key} true {medianNat durations} {checksum}"
      (← IO.getStdout).flush
      workloadIndex := workloadIndex + 1
  for (id, adapter, family, weightsPath, inputsPath, samplesPerRun, sampleCount, size) in realWorkloads do
    if id.isEmpty || samplesPerRun == 0 then throw (IO.userError "invalid real workload")
    let some c := selected.find? fun cell =>
      cell.model == realCellModel family && cell.batch == 1 && cell.kind == "normal"
      | throw (IO.userError s!"real workload {id} has no selected family cell")
    let row := s!"{id}|{c.name}"
    IO.eprintln s!"{row} start"
    let setupStart ← IO.monoNanosNow
    let bytes ← IO.FS.readBinFile weightsPath
    let tensors ← match RealSuite.readTensors bytes with
      | .ok value => pure value
      | .error message => throw (IO.userError s!"{id}: {message}")
    let layers ← match RealSuite.prepare adapter family size tensors with
      | .ok value => pure value
      | .error message => throw (IO.userError s!"{id}: {message}")
    let setupEnd ← IO.monoNanosNow
    let requested := samplesPerRun * inner
    let indices := match inputSeedMap.find? (fun entry => entry.1 == row) with
      | some (_, values) => values
      | none => []
    if indices.length != requested then throw (IO.userError s!"input index count mismatch for {row}")
    if indices.any (fun i => i.toNat ≥ sampleCount) then
      throw (IO.userError s!"invalid sample index for {row}")
    let inputStart ← IO.monoNanosNow
    let inputBytes ← IO.FS.readBinFile inputsPath
    let inputTensors ← match RealSuite.readTensors inputBytes with
      | .ok value => pure value
      | .error message => throw (IO.userError s!"{id} selected samples: {message}")
    if inputTensors.size != 1 || inputTensors[0]!.size != samplesPerRun * size then
      throw (IO.userError s!"{id}: selected sample file has invalid dimensions")
    let inputEnd ← IO.monoNanosNow
    let t0 ← IO.monoNanosNow
    let output ← IO.lazyPure fun _ => ExactRuntime.runModel Submission.kernels layers
      samplesPerRun size inputTensors[0]!
    let t1 ← IO.monoNanosNow
    IO.println s!"REAL_PREP|{id}|{setupEnd - setupStart}|{inputEnd - inputStart}"
    let checksum := ExactSuite.hashArr output
    IO.println s!"{row} true {t1 - t0} {checksum}"
    (← IO.getStdout).flush
  return 0
