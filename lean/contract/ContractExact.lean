import ExactToolkit.Values
import ExactToolkit.StableNorm
import ExactToolkit.SqrtProof

/-!
# ContractExact: the exact-runtime slot's pinned specs, models and obligation

Scalar operations use FloatLib definitions; normalization uses exact rational statistics and
a proved square-root rounding bridge. Most kernels are dot products: `dotOf n fa fb` is
FloatLib's correctly rounded dot product of `[fa 0, …, fa (n-1)]` and `[fb 0, …, fb (n-1)]` (the
exact sum of the products, rounded once), and a kernel says where its operands come from.

| family | kernel | spec |
|---|---|---|
| core | `sum`, `dot` | `sumSpec`, `dotSpec` (toolkit) |
| dense | `linear` | each output a `dotOf` over an input row and a weight row, with the bias as one more exact term (`b·1`); optional ReLU |
| dense | `gemm` | batched matrix product; each output a `dotOf` over a row and a column |
| conv | `conv2d` | each output a `dotOf` over its input window (all channels) and its filter, bias included; optional ReLU |
| conv | `depthwise` | the same per channel, each channel with its own filter |
| element | `avgpool` | `dotOf` the window with ones (its exact sum), then FloatLib's `div` by the window size |
| element | `maxpool` | FloatLib's IEEE `maximum` over each window |
| element | `addRelu` | FloatLib's `add` per element, optional ReLU |
| element | `scaleShift` | FloatLib's `fma` per element (`x·s + t`, one rounding), per channel |
| norm | `rmsnorm` | exact mean square; round the square-root denominator, then the final scaled quotient |
| norm | `layernorm` | exact mean, centering and variance; round the denominator, then the final affine expression |

Each dot-based spec is written once over a dot product (`linearWith dot …`) and the spec is that
definition with `dotOf`; an entry may use the same definitions with a faster proved dot product.

An entry provides one `Kernels` record and proves `Obligation`: each kernel equals its spec. Then
every model runs exactly as the spec models do (`runModel_eq`). Each family also has its own
obligation, for family boards.
-/

namespace ExactRuntime

open FloatLib.Floats.Formats.BinaryInterchange FloatLib.Floats

abbrev Word := UInt32

@[inline] def ofW (w : Word) : Model Fmt := Model.ofUInt32 w

/-- ReLU on a word: negative non-NaN values (including -0) become +0. -/
@[inline] def reluW (w : Word) : Word :=
  if w &&& 0x80000000 != 0 && !(expOf w = 255 && fracOf w != 0) then 0 else w

@[inline] def act (relu : Bool) (w : Word) : Word := if relu then reluW w else w

/-- FloatLib's correctly rounded dot product of `[fa 0, …, fa (n-1)]` and `[fb 0, …, fb (n-1)]`. -/
def dotOf (n : Nat) (fa fb : Nat → Word) : Word :=
  dotSpec (Array.ofFn (n := n) fun i => fa i.val) (Array.ofFn (n := n) fun i => fb i.val)

/-- A dot product of `n` terms given by two functions; `dotOf` is the spec. -/
abbrev DotFn := Nat → (Nat → Word) → (Nat → Word) → Word

/-- `n` as a binary32 word (exact for `n ≤ 2^24`). -/
def natW (n : Nat) : Word := toWord (Model.roundDyadic Fmt ⟨false, n, 0⟩)

/-! ## Dense -/

/-- `Y[i, j] = act(Σ_t X[i, t] · W[j, t] + b[j])`, `rows × inW` times `outW × inW`. -/
@[specialize] def linearWith (dot : DotFn) (rows inW outW : Nat) (relu : Bool) (X W b : Array Word) :
    Array Word :=
  Array.ofFn (n := rows * outW) fun q =>
    let i := q.val / outW
    let j := q.val % outW
    act relu <| dot (inW + 1) (fun t => if t < inW then X[i * inW + t]! else oneW)
      (fun t => if t < inW then W[j * inW + t]! else b[j]!)

def linearSpec := linearWith dotOf

/-- `C[β, i, j] = Σ_t A[β, i, t] · B[β, t, j]`, batched, row-major. -/
@[specialize] def gemmWith (dot : DotFn) (batch m k n : Nat) (A B : Array Word) : Array Word :=
  Array.ofFn (n := batch * (m * n)) fun q =>
    let β := q.val / (m * n)
    let i := q.val / n % m
    let j := q.val % n
    dot k (fun t => A[(β * m + i) * k + t]!) (fun t => B[(β * k + t) * n + j]!)

def gemmSpec := gemmWith dotOf

/-! ## Convolution, NCHW, one image -/

