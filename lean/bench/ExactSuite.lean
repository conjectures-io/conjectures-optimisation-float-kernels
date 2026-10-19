import ContractExact

/-!
# The exact-runtime suite: inputs and models, shared by the gate's bench and the dev bench

Everything is drawn from a seed: data of one of three types, and the five models (MLP,
ResNet-style CNN, MobileNet-style CNN, normalised MLP, Mixer-style block).
-/

namespace ExactSuite

open ExactRuntime

/-- `n` pseudo-random words. -/
def lcg (n : Nat) (seed : UInt32) : Array UInt32 := Id.run do
  let mut a : Array UInt32 := Array.mkEmpty n
  let mut s := seed
  for _ in [0:n] do
    s := s * 1664525 + 1013904223
    a := a.push s
  return a

/-- Bounded data: normal in [2^-6, 1), wide in [2^-40, 1), sparse half zeros.
Signs and significands vary; the exponent range never creates enormous initial activations. -/
def gen (kind : String) (n : Nat) (seed : UInt32) : Array Word :=
  (lcg n seed).map fun (w : UInt32) =>
    let sgnFrac : UInt32 := w &&& 0x807FFFFF
    let e (lo span : UInt32) : UInt32 := (lo + (w >>> 23) % span) <<< (23 : UInt32)
    match kind with
    | "wide" => sgnFrac ||| e 87 40
    | "sparse" => if (w >>> 9) &&& 1 == 0 then 0 else sgnFrac ||| e 121 6
    | _ => sgnFrac ||| e 121 6

/-- Batch-norm and normalisation scales near 1. -/
def scales (n : Nat) (seed : UInt32) : Array Word :=
  (gen "normal" n seed).map fun w => (w &&& 0x007FFFFF) ||| 0x3F800000

/-- 1e-5, the normalisation epsilon. -/
def eps : Word := 0x3727C5AC

/-- Weights scaled by fan-in, not total parameter count. The bounded log-uniform draw has
variance about 0.13, so a gain of four approximates a He-style scale for ReLU layers.
Wide workloads vary input exponents; their weights use the ordinary bounded distribution.
Sparse workloads retain half-zero weights. All preparation is outside inference timing. -/
def wts (kind : String) (seed : UInt32) (k fanIn n : Nat) : Array Word :=
  let divisor := ofW (natW (max 1 (Nat.sqrt fanIn)))
  let scale := FloatLib.Floats.Formats.BinaryInterchange.Model.div (ofW (natW 4)) divisor
  (gen (if kind == "sparse" then "sparse" else "normal") n
      (seed + 101 * k.toUInt32)).map fun w =>
    toWord (FloatLib.Floats.Formats.BinaryInterchange.Model.mul (ofW w) scale)

/-- Small biases, independently scaled from weights; avoid erasing input-dependent signals. -/
def biases (seed : UInt32) (k n : Nat) : Array Word :=
  (gen "normal" n (seed + 101 * k.toUInt32)).map fun w =>
    toWord (FloatLib.Floats.Formats.BinaryInterchange.Model.div (ofW w) (ofW (natW 256)))

/-- An MLP: `linear` layers through `widths`, with ReLU between them. -/
def mlpLayers (kind : String) (seed : UInt32) (rows : Nat) (widths : List Nat) : List Layer :=
  let pairs := widths.zip widths.tail
  pairs.zipIdx.map fun ((i, o), k) =>
    .linear rows i o (k + 1 < pairs.length)
      (wts kind seed (2 * k) i (i * o)) (biases seed (2 * k + 1) o)

