import ContractExactChecked
import Lean
import ExactSuite
import ExactToolkit.ModelProofs

/-! Developer-only specification probe. No imports from Submission or the benchmark.
JSON-lines in/out lets an independently written oracle exercise the actual Lean definitions.
This is not part of the submission gate or the pinned contract. -/

open Lean ExactRuntime

instance : Inhabited Layer := ⟨.save⟩

structure Request where
  op : String
  dims : Array Nat := #[]
  x : Array Nat := #[]
  w : Array Nat := #[]
  b : Array Nat := #[]
  relu : Bool := false
  eps : Nat := 0x3727C5AC
  deriving FromJson, ToJson

def describeLayer (l : Layer) (x saved : Array Word) : Request :=
  let base : Request := { op := "", x := x.map UInt32.toNat }
  let words := fun a : Array Word => a.map UInt32.toNat
  let dims := fun s : ConvShape => #[s.cin, s.h, s.w, s.cout, s.kh, s.kw, s.stride, s.pad]
  match l with
  | .linear r i o relu w b =>
    {base with
      op := "linear", dims := #[r,i,o], relu := relu, w := words w, b := words b}
  | .gemm m k n a => {base with op := "gemm", dims := #[1,m,k,n], x := words a, w := words x}
  | .conv s relu w b => {base with op := "conv2d", dims := dims s, relu := relu, w := words w, b := words b}
  | .depthwise s relu w b =>
      {base with op := "depthwise", dims := dims s, relu := relu, w := words w, b := words b}
  | .bn relu c hw s t =>
    {base with
      op := "scaleShift", dims := #[c,hw], relu := relu, w := words s, b := words t}
  | .maxpool c h w k => {base with op := "maxpool", dims := #[c,h,w,k]}
  | .avgpool c h w k => {base with op := "avgpool", dims := #[c,h,w,k]}
  | .rmsnorm r n eps g => {base with op := "rmsnorm", dims := #[r,n], eps := eps.toNat, w := words g}
  | .layernorm r n eps g b =>
      {base with op := "layernorm", dims := #[r,n], eps := eps.toNat, w := words g, b := words b}
  | .save => {base with op := "save"}
  | .addSaved relu => {base with op := "addRelu", relu := relu, w := words saved}

def traceSuite (seed : UInt32) (boundaries : Bool := false) : Json := Id.run do
  let mut models : Array Json := #[]
  for kind in ["normal", "wide", "sparse"] do
    let builders := if boundaries then [
      ("mlp-min", ExactSuite.mlpLayers kind seed 1 [1,1], 1),
      ("mlp-rows", ExactSuite.mlpLayers kind seed 3 [2,1,3], 6),
      ("cnn-min", ExactSuite.cnnLayers kind seed 1 2 1 1 1, 4),
      ("cnn-odd", ExactSuite.cnnLayers kind seed 2 3 2 1 3, 18),
      ("mnet-min", ExactSuite.mnetLayers kind seed 1 2 1 1 1, 4),
      ("mnet-odd", ExactSuite.mnetLayers kind seed 2 5 2 1 3, 50),
      ("nmlp-min", ExactSuite.nmlpLayers kind seed 1 1 1 1 1, 1),
      ("nmlp-rows", ExactSuite.nmlpLayers kind seed 3 2 1 3 2, 6),
      ("mixer-min", ExactSuite.mixerLayers kind seed 1 1 1, 1),
      ("mixer-rect", ExactSuite.mixerLayers kind seed 3 2 3, 6)] else [
      ("mlp", ExactSuite.mlpLayers kind seed 2 [3,4,2], 6),
      ("cnn", ExactSuite.cnnLayers kind seed 1 4 2 3 2, 16),
      ("mnet", ExactSuite.mnetLayers kind seed 1 4 2 3 2, 16),
      ("nmlp", ExactSuite.nmlpLayers kind seed 2 3 4 3 2, 6),
      ("mixer", ExactSuite.mixerLayers kind seed 2 3 2, 6)]
    for (name, layers, size) in builders do
      let input := ExactSuite.gen kind size (seed + 999)
      let mut state := (input, #[])
      let mut trace : Array Json := #[]
      for layer in layers do
        let request := describeLayer layer state.1 state.2
        match layer.checked specKernels state with
        | .error e => return Json.mkObj [("error", toJson e)]
        | .ok st => state := st
        trace := trace.push (Json.mkObj [("request", toJson request),
          ("actual", toJson (state.1.map UInt32.toNat))])
      models := models.push (Json.mkObj [("model", toJson name), ("kind", toJson kind),
        ("input", toJson (input.map UInt32.toNat)), ("trace", toJson trace)])
  return toJson models

def evaluate (r : Request) : Array Word := Id.run do
  let d := r.dims
  let x := r.x.map UInt32.ofNat
  let w := r.w.map UInt32.ofNat
  let b := r.b.map UInt32.ofNat
  let shape (_ : Unit) : ConvShape :=
    ⟨d[0]!, d[1]!, d[2]!, d[3]!, d[4]!, d[5]!, d[6]!, d[7]!⟩
  if r.op.startsWith "checked:" then
    let op := (r.op.drop 8).toString
    let layer := match op with
      | "linear" => Layer.linear d[0]! d[1]! d[2]! r.relu w b
      | "gemm" => Layer.gemm d[1]! d[2]! d[3]! x
      | "conv2d" => Layer.conv (shape ()) r.relu w b
      | "depthwise" => Layer.depthwise (shape ()) r.relu w b
      | "maxpool" => Layer.maxpool d[0]! d[1]! d[2]! d[3]!
      | "avgpool" => Layer.avgpool d[0]! d[1]! d[2]! d[3]!
      | "scaleShift" => Layer.bn r.relu d[0]! d[1]! w b
      | "rmsnorm" => Layer.rmsnorm d[0]! d[1]! (UInt32.ofNat r.eps) w
      | "layernorm" => Layer.layernorm d[0]! d[1]! (UInt32.ofNat r.eps) w b
      | "addRelu" => Layer.addSaved r.relu
      | _ => panic! s!"unknown checked operation {op}"
    let state := if op == "gemm" then (w, #[]) else (x, w)
    match layer.checked specKernels state with
    | .error _ => return #[1]
    | .ok (out, _) => return #[0] ++ out
  match r.op with
  | "dot" => return #[dotSpec x w]
  | "sum" => return #[sumSpec x]
  | "sqrt" => return #[sqrtRational ((d[0]! : ℚ) / d[1]!)]
  | "checkedConv" =>
    match (Layer.conv (shape ()) r.relu w b).checked specKernels (x, #[]) with
    | .error _ => return #[1]
    | .ok (out, _) => return #[0] ++ out
  | "checkedNorm" =>
    match (Layer.rmsnorm d[0]! d[1]! (UInt32.ofNat r.eps) w).checked specKernels (x, #[]) with
    | .error _ => return #[1]
    | .ok (out, _) => return #[0] ++ out
  | "checkedModel" =>
    let layers := [Layer.save, .linear 1 2 2 false w b, .addSaved true]
    match runModelChecked specKernels layers 2 2 x with
    | .error _ => return #[1]
    | .ok out => return #[0] ++ out
  | "linear" => return linearSpec d[0]! d[1]! d[2]! r.relu x w b
  | "gemm" => return gemmSpec d[0]! d[1]! d[2]! d[3]! x w
  | "conv2d" => return conv2dSpec (shape ()) r.relu x w b
  | "depthwise" => return depthwiseSpec (shape ()) r.relu x w b
  | "avgpool" => return avgpoolSpec d[0]! d[1]! d[2]! d[3]! x
  | "maxpool" => return maxpoolSpec d[0]! d[1]! d[2]! d[3]! x
  | "addRelu" => return addReluSpec r.relu x w
  | "scaleShift" => return scaleShiftSpec r.relu d[0]! d[1]! x w b
  | "rmsnorm" => return rmsnormSpec d[0]! d[1]! (UInt32.ofNat r.eps) x w
  | "layernorm" => return layernormSpec d[0]! d[1]! (UInt32.ofNat r.eps) x w b
  | "model" =>
    -- Two samples; saved residual must be local to each sample.
    let layers := [Layer.save, .linear 1 2 2 false w b, .addSaved true]
    return runModel specKernels layers 2 2 x
  | "shape" => return #[UInt32.ofNat (shape ()).ho, UInt32.ofNat (shape ()).wo]
  | _ => panic! s!"unknown operation {r.op}"

def main : IO Unit := do
  let stdin ← IO.getStdin
  let stdout ← IO.getStdout
  repeat
    let line ← stdin.getLine
    if line.isEmpty then break
    let result := do
      let json ← Json.parse line
      let request : Request ← fromJson? json
      return if request.op == "traceSuite" then traceSuite (UInt32.ofNat request.eps)
        else if request.op == "traceBoundarySuite" then traceSuite (UInt32.ofNat request.eps) true
        else toJson ((evaluate request).map UInt32.toNat)
    match result with
    | .ok json => stdout.putStrLn json.compress
    | .error err => throw (IO.userError err)
