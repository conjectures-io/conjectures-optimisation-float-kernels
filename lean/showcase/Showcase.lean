import ExactSuite
import Submission
import Scalar.TunedAdd
import Scalar.FastMul

/-!
# Showcase benchmark for the exact runtime (not used by the gate)

Two models trained on scikit-learn's 8×8 digits (`train.py` writes `digits.bin`), run on the 360
test images by four runtimes:

* `exact`: the entry in `work/Submission.lean`, proved equal to the spec;
* `spec`: FloatLib's own kernels, the reference bits;
* `fold`: today's proved left fold, every product and sum rounded, with the current competition's
  best proved scalar kernels (`TunedAdd.add`, `FastMul.mul`);
* `native`: hardware `Float32` through the same Lean model code, unproved, rounding after every
  step; both it and `fold` are IEEE left folds, so their errors should agree. Its time is Lean's
  model code with hardware floats, not a BLAS's.

For each it reports the time per image, the test accuracy, whether every output word equals the
spec's, and the error of the output logits in ulps against the spec.

    lake exe exactShowcase [path to digits.bin]     (from lean/; the default is showcase/digits.bin)
-/

open ExactRuntime ExactSuite

/-! ## The other runtimes: the same model code with other dot products -/

/-- Each product and each sum rounded, left to right, with today's best proved scalar kernels. -/
def foldDot : DotFn := fun n fa fb => Id.run do
  let mut acc := TunedAdd.ofBits 0
  for t in [0:n] do
    acc := TunedAdd.add acc (FastMul.mul (TunedAdd.ofBits (fa t)) (TunedAdd.ofBits (fb t)))
  return acc.raw.1

/-- The exact runtime's element kernels, every dot product a `foldDot`. -/
def foldKernels : Kernels where
  core := specCore
  dense := ⟨linearWith foldDot, gemmWith foldDot⟩
  conv := ⟨conv2dWith foldDot, depthwiseWith foldDot⟩
  elem := ⟨avgpoolWith foldDot, maxpoolSpec, addReluSpec, scaleShiftSpec⟩
  norm := ⟨rmsnormWith foldDot, layernormWith foldDot⟩

/-- A word as a native `Float32`. -/
@[inline] def f32 (w : Word) : Float32 := Float32.ofBits w

/-- Native `Float32`: each product and each sum rounded, left to right. -/
def nativeDot : DotFn := fun n fa fb => Id.run do
  let mut acc : Float32 := 0
  for t in [0:n] do
    acc := acc + f32 (fa t) * f32 (fb t)
  return acc.toBits

/-- Native max pooling. -/
def nativeMaxpool (c h w k : Nat) (inp : Array Word) : Array Word :=
  Array.ofFn (n := c * (h / k * (w / k))) fun q =>
    let ch := q.val / (h / k * (w / k))
    let y := q.val / (w / k) % (h / k)
    let x := q.val % (w / k)
    let m := (List.range (k * k)).foldl (fun acc t =>
      let v := f32 (poolAt h w k inp ch y x t)
      if acc < v then v else acc) (f32 (poolAt h w k inp ch y x 0))
    m.toBits

/-- Native average pooling: a native sum, then a native divide. -/
def nativeAvgpool (c h w k : Nat) (inp : Array Word) : Array Word :=
  Array.ofFn (n := c * (h / k * (w / k))) fun q =>
    let ch := q.val / (h / k * (w / k))
    let y := q.val / (w / k) % (h / k)
    let x := q.val % (w / k)
    (f32 (nativeDot (k * k) (poolAt h w k inp ch y x) (fun _ => oneW)) / (k * k).toFloat32).toBits

