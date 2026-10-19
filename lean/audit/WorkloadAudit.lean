import ExactSuite
import ContractExactChecked
import Submission
import Lean

/-! Untimed paired-input diagnostics. Weights, shapes and seeds stay fixed while inputs change.
Float64 is used only for descriptive statistics, never for kernel correctness or scoring. -/
open ExactRuntime ExactSuite Lean

def activationStats (x : Array Word) : Json := Id.run do
  let mut zeros := 0
  let mut nonfinite := 0
  let mut subnormal := 0
  let mut negative := 0
  let mut total : Float := 0
  let mut squares : Float := 0
  let mut largest : Float := 0
  for w in x do
    if !finiteWord w then nonfinite := nonfinite + 1
    else
      let v := (Float32.ofBits w).toFloat
      total := total + v
      squares := squares + v * v
      largest := max largest v.abs
    if w &&& 0x7FFFFFFF == 0 then zeros := zeros + 1
    else if w &&& 0x80000000 != 0 then negative := negative + 1
    if expOf w == 0 && fracOf w != 0 then subnormal := subnormal + 1
  return Json.mkObj [("size", toJson x.size), ("zeros", toJson zeros),
    ("nonfinite", toJson nonfinite), ("subnormal", toJson subnormal),
    ("negative", toJson negative), ("sum", toJson total), ("sum_squares", toJson squares),
    ("max_abs", toJson largest), ("constant", toJson (x.all (· == x[0]!)))]

def sensitivityStats (base other : Array Word) : Json := Id.run do
  let mut changed := 0
  let mut deltaSquares : Float := 0
  for i in [:base.size] do
    if base[i]! != other[i]! then changed := changed + 1
    let delta := (Float32.ofBits base[i]!).toFloat - (Float32.ofBits other[i]!).toFloat
    deltaSquares := deltaSquares + delta * delta
  return Json.mkObj [("activation", activationStats other), ("changed", toJson changed),
    ("delta_squares", toJson deltaSquares)]

def layerDescription : Layer → String × Bool
  | .linear _ _ _ relu _ _ => ("linear", relu)
  | .gemm .. => ("gemm", false)
  | .conv _ relu _ _ => ("conv2d", relu)
  | .depthwise _ relu _ _ => ("depthwise", relu)
  | .bn relu .. => ("scaleShift", relu)
  | .maxpool .. => ("maxpool", false)
  | .avgpool .. => ("avgpool", false)
  | .rmsnorm .. => ("rmsnorm", false)
  | .layernorm .. => ("layernorm", false)
  | .save => ("save", false)
  | .addSaved relu => ("addSaved", relu)

def checkedStep (layer : Layer) (state : Array Word × Array Word) :
    IO (Array Word × Array Word) := do
  match layer.checked Submission.kernels state with
  | .error e => throw (IO.userError e)
  | .ok out =>
    if out.1.isEmpty || !(out.1.all finiteWord) then
      throw (IO.userError "empty or nonfinite workload activation")
    return out

def main (args : List String) : IO Unit := do
  for arg in args do
    let seed := arg.toNat!.toUInt32
    for cell in cells do
      let p := prepareCell cell seed
      let redraw := gen cell.kind p.input.size (seed ^^^ 0xA511E9B3)
      for sample in [:p.samples] do
        let slice := fun a : Array Word => a.extract (sample * p.size) ((sample + 1) * p.size)
        let initial := slice p.input
        let mut base := (initial, #[])
        let mut fresh := (slice redraw, #[])
        let mut negated := (initial.map (fun w => w ^^^ 0x80000000), #[])
        for (layer, index) in p.layers.zipIdx do
          base ← checkedStep layer base
          fresh ← checkedStep layer fresh
          negated ← checkedStep layer negated
          let (op, relu) := layerDescription layer
          IO.println <| (Json.mkObj [("seed", toJson seed.toNat), ("cell", toJson cell.name),
            ("sample", toJson sample), ("layer", toJson index), ("op", toJson op),
            ("relu", toJson relu), ("final", toJson (index + 1 == p.layers.length)),
            ("base", activationStats base.1), ("redraw", sensitivityStats base.1 fresh.1),
            ("negate", sensitivityStats base.1 negated.1)]).compress
