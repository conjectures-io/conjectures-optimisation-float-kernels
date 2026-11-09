import ContractExactChecked
import Lean
import ExactSuite
import ExactToolkit.ModelProofs

/-! Developer-only specification probe. No imports from Submission or the benchmark.
JSON-lines in/out lets an independently written oracle exercise the actual Lean definitions.
This is not part of the submission gate or the pinned contract.

Shapes travel as a flat `dims` array (booleans as 0/1):
* `conv2d`, `depthwise`: `[cin, h, w, cout, kh, kw, strideH, strideW, padT, padL, padB, padR,
  dilH, dilW, groups]`
* `convTranspose`: the convolution's fifteen, then `[outPadH, outPadW]`
* `maxpool`, `avgpool`: `[c, h, w, kh, kw, strideH, strideW, padT, padL, padB, padR, ceilMode,
  countIncludePad]`
* `lstm`, `gru`: `[T, inW, H]`; `w` is `wih ++ whh` and `b` is `bih ++ bhh`
* `lastStep`: `[T, n]` -/

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

/-- A convolution shape's fifteen fields, in declaration order. -/
def convDims (s : ConvShape) : Array Nat :=
  #[s.cin, s.h, s.w, s.cout, s.kh, s.kw, s.strideH, s.strideW, s.padT, s.padL, s.padB, s.padR,
    s.dilH, s.dilW, s.groups]

/-- A transposed convolution's fields: the convolution's, then the output padding. -/
def convTDims (s : ConvTShape) : Array Nat := convDims s.toConvShape ++ #[s.outPadH, s.outPadW]

/-- A pooling shape's thirteen fields, in declaration order. -/
def poolDims (s : PoolShape) : Array Nat :=
  #[s.c, s.h, s.w, s.kh, s.kw, s.strideH, s.strideW, s.padT, s.padL, s.padB, s.padR,
    if s.ceilMode then 1 else 0, if s.countIncludePad then 1 else 0]

def convOf (d : Array Nat) : ConvShape :=
  { cin := d[0]!, h := d[1]!, w := d[2]!, cout := d[3]!, kh := d[4]!, kw := d[5]!,
    strideH := d[6]!, strideW := d[7]!, padT := d[8]!, padL := d[9]!, padB := d[10]!,
    padR := d[11]!, dilH := d[12]!, dilW := d[13]!, groups := d[14]! }

def convTOf (d : Array Nat) : ConvTShape :=
  { toConvShape := convOf d, outPadH := d[15]!, outPadW := d[16]! }

def poolOf (d : Array Nat) : PoolShape :=
  { c := d[0]!, h := d[1]!, w := d[2]!, kh := d[3]!, kw := d[4]!, strideH := d[5]!,
    strideW := d[6]!, padT := d[7]!, padL := d[8]!, padB := d[9]!, padR := d[10]!,
    ceilMode := d[11]! != 0, countIncludePad := d[12]! != 0 }