structure ConvShape where
  cin : Nat
  h : Nat
  w : Nat
  cout : Nat
  kh : Nat
  kw : Nat
  stride : Nat
  pad : Nat

def ConvShape.ho (s : ConvShape) : Nat := (s.h + 2 * s.pad - s.kh) / s.stride + 1
def ConvShape.wo (s : ConvShape) : Nat := (s.w + 2 * s.pad - s.kw) / s.stride + 1

/-- Element `t` of the input window of output `(y, x)`, in `(c, dy, dx)` order; zero outside the
image. -/
@[inline] def windowAt (s : ConvShape) (inp : Array Word) (y x t : Nat) : Word :=
  let c := t / (s.kh * s.kw)
  let dy := t / s.kw % s.kh
  let dx := t % s.kw
  let yy := y * s.stride + dy
  let xx := x * s.stride + dx
  if yy < s.pad ∨ xx < s.pad ∨ yy - s.pad ≥ s.h ∨ xx - s.pad ≥ s.w then 0
  else inp[(c * s.h + (yy - s.pad)) * s.w + (xx - s.pad)]!

/-- `out[o, y, x] = act(Σ_{c, dy, dx} in[c, …] · w[o, c, dy, dx] + b[o])`. -/
@[specialize] def conv2dWith (dot : DotFn) (s : ConvShape) (relu : Bool) (inp wts b : Array Word) :
    Array Word :=
  let k := s.cin * s.kh * s.kw
  Array.ofFn (n := s.cout * (s.ho * s.wo)) fun q =>
    let o := q.val / (s.ho * s.wo)
    let y := q.val / s.wo % s.ho
    let x := q.val % s.wo
    act relu <| dot (k + 1) (fun t => if t < k then windowAt s inp y x t else oneW)
      (fun t => if t < k then wts[o * k + t]! else b[o]!)

def conv2dSpec := conv2dWith dotOf

/-- Depthwise: channel `c` of the output uses only channel `c` of the input and its own filter
(`cout` is ignored; the output has `cin` channels). -/
@[specialize] def depthwiseWith (dot : DotFn) (s : ConvShape) (relu : Bool)
    (inp wts b : Array Word) : Array Word :=
  let k := s.kh * s.kw
  Array.ofFn (n := s.cin * (s.ho * s.wo)) fun q =>
    let c := q.val / (s.ho * s.wo)
    let y := q.val / s.wo % s.ho
    let x := q.val % s.wo
    act relu <| dot (k + 1) (fun t => if t < k then windowAt s inp y x (c * k + t) else oneW)
      (fun t => if t < k then wts[c * k + t]! else b[c]!)

def depthwiseSpec := depthwiseWith dotOf

/-! ## Element-wise and pooling (`c` channels of `h × w`, `k × k` windows, stride `k`) -/

/-- Element `t` of the pooling window of output `(ch, y, x)`. -/
@[inline] def poolAt (h w k : Nat) (inp : Array Word) (ch y x t : Nat) : Word :=
  inp[(ch * h + y * k + t / k) * w + x * k + t % k]!

/-- The window's exact sum, then FloatLib's `div` by `k²`. Global average pooling: `k = h = w`. -/
@[specialize] def avgpoolWith (dot : DotFn) (c h w k : Nat) (inp : Array Word) : Array Word :=
  Array.ofFn (n := c * (h / k * (w / k))) fun q =>
    let ch := q.val / (h / k * (w / k))
    let y := q.val / (w / k) % (h / k)
    let x := q.val % (w / k)
    let s := dot (k * k) (poolAt h w k inp ch y x) (fun _ => oneW)
    toWord (Model.div (ofW s) (ofW (natW (k * k))))

def avgpoolSpec := avgpoolWith dotOf

/-- FloatLib's IEEE `maximum` over each window, left to right. -/
def maxpoolSpec (c h w k : Nat) (inp : Array Word) : Array Word :=
  Array.ofFn (n := c * (h / k * (w / k))) fun q =>
    let ch := q.val / (h / k * (w / k))
    let y := q.val / (w / k) % (h / k)
    let x := q.val % (w / k)
    toWord <| (List.range (k * k)).foldl (fun acc t =>
      Model.maximum acc (ofW (poolAt h w k inp ch y x t))) (ofW (poolAt h w k inp ch y x 0))

/-- Residual add with optional ReLU. -/
def addReluSpec (relu : Bool) (a b : Array Word) : Array Word :=
  Array.ofFn (n := a.size) fun i => act relu <| toWord (Model.add (ofW a[i]) (ofW b[i.val]!))