/-- Hardware `Float32` through the same model code (norms keep the runtime's specs; unused here). -/
def nativeKernels : Kernels where
  core := specCore
  dense := ⟨linearWith nativeDot, gemmWith nativeDot⟩
  conv := ⟨conv2dWith nativeDot, depthwiseWith nativeDot⟩
  elem := ⟨nativeAvgpool, nativeMaxpool,
    fun relu a b => Array.ofFn (n := a.size) fun i => act relu (f32 a[i] + f32 b[i.val]!).toBits,
    fun relu c hw x s t => Array.ofFn (n := c * hw) fun i =>
      act relu (f32 x[i.val]! * f32 s[i.val / hw]! + f32 t[i.val / hw]!).toBits⟩
  norm := specNorm

/-! ## The trained models -/

/-- `digits.bin`: float32 tensors, each a little-endian u32 count and that many words. -/
def readTensors (bytes : ByteArray) : Array (Array Word) := Id.run do
  let word (i : Nat) : Word :=
    bytes[i]!.toUInt32 ||| bytes[i + 1]!.toUInt32 <<< 8 ||| bytes[i + 2]!.toUInt32 <<< 16 |||
      bytes[i + 3]!.toUInt32 <<< 24
  let mut out := #[]
  let mut i := 0
  while i + 4 ≤ bytes.size do
    let n := (word i).toNat
    out := out.push (Array.ofFn (n := n) fun k => word (i + 4 + 4 * k.val))
    i := i + 4 + 4 * n
  return out

/-- The trained MLP: 64 → 64 → 32 → 10, ReLU between. -/
def mlpLayers (t : Array (Array Word)) : List Layer :=
  [.linear 1 64 64 true t[0]! t[1]!, .linear 1 64 32 true t[2]! t[3]!,
   .linear 1 32 10 false t[4]! t[5]!]

/-- The trained CNN: conv 1 → 8, max pool, conv 8 → 16, global average, linear. -/
def cnnLayers (t : Array (Array Word)) : List Layer :=
  [.conv ⟨1, 8, 8, 8, 3, 3, 1, 1⟩ true t[6]! t[7]!, .maxpool 8 8 8 2,
   .conv ⟨8, 4, 4, 16, 3, 3, 1, 1⟩ true t[8]! t[9]!, .avgpool 16 4 4 4,
   .linear 1 16 10 false t[10]! t[11]!]

/-! ## Measures -/

/-- Words in the order of the values they stand for, so ulps are differences. -/
def ordered (w : Word) : Int :=
  if w &&& 0x80000000 != 0 then -((w &&& 0x7FFFFFFF).toNat : Int) else w.toNat

/-- The distance between two words in ulps. -/
def ulps (a b : Word) : Nat := (ordered a - ordered b).natAbs

/-- How many of the samples' 10 logits peak at the label. -/
def correct (logits labels : Array Word) : Nat :=
  (List.range labels.size).countP fun i =>
    let best := (List.range 10).foldl (fun j k =>
      if f32 logits[i * 10 + j]! < f32 logits[i * 10 + k]! then k else j) 0
    best == labels[i]!.toNat

/-- `q / 10^digits` with `digits` decimals. -/
def fixed (q : Nat) (digits : Nat) : String :=
  let p := 10 ^ digits
  s!"{q / p}.{toString (q % p + p) |>.drop 1}"

/-- The report: four runtimes on both trained models. -/
def main (args : List String) : IO Unit := do
  let path := args.headD "showcase/digits.bin"
  let t := readTensors (← IO.FS.readBinFile path)
  let images := t[12]!
  let labels := t[13]!
  let n := labels.size
  IO.println s!"digits.bin: {t.size} tensors, {n} test images, hash {hashArr (t.foldl (· ++ ·) #[])}"
  IO.println ""
  IO.println "| model | runtime | per image | vs exact | accuracy | same bits as spec | max ulps | mean ulps |"
  IO.println "|---|---|---:|---:|---:|---|---:|---:|"
  let runtimes := [("exact", Submission.kernels, 5), ("spec", specKernels, 1),
    ("fold", foldKernels, 3), ("native", nativeKernels, 5)]
  for (model, layers) in [("mlp", mlpLayers t), ("cnn", cnnLayers t)] do
    let reference := runModel specKernels layers n 64 images
    let mut base := 0
    for (name, K, reps) in runtimes do
      let mut ns := 0
      let mut out := #[]
      for r in [0:reps] do
        let t0 ← IO.monoNanosNow
        out ← IO.lazyPure fun _ => runModel K layers n 64 (touch r images)
        let t1 ← IO.monoNanosNow
        ns := if ns == 0 || t1 - t0 < ns then t1 - t0 else ns
      if name == "exact" then base := ns
      let errs := (out.zip reference).map fun (a, b) => ulps a b
      let total := errs.foldl (· + ·) 0
      IO.println (s!"| {model} | {name} | {fixed (ns / n / 10) 2} µs | {fixed (ns * 100 / base) 2}x | " ++
        s!"{correct out labels}/{n} ({fixed ((correct out labels * 2000 / n + 1) / 2) 1}%) | " ++
        s!"{if out == reference then "yes" else "no"} | " ++
        s!"{errs.foldl max 0} | {fixed (total * 100 / errs.size) 2} |")