def describeLayer (l : Layer) (x saved : Array Word) : Request :=
  let base : Request := { op := "", x := x.map UInt32.toNat }
  let words := fun a : Array Word => a.map UInt32.toNat
  match l with
  | .linear r i o relu w b =>
    {base with
      op := "linear", dims := #[r,i,o], relu := relu, w := words w, b := words b}
  | .gemm m k n a => {base with op := "gemm", dims := #[1,m,k,n], x := words a, w := words x}
  | .conv s relu w b =>
      {base with op := "conv2d", dims := convDims s, relu := relu, w := words w, b := words b}
  | .depthwise s relu w b =>
      {base with op := "depthwise", dims := convDims s, relu := relu, w := words w, b := words b}
  | .convTranspose s relu w b =>
    {base with
      op := "convTranspose", dims := convTDims s, relu := relu, w := words w, b := words b}
  | .bn relu c hw s t =>
    {base with
      op := "scaleShift", dims := #[c,hw], relu := relu, w := words s, b := words t}
  | .maxpool s => {base with op := "maxpool", dims := poolDims s}
  | .avgpool s => {base with op := "avgpool", dims := poolDims s}
  | .rmsnorm r n eps g => {base with op := "rmsnorm", dims := #[r,n], eps := eps.toNat, w := words g}
  | .layernorm r n eps g b =>
      {base with op := "layernorm", dims := #[r,n], eps := eps.toNat, w := words g, b := words b}
  | .save => {base with op := "save"}
  | .addSaved relu => {base with op := "addRelu", relu := relu, w := words saved}
  | .attention T heads d causal wq bq wk bk wv bv wo bo =>
    {base with
      op := "attention", dims := #[T, heads, d, (if causal then 1 else 0)],
      w := words (wq ++ wk ++ wv ++ wo), b := words (bq ++ bk ++ bv ++ bo)}
  | .silu => {base with op := "silu"}
  | .lstm T i H wih whh bih bhh =>
    {base with op := "lstm", dims := #[T, i, H], w := words (wih ++ whh), b := words (bih ++ bhh)}
  | .gru T i H wih whh bih bhh =>
    {base with op := "gru", dims := #[T, i, H], w := words (wih ++ whh), b := words (bih ++ bhh)}
  | .lastStep T n => {base with op := "lastStep", dims := #[T, n]}

/-- An `attention` layer from a request: `w` and `b` hold the four projections, Q, K, V, O, one
after another (each `c × c` and `c`, with `c = heads · d`); the last part takes the rest, so the
layer is valid exactly when the lengths are. -/
def attentionOf (d : Array Nat) (w b : Array Word) : Layer :=
  let c := d[1]! * d[2]!
  let part (a : Array Word) (i n : Nat) := a.extract (i * n) ((i + 1) * n)
  .attention d[0]! d[1]! d[2]! (d[3]! == 1) (part w 0 (c * c)) (part b 0 c) (part w 1 (c * c))
    (part b 1 c) (part w 2 (c * c)) (part b 2 c) (w.extract (3 * (c * c)) w.size)
    (b.extract (3 * c) b.size)

/-- A recurrent layer from a request (`gates` 4 for the LSTM, 3 for the GRU): `w` is `wih ++ whh`
split after `gates·H·inW` words, `b` is `bih ++ bhh` split after `gates·H`, so the layer is valid
exactly when the lengths are. -/
def recurrentOf (lstm : Bool) (d : Array Nat) (w b : Array Word) : Layer :=
  let gates := if lstm then 4 else 3
  let wi := gates * d[2]! * d[1]!
  let bi := gates * d[2]!
  let wih := w.extract 0 wi
  let whh := w.extract wi w.size
  let bih := b.extract 0 bi
  let bhh := b.extract bi b.size
  if lstm then .lstm d[0]! d[1]! d[2]! wih whh bih bhh else .gru d[0]! d[1]! d[2]! wih whh bih bhh

/-- A probe-only model on a `2 × 5 × 5` input through every generalised geometry: a grouped,
dilated, asymmetrically padded conv with unequal strides; a ceil-mode padded max pool; a grouped
transposed convolution with output padding; a ceil-mode average pool counting padding; a padded
depthwise conv; global average pooling; a classifier. -/
def generalLayers (kind : String) (seed : UInt32) : List Layer :=
  let w := ExactSuite.wts kind seed
  let b := ExactSuite.biases seed
  let conv : ConvShape :=
    { cin := 2, h := 5, w := 5, cout := 4, kh := 3, kw := 2, strideW := 2, padT := 1, padB := 2,
      padR := 1, dilH := 2, groups := 2 }
  let convT : ConvTShape :=
    { cin := 4, h := 3, w := 2, cout := 2, kh := 3, kw := 3, strideH := 2, padT := 1, padR := 1,
      groups := 2, outPadH := 1 }
  let dw : ConvShape :=
    { cin := 2, h := 4, w := 2, cout := 2, kh := 2, kw := 2, padT := 1, padL := 1 }
  let maxp : PoolShape :=
    { c := 4, h := 4, w := 3, kh := 2, kw := 2, strideH := 2, strideW := 2, padT := 1, padL := 1,
      ceilMode := true }
  let avgp : PoolShape :=
    { c := 2, h := 7, w := 3, kh := 3, kw := 2, strideH := 2, strideW := 1, padT := 1, padB := 1,
      ceilMode := true, countIncludePad := true }
  [.conv conv true (w 1 6 24) (b 2 4),
   .maxpool maxp,
   .convTranspose convT false (w 3 18 36) (b 4 2),
   .avgpool avgp,
   .depthwise dw true (w 5 4 8) (b 6 2),
   .avgpool (.global 2 4 2),
   .linear 1 2 3 false (w 7 2 6) (b 8 3)]

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
      ("mixer-rect", ExactSuite.mixerLayers kind seed 3 2 3, 6),
      ("general", generalLayers kind (seed + 1), 50),
      ("tfm-min", ExactSuite.tfmLayers kind seed 1 1 1 1, 1),
      ("tfm-odd", ExactSuite.tfmLayers kind seed 3 1 3 2, 9),
      ("rnn-min", ExactSuite.rnnLayers kind seed 1 1 1 1, 1),
      ("rnn-odd", ExactSuite.rnnLayers kind seed 3 2 3 2, 6)] else [
      ("mlp", ExactSuite.mlpLayers kind seed 2 [3,4,2], 6),
      ("cnn", ExactSuite.cnnLayers kind seed 1 4 2 3 2, 16),
      ("mnet", ExactSuite.mnetLayers kind seed 1 4 2 3 2, 16),
      ("nmlp", ExactSuite.nmlpLayers kind seed 2 3 4 3 2, 6),
      ("mixer", ExactSuite.mixerLayers kind seed 2 3 2, 6),
      ("general", generalLayers kind seed, 50),
      ("tfm", ExactSuite.tfmLayers kind seed 3 2 2 2, 12),
      ("rnn", ExactSuite.rnnLayers kind seed 3 2 2 2, 6)]
    for (name, layers, size) in builders do
      let input := ExactSuite.gen kind size (seed + 999)
      let mut state := (input, #[])
      let mut trace : Array Json := #[]
      for layer in layers do
        let request := describeLayer layer state.1 state.2
        match layer.checked specKernels state with
        | .error e => return Json.mkObj [("error", toJson e), ("model", toJson name)]
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
  if r.op.startsWith "checked:" then
    let op := (r.op.drop 8).toString
    let layer := match op with
      | "linear" => Layer.linear d[0]! d[1]! d[2]! r.relu w b
      | "gemm" => Layer.gemm d[1]! d[2]! d[3]! x
      | "conv2d" => Layer.conv (convOf d) r.relu w b
      | "depthwise" => Layer.depthwise (convOf d) r.relu w b
      | "convTranspose" => Layer.convTranspose (convTOf d) r.relu w b
      | "maxpool" => Layer.maxpool (poolOf d)
      | "avgpool" => Layer.avgpool (poolOf d)
      | "scaleShift" => Layer.bn r.relu d[0]! d[1]! w b
      | "rmsnorm" => Layer.rmsnorm d[0]! d[1]! (UInt32.ofNat r.eps) w
      | "layernorm" => Layer.layernorm d[0]! d[1]! (UInt32.ofNat r.eps) w b
      | "addRelu" => Layer.addSaved r.relu
      | "attention" => attentionOf d w b
      | "silu" => Layer.silu
      | "lstm" => recurrentOf true d w b
      | "gru" => recurrentOf false d w b
      | "lastStep" => Layer.lastStep d[0]! d[1]!
      | _ => panic! s!"unknown checked operation {op}"
    let state := if op == "gemm" then (w, #[]) else if op == "addRelu" then (x, w) else (x, #[])
    match layer.checked specKernels state with
    | .error _ => return #[1]
    | .ok (out, _) => return #[0] ++ out
  match r.op with
  | "dot" => return #[dotSpec x w]
  | "sum" => return #[sumSpec x]
  | "sqrt" => return #[sqrtRational ((d[0]! : ℚ) / d[1]!)]
  | "checkedConv" =>
    match (Layer.conv (convOf d) r.relu w b).checked specKernels (x, #[]) with
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
  | "conv2d" => return conv2dSpec (convOf d) r.relu x w b
  | "depthwise" => return depthwiseSpec (convOf d) r.relu x w b
  | "convTranspose" => return convTransposeSpec (convTOf d) r.relu x w b
  | "avgpool" => return avgpoolSpec (poolOf d) x
  | "maxpool" => return maxpoolSpec (poolOf d) x
  | "addRelu" => return addReluSpec r.relu x w
  | "scaleShift" => return scaleShiftSpec r.relu d[0]! d[1]! x w b
  | "rmsnorm" => return rmsnormSpec d[0]! d[1]! (UInt32.ofNat r.eps) x w
  | "layernorm" => return layernormSpec d[0]! d[1]! (UInt32.ofNat r.eps) x w b
  | "exp" => return x.map expW
  | "scores" => return scoresSpec d[0]! d[1]! d[2]! (d[3]! == 1) x w
  | "mix" => return mixSpec d[0]! d[1]! d[2]! x w
  | "softmax" => return softmaxSpec d[0]! d[1]! x
  | "silu" => return siluSpec x
  | "sigmoid" => return x.map sigmoidW
  | "tanh" => return x.map tanhW
  | "lstm" => return ((recurrentOf true d w b).apply specKernels (x, #[])).1
  | "gru" => return ((recurrentOf false d w b).apply specKernels (x, #[])).1
  | "lastStep" => return ((Layer.lastStep d[0]! d[1]!).apply specKernels (x, #[])).1
  | "attention" => return ((attentionOf d w b).apply specKernels (x, #[])).1
  | "model" =>
    -- Two samples; saved residual must be local to each sample.
    let layers := [Layer.save, .linear 1 2 2 false w b, .addSaved true]
    return runModel specKernels layers 2 2 x
  | "shape" => return #[UInt32.ofNat (convOf d).ho, UInt32.ofNat (convOf d).wo]
  | "convTShape" => return #[UInt32.ofNat (convTOf d).ho, UInt32.ofNat (convTOf d).wo]
  | "poolShape" => return #[UInt32.ofNat (poolOf d).ho, UInt32.ofNat (poolOf d).wo]
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