/-- Inference batch norm: `x·s[ch] + t[ch]` with one rounding, then optional ReLU. -/
def scaleShiftSpec (relu : Bool) (c hw : Nat) (x s t : Array Word) : Array Word :=
  Array.ofFn (n := c * hw) fun i =>
    let ch := i.val / hw
    act relu <| toWord (Model.fma (ofW x[i.val]!) (ofW s[ch]!) (ofW t[ch]!))

/-! ## Normalisation (`rows` rows of `n` values; `g`, `b` per position in a row) -/

/-- `1 / sqrt(v / n + eps)`, FloatLib's correctly rounded steps in this order. -/
def invStd (n : Nat) (eps v : Word) : Word :=
  toWord <| Model.div (ofW oneW)
    (Model.sqrt (Model.add (Model.div (ofW v) (ofW (natW n))) (ofW eps)))

/-- `y = (x · scale[row]) · g`, where a row's scale is `invStd` of its exact sum of squares. -/
@[specialize] def rmsnormLegacyWith (dot : DotFn) (rows n : Nat) (eps : Word) (x g : Array Word) :
    Array Word :=
  let scale := Array.ofFn (n := rows) fun r =>
    invStd n eps (dot n (fun t => x[r.val * n + t]!) (fun t => x[r.val * n + t]!))
  Array.ofFn (n := rows * n) fun q =>
    toWord <| Model.mul (Model.mul (ofW x[q.val]!) (ofW scale[q.val / n]!)) (ofW g[q.val % n]!)

/-- `x - mean` for element `q`. -/
@[inline] def centered (x : Array Word) (mean : Word) (q : Nat) : Word :=
  toWord <| Model.sub (ofW x[q]!) (ofW mean)

/-- `y = ((x - mean[row]) · scale[row]) · g + b`, where a row's mean is its exact sum divided by
`n`, and its scale is `invStd` of the exact sum of squares of the centred row. -/
@[specialize] def layernormLegacyWith (dot : DotFn) (rows n : Nat) (eps : Word) (x g b : Array Word) :
    Array Word :=
  let mean := Array.ofFn (n := rows) fun r =>
    toWord <| Model.div (ofW (dot n (fun t => x[r.val * n + t]!) (fun _ => oneW))) (ofW (natW n))
  let scale := Array.ofFn (n := rows) fun r =>
    invStd n eps (dot n (fun t => centered x mean[r.val]! (r.val * n + t))
      (fun t => centered x mean[r.val]! (r.val * n + t)))
  Array.ofFn (n := rows * n) fun q =>
    toWord <| Model.add
      (Model.mul (Model.mul (ofW (centered x mean[q.val / n]! q.val)) (ofW scale[q.val / n]!))
        (ofW g[q.val % n]!))
      (ofW b[q.val % n]!)

/-- Stable finite statistics, with the previous staged semantics outside the stable domain. -/
@[specialize] def rmsnormWith (dot : DotFn) (rows n : Nat) (eps : Word)
    (x g : Array Word) : Array Word :=
  if stableNormDomain rows n eps x g then rmsnormStable rows n eps x g
  else rmsnormLegacyWith dot rows n eps x g

@[specialize] def layernormWith (dot : DotFn) (rows n : Nat) (eps : Word)
    (x g b : Array Word) : Array Word :=
  if stableNormDomain rows n eps x g && b.size == n && b.all finiteWord then
    layernormStable rows n eps x g b
  else layernormLegacyWith dot rows n eps x g b

def rmsnormSpec := rmsnormWith dotOf
def layernormSpec := layernormWith dotOf

/-! ## The kernels, by family -/

structure CoreKernels where
  sum : Array Word → Word
  dot : Array Word → Array Word → Word

structure DenseKernels where
  linear : Nat → Nat → Nat → Bool → Array Word → Array Word → Array Word → Array Word
  gemm : Nat → Nat → Nat → Nat → Array Word → Array Word → Array Word

structure ConvKernels where
  conv2d : ConvShape → Bool → Array Word → Array Word → Array Word → Array Word
  depthwise : ConvShape → Bool → Array Word → Array Word → Array Word → Array Word

structure ElementKernels where
  avgpool : Nat → Nat → Nat → Nat → Array Word → Array Word
  maxpool : Nat → Nat → Nat → Nat → Array Word → Array Word
  addRelu : Bool → Array Word → Array Word → Array Word
  scaleShift : Bool → Nat → Nat → Array Word → Array Word → Array Word → Array Word

structure NormKernels where
  rmsnorm : Nat → Nat → Word → Array Word → Array Word → Array Word
  layernorm : Nat → Nat → Word → Array Word → Array Word → Array Word → Array Word

