import ExactSuite
import Submission

/-!
# Developer bench for the exact-runtime slot (not used by the gate)

Times the entry in `work/Submission.lean` against FloatLib's kernels (the specs) and native
`Float32`, and checks every output against the spec.

* `exactDev`: `sum` and `dot` at three sizes and three data types;
* `exactDev profile <model>`: one sample of a model (`mlp`, `cnn`, `mnet`, `nmlp`, `mixer`),
  layer by layer;
* `exactDev models`: the model suite against the spec kernels (slow: FloatLib's are ~50x slower).
-/

open ExactRuntime ExactSuite

/-- `a / b` with three decimals. -/
def ratio (a b : Nat) : String :=
  if b == 0 then "-" else
    let q := a * 1000 / b; s!"{q / 1000}.{toString (q % 1000 + 1000) |>.drop 1}"

/-- A native `Float32` left-fold sum, for scale. -/
def nativeSum (x : Array Float32) : UInt32 := (x.foldl (· + ·) (Float32.ofBits 0)).toBits

/-- A native `Float32` left-fold dot product, for scale. -/
def nativeDot (x y : Array Float32) : UInt32 := Id.run do
  let mut acc := Float32.ofBits 0
  for k in [0:x.size] do
    acc := acc + x[k]! * y[k]!
  return acc.toBits

/-- `sum` and `dot` against FloatLib's kernels and native floats, at three sizes and data types. -/
def kernelBench : IO Unit := do
  let K := Submission.kernels
  IO.println "| kernel | input | n | FloatLib | entry | native | entry/FloatLib | entry/native |"
  IO.println "|---|---|---:|---:|---:|---:|---:|---:|"
  for n in [1024, 16384, 262144] do
    for kind in ["normal", "wide", "sparse"] do
      let x := gen kind n 1
      let y := gen kind n 2
      let xf := x.map Float32.ofBits
      let yf := y.map Float32.ofBits
      let r := if n ≥ 262144 then 3 else 7
      let r0 := if n ≥ 262144 then 1 else r
      let rows : List (String × (Nat × UInt32) × (Nat × UInt32) × (Nat × UInt32)) := [
        ("sum", ← best r0 fun i => sumSpec (touch i x), ← best r fun i => K.core.sum (touch i x),
          ← best r fun _ => nativeSum xf),
        ("dot", ← best r0 fun i => dotSpec (touch i x) y, ← best r fun i => K.core.dot (touch i x) y,
          ← best r fun _ => nativeDot xf yf)]
      for (name, s0, s1, s2) in rows do
        IO.println s!"| {name} | {kind} | {n} | {s0.1 / 1000} us | {s1.1 / 1000} us | {s2.1 / 1000} us | {ratio s1.1 s0.1} | {ratio s1.1 s2.1} |"
        if s0.2 != s1.2 then IO.println "  ^ the entry disagrees with FloatLib"

/-- Every suite cell against FloatLib's kernels, with bit identity. -/
def modelBench : IO Unit := do
  IO.println "| cell | FloatLib | entry | entry/FloatLib | identical |"
  IO.println "|---|---:|---:|---:|---|"
  for c in cells do
    let b0 ← best 1 fun _ => runCell specKernels c 42
    let b1 ← best 2 fun _ => runCell Submission.kernels c 42
    IO.println s!"| {c.name} | {b0.1 / 1000000} ms | {b1.1 / 1000000} ms | {ratio b1.1 b0.1} | {if b0.2 == b1.2 then "yes" else "NO"} |"

/-- A layer's name for the profile. -/
def ExactRuntime.Layer.label : Layer → String
  | .linear rows i o _ _ _ => s!"linear {rows}×{i}→{o}"
  | .gemm m k n _ => s!"gemm {m}×{k}·{k}×{n}"
  | .conv s _ _ _ => s!"conv2d {s.kh}×{s.kw} {s.cin}→{s.cout} on {s.h}×{s.w}"
  | .depthwise s _ _ _ => s!"depthwise {s.kh}×{s.kw} {s.cin} on {s.h}×{s.w}"
  | .bn _ c hw _ _ => s!"scaleShift {c}×{hw}"
  | .maxpool c h w k => s!"maxpool {k}×{k} {c}×{h}×{w}"
  | .avgpool c h w k => s!"avgpool {k}×{k} {c}×{h}×{w}"
  | .rmsnorm rows n _ _ => s!"rmsnorm {rows}×{n}"
  | .layernorm rows n _ _ _ => s!"layernorm {rows}×{n}"
  | .save => "save"
  | .addSaved _ => "addRelu"

/-- One sample of a model, layer by layer. -/
def profile (model : String) : IO Unit := do
  let K := Submission.kernels
  let (layers, _, size) := cellModel ⟨model, 1, "normal"⟩ 42
  IO.println s!"| {model} layer | µs |"
  IO.println "|---|---:|"
  let mut st : Array Word × Array Word := (gen "normal" size 7, #[])
  let mut total := 0
  for l in layers do
    let cur := st
    let (ns, _) ← best 5 fun i => hashArr (l.apply K (touch i cur.1, cur.2)).1
    total := total + ns
    IO.println s!"| {l.label} | {ns / 1000} |"
    st := l.apply K st
  IO.println s!"| total | {total / 1000} |"

/-- `exactDev`, `exactDev profile <model>` or `exactDev models`. -/
def main (args : List String) : IO UInt32 := do
  if args.contains "models" then modelBench
  else if args.head? == some "profile" then profile (args.getD 1 "cnn")
  else kernelBench
  return 0