/-- ResNet-style: conv, a residual block, max pooling, conv, global average, classifier. -/
def cnnLayers (kind : String) (seed : UInt32) (cin n c1 c2 classes : Nat) : List Layer :=
  let n2 := n / 2
  let w := wts kind seed
  let b := biases seed
  let bn (relu : Bool) (c hw k : Nat) : Layer := .bn relu c hw (scales c (seed + 7 * k.toUInt32)) (b k c)
  [.conv ⟨cin, n, n, c1, 3, 3, 1, 1⟩ false (w 1 (cin * 9) (c1 * cin * 9)) (b 2 c1), bn true c1 (n * n) 3,
   .save,
   .conv ⟨c1, n, n, c1, 3, 3, 1, 1⟩ false (w 4 (c1 * 9) (c1 * c1 * 9)) (b 5 c1), bn true c1 (n * n) 6,
   .conv ⟨c1, n, n, c1, 3, 3, 1, 1⟩ false (w 7 (c1 * 9) (c1 * c1 * 9)) (b 8 c1), bn false c1 (n * n) 9,
   .addSaved true,
   .maxpool c1 n n 2,
   .conv ⟨c1, n2, n2, c2, 3, 3, 1, 1⟩ false (w 10 (c1 * 9) (c2 * c1 * 9)) (b 11 c2), bn true c2 (n2 * n2) 12,
   .avgpool c2 n2 n2 n2,
   .linear 1 c2 classes false (w 13 c2 (classes * c2)) (b 14 classes)]

/-- MobileNet-style: depthwise 3×3 and pointwise 1×1 convolutions, average pooling. -/
def mnetLayers (kind : String) (seed : UInt32) (cin n c1 c2 classes : Nat) : List Layer :=
  let n2 := n / 2
  let w := wts kind seed
  let b := biases seed
  let bn (c hw k : Nat) : Layer := .bn true c hw (scales c (seed + 7 * k.toUInt32)) (b k c)
  [.conv ⟨cin, n, n, c1, 3, 3, 1, 1⟩ false (w 1 (cin * 9) (c1 * cin * 9)) (b 2 c1), bn c1 (n * n) 3,
   .depthwise ⟨c1, n, n, c1, 3, 3, 1, 1⟩ false (w 4 9 (c1 * 9)) (b 5 c1), bn c1 (n * n) 6,
   .conv ⟨c1, n, n, c2, 1, 1, 1, 0⟩ false (w 7 c1 (c2 * c1)) (b 8 c2), bn c2 (n * n) 9,
   .avgpool c2 n n 2,
   .depthwise ⟨c2, n2, n2, c2, 3, 3, 1, 1⟩ false (w 10 9 (c2 * 9)) (b 11 c2), bn c2 (n2 * n2) 12,
   .conv ⟨c2, n2, n2, c2, 1, 1, 1, 0⟩ false (w 13 c2 (c2 * c2)) (b 14 c2), bn c2 (n2 * n2) 15,
   .avgpool c2 n2 n2 n2,
   .linear 1 c2 classes false (w 16 c2 (classes * c2)) (b 17 classes)]

/-- An MLP with RMS norm and layer norm between its layers. -/
def nmlpLayers (kind : String) (seed : UInt32) (rows i h h2 out : Nat) : List Layer :=
  let w := wts kind seed
  let b := biases seed
  [.linear rows i h true (w 1 i (h * i)) (b 2 h),
   .rmsnorm rows h eps (scales h (seed + 3)),
   .linear rows h h2 true (w 4 h (h2 * h)) (b 5 h2),
   .layernorm rows h2 eps (scales h2 (seed + 6)) (b 7 h2),
   .linear rows h2 out false (w 8 h2 (out * h2)) (b 9 out)]

/-- A Mixer-style block (ReLU instead of GELU) on `T` tokens of `C` channels: layer norm, token
mixing (a `gemm` across tokens), residual; layer norm, channel MLP, residual; classifier. -/
def mixerLayers (kind : String) (seed : UInt32) (T C classes : Nat) : List Layer :=
  let w := wts kind seed
  let b := biases seed
  [.save, .layernorm T C eps (scales C (seed + 1)) (b 2 C),
   .gemm T T C (w 3 T (T * T)), .addSaved false,
   .save, .layernorm T C eps (scales C (seed + 4)) (b 5 C),
   .linear T C (2 * C) true (w 6 C (2 * C * C)) (b 7 (2 * C)),
   .linear T (2 * C) C false (w 8 (2 * C) (C * 2 * C)) (b 9 C), .addSaved false,
   .linear 1 (T * C) classes false (w 10 (T * C) (classes * T * C)) (b 11 classes)]