structure Kernels where
  core : CoreKernels
  dense : DenseKernels
  conv : ConvKernels
  elem : ElementKernels
  norm : NormKernels

def specCore : CoreKernels := ⟨sumSpec, dotSpec⟩
def specDense : DenseKernels := ⟨linearSpec, gemmSpec⟩
def specConv : ConvKernels := ⟨conv2dSpec, depthwiseSpec⟩
def specElem : ElementKernels := ⟨avgpoolSpec, maxpoolSpec, addReluSpec, scaleShiftSpec⟩
def specNorm : NormKernels := ⟨rmsnormSpec, layernormSpec⟩
def specKernels : Kernels := ⟨specCore, specDense, specConv, specElem, specNorm⟩

/-- The obligation an entry proves: each of its kernels equals the spec. -/
def Obligation (K : Kernels) : Prop := K = specKernels

/-- Family obligations, for family boards. -/
def CoreObligation (k : CoreKernels) : Prop := k = specCore
def DenseObligation (k : DenseKernels) : Prop := k = specDense
def ConvObligation (k : ConvKernels) : Prop := k = specConv
def ElementObligation (k : ElementKernels) : Prop := k = specElem
def NormObligation (k : NormKernels) : Prop := k = specNorm

/-- Champions of the five families together meet the whole obligation. -/
theorem obligation_of_families (K : Kernels) (h1 : CoreObligation K.core)
    (h2 : DenseObligation K.dense) (h3 : ConvObligation K.conv) (h4 : ElementObligation K.elem)
    (h5 : NormObligation K.norm) : Obligation K := by
  obtain ⟨c, d, v, e, n⟩ := K
  simp only [CoreObligation, DenseObligation, ConvObligation, ElementObligation,
    NormObligation] at h1 h2 h3 h4 h5
  subst h1 h2 h3 h4 h5
  rfl

/-! ## Models: a list of layers, run on each sample -/

inductive Layer where
  | linear (rows inW outW : Nat) (relu : Bool) (w b : Array Word)
  | gemm (m k n : Nat) (a : Array Word)       -- left-multiply the activation (`k × n`) by `a`
  | conv (s : ConvShape) (relu : Bool) (w b : Array Word)
  | depthwise (s : ConvShape) (relu : Bool) (w b : Array Word)
  | bn (relu : Bool) (c hw : Nat) (s t : Array Word)
  | maxpool (c h w k : Nat)
  | avgpool (c h w k : Nat)
  | rmsnorm (rows n : Nat) (eps : Word) (g : Array Word)
  | layernorm (rows n : Nat) (eps : Word) (g b : Array Word)
  | save                                      -- remember the activation
  | addSaved (relu : Bool)                    -- add the remembered activation (residual)

/-- One layer on `(activation, saved)`. -/
def Layer.apply (K : Kernels) : Layer → Array Word × Array Word → Array Word × Array Word
  | .linear rows inW outW relu w b, (x, s) => (K.dense.linear rows inW outW relu x w b, s)
  | .gemm m k n a, (x, s) => (K.dense.gemm 1 m k n a x, s)
  | .conv sh relu w b, (x, s) => (K.conv.conv2d sh relu x w b, s)
  | .depthwise sh relu w b, (x, s) => (K.conv.depthwise sh relu x w b, s)
  | .bn relu c hw sc t, (x, s) => (K.elem.scaleShift relu c hw x sc t, s)
  | .maxpool c h w k, (x, s) => (K.elem.maxpool c h w k x, s)
  | .avgpool c h w k, (x, s) => (K.elem.avgpool c h w k x, s)
  | .rmsnorm rows n eps g, (x, s) => (K.norm.rmsnorm rows n eps x g, s)
  | .layernorm rows n eps g b, (x, s) => (K.norm.layernorm rows n eps x g b, s)
  | .save, (x, _) => (x, x)
  | .addSaved relu, (x, s) => (K.elem.addRelu relu x s, s)

/-- Run `layers` on each of `samples` consecutive inputs of `size` words; concatenate outputs. -/
def runModel (K : Kernels) (layers : List Layer) (samples size : Nat) (x : Array Word) :
    Array Word :=
  (List.range samples).foldl (fun out i =>
    out ++ (layers.foldl (fun st l => l.apply K st)
      (x.extract (i * size) (i * size + size), #[])).1) #[]

/-- Any accepted entry runs every model exactly as the spec models do. -/
theorem runModel_eq (K : Kernels) (h : Obligation K) (layers : List Layer) (samples size : Nat)
    (x : Array Word) : runModel K layers samples size x = runModel specKernels layers samples size x := by
  rw [h]

end ExactRuntime
