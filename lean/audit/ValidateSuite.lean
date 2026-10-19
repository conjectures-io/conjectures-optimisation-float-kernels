import ExactSuite
import ContractExactChecked
import Submission
import Lean

/-! Developer validation, outside timing: all layers of all generated models.
Rejects invalid inputs and exceptional activations; records zero fractions for inspection.
No submission is judged by these developer diagnostics. -/

open ExactRuntime ExactSuite Lean

def main (args : List String) : IO Unit := do
  let seeds := if args.isEmpty then [42, 20261019, 20261020]
    else args.map (fun s => s.toNat!.toUInt32)
  for seed in seeds do
    for c in cells do
      let p := prepareCell c seed
      for sample in [0:p.samples] do
        let mut state := (p.input.extract (sample * p.size) ((sample + 1) * p.size), #[])
        for (layer, index) in p.layers.zipIdx do
          match layer.checked Submission.kernels state with
          | .error e => throw (IO.userError s!"{seed}:{c.name}:{index}: {e}")
          | .ok st => state := st
          let nonfinite := state.1.foldl (fun n w => n + if finiteWord w then 0 else 1) 0
          let zeros := state.1.foldl (fun n w => n + if w &&& 0x7FFFFFFF == 0 then 1 else 0) 0
          let negative := state.1.foldl (fun n w =>
            n + if w &&& 0x80000000 != 0 && w &&& 0x7FFFFFFF != 0 then 1 else 0) 0
          let subnormal := state.1.foldl (fun n w =>
            n + if expOf w == 0 && fracOf w != 0 then 1 else 0) 0
          IO.println <| (Json.mkObj [
            ("seed", toJson seed.toNat), ("cell", toJson c.name),
            ("sample", toJson sample), ("layer", toJson index),
            ("size", toJson state.1.size), ("nonfinite", toJson nonfinite),
            ("zeros", toJson zeros), ("negative", toJson negative),
            ("subnormal", toJson subnormal)]).compress
          if nonfinite > 0 || state.1.isEmpty then
            throw (IO.userError "invalid or exceptional intermediate activation")