/-- A cell's checksum: an FNV-1a hash of its output words. -/
def hashArr (v : Array UInt32) : UInt32 := v.foldl (fun h x => (h ^^^ x) * 16777619) 2166136261

/-- `a` unchanged, but only known at run time: stops the compiler computing a timed call once,
ahead of the timing loop, when its inputs are constants. -/
@[noinline] def touch (i : Nat) (a : Array Word) : Array Word :=
  if i = 1000000007 then a.push 0 else a

/-- Best of `reps` wall-clock runs of `f i` (`i` the run number), in nanoseconds, and the last
result. `IO.lazyPure` keeps the call between the two clock readings. -/
def best (reps : Nat) (f : Nat → UInt32) : IO (Nat × UInt32) := do
  let mut b := 0
  let mut last := 0
  for i in [0:reps] do
    let t0 ← IO.monoNanosNow
    last ← IO.lazyPure fun _ => f i
    let t1 ← IO.monoNanosNow
    b := if b == 0 || t1 - t0 < b then t1 - t0 else b
  return (b, last)

/-- One cell: a model at a batch size on a data type. -/
structure Cell where
  model : String
  batch : Nat
  kind : String

/-- `model:batch:kind`, the name the gate reads. -/
def Cell.name (c : Cell) : String := s!"{c.model}:b{c.batch}:{c.kind}"

/-- The 30 cells: 5 models × 2 batch sizes × 3 data types. -/
def cells : List Cell :=
  ["normal", "wide", "sparse"].flatMap fun kind =>
    [⟨"mlp", 1, kind⟩, ⟨"mlp", 8, kind⟩, ⟨"cnn", 1, kind⟩, ⟨"cnn", 4, kind⟩,
     ⟨"mnet", 1, kind⟩, ⟨"mnet", 4, kind⟩, ⟨"nmlp", 1, kind⟩, ⟨"nmlp", 8, kind⟩,
     ⟨"mixer", 1, kind⟩, ⟨"mixer", 4, kind⟩]

/-- A cell's model, with sizes drawn from `seed`, and how to run it: the layers, the number of
samples, the words per sample. -/
def cellModel (c : Cell) (seed : UInt32) : List Layer × Nat × Nat :=
  let r := lcg 8 (seed ^^^ 0x5bd1e995)
  let pick (i lo span : Nat) : Nat := lo + (r[i]!.toNat % span)
  let side := if r[5]! % 2 == 0 then 12 else 16
  match c.model with
  | "mlp" =>
    let widths := [pick 0 256 529, pick 1 64 193, pick 2 32 97, 10]
    (mlpLayers c.kind seed c.batch widths, 1, c.batch * widths.head!)
  | "cnn" => (cnnLayers c.kind seed 3 side (pick 3 4 5) (pick 4 8 9) 10, c.batch, 3 * side * side)
  | "mnet" => (mnetLayers c.kind seed 3 side (pick 3 4 5) (pick 4 8 9) 10, c.batch, 3 * side * side)
  | "nmlp" =>
    let i := pick 0 128 385
    (nmlpLayers c.kind seed c.batch i (pick 1 64 193) (pick 2 32 97) 10, 1, c.batch * i)
  | _ =>
    let T := pick 6 8 9
    let C := pick 7 16 33
    (mixerLayers c.kind seed T C 10, c.batch, T * C)

/-- Run one cell with kernels `K`; the output's hash. `rep` (the repetition number) only reaches
the input through `touch`, so repeated runs are really run. -/
structure PreparedCell where
  layers : List Layer
  samples : Nat
  size : Nat
  input : Array Word

def prepareCell (c : Cell) (seed : UInt32) : PreparedCell :=
  let (layers, samples, size) := cellModel c seed
  ⟨layers, samples, size, gen c.kind (samples * size) (seed + 999)⟩

def runPrepared (K : Kernels) (p : PreparedCell) (rep : Nat) : Array Word :=
  runModel K p.layers p.samples p.size (touch rep p.input)

def runCell (K : Kernels) (c : Cell) (seed : UInt32) (rep : Nat := 0) : UInt32 :=
  hashArr (runPrepared K (prepareCell c seed) rep)

end ExactSuite
