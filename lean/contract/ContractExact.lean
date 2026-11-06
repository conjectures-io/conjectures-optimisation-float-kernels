import ExactToolkit.Values
import ExactToolkit.StableNorm
import ExactToolkit.SqrtProof
import ExactToolkit.Activation
import FloatLib.Floats.Formats.BinaryInterchange.Transcendentals.CorrectlyRounded.Runtime

/-!
# ContractExact: the exact-runtime slot's pinned specs, models and obligation

Scalar operations use FloatLib definitions, `exp` included (FloatLib's correctly rounded
binary32 exponential); normalization uses exact rational statistics and
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
| attn | `scores` | per head, each output a `dotOf` over a query row and a key row; optional causal mask (−∞ above the diagonal) |
| attn | `mix` | per head, each output a `dotOf` over a row of weights and a column of values |
| attn | `softmax` | per row: FloatLib's `maximum`, then `exp (x − max)`, their exact sum, and each `div` by it |
| attn | `silu` | `x · sigmoid x`, one rounding after the correctly rounded `sigmoid` |
| rnn | `lstm` | per step, each gate a `dotOf` over `[x_t; h; 1; 1]` and `[w_ih; w_hh; b_ih; b_hh]`; correctly rounded `sigmoid`/`tanh`; `c' = f·c + i·g` and `h' = o · tanh c'` each rounded once |
| rnn | `gru` | per step, `r`, `z` as LSTM gates; `n = tanh(w_in·x + b_in + r·(w_hn·h + b_hn))` with the inner sum rounded once; `h' = (1 − z)·n + z·h` rounded once |

Each dot-based spec is written once over a dot product (`linearWith dot …`) and the spec is that
definition with `dotOf`; an entry may use the same definitions with a faster proved dot product.

An entry provides one `Kernels` record and proves `Obligation`: each kernel equals its spec. Then
every model runs exactly as the spec models do (`runModel_eq`). Each of the seven families has its
own obligation, for family proofs.
-/

namespace ExactRuntime

open FloatLib.Floats.Formats.BinaryInterchange FloatLib.Floats

/-- A binary32 value, as its 32-bit word. -/
abbrev Word := UInt32

/-- A word as FloatLib's binary32 value. -/
@[inline] def ofW (w : Word) : Model Fmt := Model.ofUInt32 w

/-- ReLU on a word: negative non-NaN values (including -0) become +0. -/
@[inline] def reluW (w : Word) : Word :=
  if w &&& 0x80000000 != 0 && !(expOf w = 255 && fracOf w != 0) then 0 else w

/-- ReLU when `relu` is set; otherwise the word unchanged. -/
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

/-- The `linear` spec: `linearWith` run with FloatLib's dot product. -/
def linearSpec := linearWith dotOf

/-- `C[β, i, j] = Σ_t A[β, i, t] · B[β, t, j]`, batched, row-major. -/
@[specialize] def gemmWith (dot : DotFn) (batch m k n : Nat) (A B : Array Word) : Array Word :=
  Array.ofFn (n := batch * (m * n)) fun q =>
    let β := q.val / (m * n)
    let i := q.val / n % m
    let j := q.val % n
    dot k (fun t => A[(β * m + i) * k + t]!) (fun t => B[(β * k + t) * n + j]!)

/-- The `gemm` spec: `gemmWith` run with FloatLib's dot product. -/
def gemmSpec := gemmWith dotOf

/-! ## Convolution, NCHW, one image -/

/-- A convolution's shape: input channels and size, output channels, filter size, per-axis
stride, zero padding on each side (top, left, bottom, right), per-axis dilation, and groups.
The defaults give an ungrouped, undilated convolution with stride 1 and no padding. -/
structure ConvShape where
  cin : Nat
  h : Nat
  w : Nat
  cout : Nat
  kh : Nat
  kw : Nat
  strideH : Nat := 1
  strideW : Nat := 1
  padT : Nat := 0
  padL : Nat := 0
  padB : Nat := 0
  padR : Nat := 0
  dilH : Nat := 1
  dilW : Nat := 1
  groups : Nat := 1

/-- The common case: square stride `stride` and the same `pad` on every side. -/
def ConvShape.simple (cin h w cout kh kw stride pad : Nat) : ConvShape :=
  { cin, h, w, cout, kh, kw, strideH := stride, strideW := stride,
    padT := pad, padL := pad, padB := pad, padR := pad }

/-- Height of the dilated filter, `dilH·(kh−1)+1`. -/
def ConvShape.ekh (s : ConvShape) : Nat := s.dilH * (s.kh - 1) + 1
/-- Width of the dilated filter, `dilW·(kw−1)+1`. -/
def ConvShape.ekw (s : ConvShape) : Nat := s.dilW * (s.kw - 1) + 1
/-- Output height. -/
def ConvShape.ho (s : ConvShape) : Nat := (s.h + s.padT + s.padB - s.ekh) / s.strideH + 1
/-- Output width. -/
def ConvShape.wo (s : ConvShape) : Nat := (s.w + s.padL + s.padR - s.ekw) / s.strideW + 1
/-- Input channels per group. -/
def ConvShape.cpg (s : ConvShape) : Nat := s.cin / s.groups
/-- Output channels per group. -/
def ConvShape.opg (s : ConvShape) : Nat := s.cout / s.groups
/-- Terms of one output's dot product (before the bias): one group's channels times the filter. -/
def ConvShape.taps (s : ConvShape) : Nat := s.cpg * s.kh * s.kw

/-- The same geometry as a depthwise convolution: one group per input channel, multiplier 1. -/
def ConvShape.depthwise (s : ConvShape) : ConvShape := { s with cout := s.cin, groups := s.cin }

/-- Element `t` of group `g`'s input window of output `(y, x)`, in `(c, dy, dx)` order with `c`
counted within the group; zero in the padding. -/
@[inline] def windowAt (s : ConvShape) (inp : Array Word) (g y x t : Nat) : Word :=
  let c := g * s.cpg + t / (s.kh * s.kw)
  let dy := t / s.kw % s.kh
  let dx := t % s.kw
  let yy := y * s.strideH + dy * s.dilH
  let xx := x * s.strideW + dx * s.dilW
  if yy < s.padT ∨ xx < s.padL ∨ yy - s.padT ≥ s.h ∨ xx - s.padL ≥ s.w then 0
  else inp[(c * s.h + (yy - s.padT)) * s.w + (xx - s.padL)]!

/-- `out[o, y, x] = act(Σ_{c, dy, dx} in[g·cpg + c, …] · w[o, c, dy, dx] + b[o])`, where
`g = o / opg` is the output channel's group; NCHW input, OIHW weights with `I = cin / groups`. -/
@[specialize] def conv2dWith (dot : DotFn) (s : ConvShape) (relu : Bool) (inp wts b : Array Word) :
    Array Word :=
  let k := s.taps
  Array.ofFn (n := s.cout * (s.ho * s.wo)) fun q =>
    let o := q.val / (s.ho * s.wo)
    let y := q.val / s.wo % s.ho
    let x := q.val % s.wo
    let g := o / s.opg
    act relu <| dot (k + 1) (fun t => if t < k then windowAt s inp g y x t else oneW)
      (fun t => if t < k then wts[o * k + t]! else b[o]!)

/-- The `conv2d` spec: `conv2dWith` run with FloatLib's dot product. -/
def conv2dSpec := conv2dWith dotOf

/-- Depthwise: channel `c` of the output uses only channel `c` of the input and its own filter.
It is `conv2d` with `groups = cout = cin`; the shape's own `cout` and `groups` are ignored. -/
@[specialize] def depthwiseWith (dot : DotFn) (s : ConvShape) (relu : Bool)
    (inp wts b : Array Word) : Array Word :=
  conv2dWith dot s.depthwise relu inp wts b

/-- The `depthwise` spec: `depthwiseWith` run with FloatLib's dot product. -/
def depthwiseSpec := depthwiseWith dotOf

/-- A transposed convolution's shape: the convolution's fields read as in ONNX `ConvTranspose`
(`cin, h, w` the input, padding removed from the output), plus extra output rows and columns. -/
structure ConvTShape extends ConvShape where
  outPadH : Nat := 0
  outPadW : Nat := 0

/-- Output height: `strideH·(h−1) + outPadH + ekh − padT − padB`. -/
def ConvTShape.ho (s : ConvTShape) : Nat :=
  s.strideH * (s.h - 1) + s.outPadH + s.ekh - (s.padT + s.padB)
/-- Output width: `strideW·(w−1) + outPadW + ekw − padL − padR`. -/
def ConvTShape.wo (s : ConvTShape) : Nat :=
  s.strideW * (s.w - 1) + s.outPadW + s.ekw - (s.padL + s.padR)

/-- Input position `i` reaches output position `p` through filter tap `d` when
`i·stride + d·dil = p + pad`; the input index, if there is one inside `[0, n)`. -/
@[inline] def tapSource (p pad d dil stride n : Nat) : Option Nat :=
  if p + pad < d * dil then none
  else
    let q := p + pad - d * dil
    if q % stride = 0 ∧ q / stride < n then some (q / stride) else none

/-- The taps `t < taps` (in `(c, dy, dx)` order) that reach output `(y, x)`. -/
def convTTaps (s : ConvTShape) (y x : Nat) : Array Nat :=
  (Array.range s.taps).filter fun t =>
    (tapSource y s.padT (t / s.kw % s.kh) s.dilH s.strideH s.h).isSome &&
      (tapSource x s.padL (t % s.kw) s.dilW s.strideW s.w).isSome

/-- The input value tap `t` of group `g` brings to output `(y, x)` (only read for listed taps). -/
@[inline] def convTInput (s : ConvTShape) (inp : Array Word) (g y x t : Nat) : Word :=
  let c := g * s.cpg + t / (s.kh * s.kw)
  let iy := (tapSource y s.padT (t / s.kw % s.kh) s.dilH s.strideH s.h).getD 0
  let ix := (tapSource x s.padL (t % s.kw) s.dilW s.strideW s.w).getD 0
  inp[(c * s.h + iy) * s.w + ix]!

/-- The weight of tap `t` for output channel `o`; weights are `cin × (cout/groups) × kh × kw`. -/
@[inline] def convTWeight (s : ConvTShape) (wts : Array Word) (o t : Nat) : Word :=
  let g := o / s.opg
  let c := g * s.cpg + t / (s.kh * s.kw)
  wts[((c * s.opg + o % s.opg) * s.kh + t / s.kw % s.kh) * s.kw + t % s.kw]!

/-- `out[o, y, x] = act(Σ in[c, iy, ix] · w[c, o mod opg, dy, dx] + b[o])` over exactly the taps
with `iy·strideH + dy·dilH = y + padT` and `ix·strideW + dx·dilW = x + padL` inside the input,
in `(c, dy, dx)` order, bias last, rounded once. Taps that reach no input are not terms, so they
add no zero (and no `0·∞`). -/
@[specialize] def convTransposeWith (dot : DotFn) (s : ConvTShape) (relu : Bool)
    (inp wts b : Array Word) : Array Word :=
  Array.ofFn (n := s.cout * (s.ho * s.wo)) fun q =>
    let o := q.val / (s.ho * s.wo)
    let y := q.val / s.wo % s.ho
    let x := q.val % s.wo
    let g := o / s.opg
    let taps := convTTaps s y x
    act relu <| dot (taps.size + 1)
      (fun i => if i < taps.size then convTInput s inp g y x taps[i]! else oneW)
      (fun i => if i < taps.size then convTWeight s wts o taps[i]! else b[o]!)

/-- The `convTranspose` spec: `convTransposeWith` run with FloatLib's dot product. -/
def convTransposeSpec := convTransposeWith dotOf

/-! ## Pooling -/

/-- A pooling shape: `c` channels of `h × w`, a `kh × kw` window, per-axis stride, padding on
each side (top, left, bottom, right), the output size rule, and whether average pooling divides
by the window's cells in the padded input (`countIncludePad`) or only its cells in the image. -/
structure PoolShape where
  c : Nat
  h : Nat
  w : Nat
  kh : Nat
  kw : Nat
  strideH : Nat
  strideW : Nat
  padT : Nat := 0
  padL : Nat := 0
  padB : Nat := 0
  padR : Nat := 0
  ceilMode : Bool := false
  countIncludePad : Bool := false

/-- The original pooling: `k × k` windows with stride `k`, no padding. Global pooling of a square
image is `square c n n n`. -/
def PoolShape.square (c h w k : Nat) : PoolShape :=
  { c, h, w, kh := k, kw := k, strideH := k, strideW := k }

/-- Global pooling: one window covering the whole `h × w` image. -/
def PoolShape.global (c h w : Nat) : PoolShape :=
  { c, h, w, kh := h, kw := w, strideH := 1, strideW := 1 }

/-- Output length along one axis of `n` cells, padding `lo`/`hi`, window `k`, stride `st`.
Floor mode keeps whole windows of the padded input. Ceil mode also keeps a last, partial window,
except one that would start in the bottom/right padding (ONNX Runtime's and PyTorch's rule). -/
def poolOut (n lo hi k st : Nat) (ceil : Bool) : Nat :=
  let span := n + lo + hi - k
  if ceil then
    let o := (span + st - 1) / st + 1
    if (o - 1) * st ≥ n + lo then o - 1 else o
  else span / st + 1

/-- Output height. -/
def PoolShape.ho (s : PoolShape) : Nat := poolOut s.h s.padT s.padB s.kh s.strideH s.ceilMode
/-- Output width. -/
def PoolShape.wo (s : PoolShape) : Nat := poolOut s.w s.padL s.padR s.kw s.strideW s.ceilMode

/-- The rows (or columns) `[lo, hi)` of the image under window `i` along an axis: the window
covers padded positions `[i·st, i·st + k)`, that is image positions from `i·st − pad`. -/
@[inline] def poolSpan (i st pad k n : Nat) : Nat × Nat :=
  (i * st - pad, min (i * st + k) (pad + n) - pad)

/-- The number of the window's positions inside the padded input of `pad + n + padHi`. -/
@[inline] def poolPadded (i st k pad n padHi : Nat) : Nat :=
  min (i * st + k) (pad + n + padHi) - i * st

/-- The image cells of output `(ch, y, x)`'s window form a rectangle `rows × cols` with corner
`(y0, x0)`; cell `t` is at `(y0 + t / cols, x0 + t % cols)`. Padding cells are not cells. -/
structure PoolWindow where
  y0 : Nat
  x0 : Nat
  rows : Nat
  cols : Nat

/-- Output `(y, x)`'s window. -/
@[inline] def PoolShape.window (s : PoolShape) (y x : Nat) : PoolWindow :=
  let (y0, y1) := poolSpan y s.strideH s.padT s.kh s.h
  let (x0, x1) := poolSpan x s.strideW s.padL s.kw s.w
  ⟨y0, x0, y1 - y0, x1 - x0⟩

/-- Cell `t` of channel `ch` in window `win`. -/
@[inline] def poolCell (s : PoolShape) (inp : Array Word) (ch : Nat) (win : PoolWindow)
    (t : Nat) : Word :=
  inp[(ch * s.h + win.y0 + t / win.cols) * s.w + win.x0 + t % win.cols]!

/-- Average pooling's divisor for output `(y, x)`: the window's cells in the image, or with
`countIncludePad` its positions in the padded input (positions past it, from ceil mode, never
count). -/
@[inline] def PoolShape.divisor (s : PoolShape) (y x : Nat) : Nat :=
  if s.countIncludePad then
    poolPadded y s.strideH s.kh s.padT s.h s.padB * poolPadded x s.strideW s.kw s.padL s.w s.padR
  else
    let win := s.window y x
    win.rows * win.cols

/-- The exact sum of the window's image cells (row-major), then FloatLib's `div` by the divisor. -/
@[specialize] def avgpoolWith (dot : DotFn) (s : PoolShape) (inp : Array Word) : Array Word :=
  Array.ofFn (n := s.c * (s.ho * s.wo)) fun q =>
    let ch := q.val / (s.ho * s.wo)
    let y := q.val / s.wo % s.ho
    let x := q.val % s.wo
    let win := s.window y x
    let total := dot (win.rows * win.cols) (poolCell s inp ch win) (fun _ => oneW)
    toWord (Model.div (ofW total) (ofW (natW (s.divisor y x))))

/-- The `avgpool` spec: `avgpoolWith` run with FloatLib's dot product. -/
def avgpoolSpec := avgpoolWith dotOf

/-- FloatLib's IEEE `maximum` over the window's image cells, row-major, starting from the first
cell (which is also the first folded in, so a lone signalling NaN is quieted). Padding is not a
value. -/
def maxpoolSpec (s : PoolShape) (inp : Array Word) : Array Word :=
  Array.ofFn (n := s.c * (s.ho * s.wo)) fun q =>
    let ch := q.val / (s.ho * s.wo)
    let y := q.val / s.wo % s.ho
    let x := q.val % s.wo
    let win := s.window y x
    toWord <| (List.range (win.rows * win.cols)).foldl (fun acc t =>
      Model.maximum acc (ofW (poolCell s inp ch win t))) (ofW (poolCell s inp ch win 0))

/-! ## Element-wise -/

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

/-- Stable finite centering and variance; staged semantics outside the stable domain. -/
@[specialize] def layernormWith (dot : DotFn) (rows n : Nat) (eps : Word)
    (x g b : Array Word) : Array Word :=
  if stableNormDomain rows n eps x g && b.size == n && b.all finiteWord then
    layernormStable rows n eps x g b
  else layernormLegacyWith dot rows n eps x g b

/-- The `rmsnorm` spec: stable statistics on its domain, otherwise FloatLib's staged steps. -/
def rmsnormSpec := rmsnormWith dotOf
/-- The `layernorm` spec: stable statistics on its domain, otherwise FloatLib's staged steps. -/
def layernormSpec := layernormWith dotOf

/-! ## Attention and activations (`T` tokens of `heads · d` channels; head `h` owns channels
`h·d … h·d + d - 1`) -/

/-- FloatLib's correctly rounded binary32 exponential on a word: the nearest-even rounding of
`e^x`, proved for every input (`CorrectlyRounded.toReal_exp_eq_roundAt`). -/
@[inline] def expW (w : Word) : Word :=
  toWord (Model.Transcendentals.CorrectlyRounded.exp (ofW w))

/-- `-w`: the sign bit flipped (exact, NaNs included). -/
@[inline] def negW (w : Word) : Word := w ^^^ 0x80000000

/-- Negative infinity, a masked score. -/
abbrev negInfW : Word := 0xFF800000

/-- `S[h, i, j] = Σ_t Q[i, h·d + t] · K[j, h·d + t]`; with `causal`, `-∞` where `j > i`. The
`1/√d` scale belongs in the query weights. -/
@[specialize] def scoresWith (dot : DotFn) (heads T d : Nat) (causal : Bool) (Q K : Array Word) :
    Array Word :=
  let c := heads * d
  Array.ofFn (n := heads * (T * T)) fun q =>
    let h := q.val / (T * T)
    let i := q.val / T % T
    let j := q.val % T
    if causal && j > i then negInfW
    else dot d (fun t => Q[i * c + h * d + t]!) (fun t => K[j * c + h * d + t]!)

/-- The `scores` spec: `scoresWith` run with FloatLib's dot product. -/
def scoresSpec := scoresWith dotOf

/-- `O[i, h·d + t] = Σ_j P[h, i, j] · V[j, h·d + t]`. -/
@[specialize] def mixWith (dot : DotFn) (heads T d : Nat) (P V : Array Word) : Array Word :=
  let c := heads * d
  Array.ofFn (n := T * c) fun q =>
    let i := q.val / c
    let h := q.val % c / d
    let t := q.val % d
    dot T (fun j => P[(h * T + i) * T + j]!) (fun j => V[j * c + h * d + t]!)

/-- The `mix` spec: `mixWith` run with FloatLib's dot product. -/
def mixSpec := mixWith dotOf

/-- Row `r`'s maximum, FloatLib's IEEE `maximum` from left to right. -/
def rowMax (n : Nat) (x : Array Word) (r : Nat) : Word :=
  toWord <| (List.range n).foldl (fun acc t => Model.maximum acc (ofW x[r * n + t]!))
    (ofW x[r * n]!)

/-- Each row: `e = exp (x - max)`, then each `e` divided by the row's exact sum of `e`. -/
@[specialize] def softmaxWith (dot : DotFn) (rows n : Nat) (x : Array Word) : Array Word :=
  let mx := Array.ofFn (n := rows) fun r => rowMax n x r.val
  let e := Array.ofFn (n := rows * n) fun q =>
    expW (toWord (Model.sub (ofW x[q.val]!) (ofW mx[q.val / n]!)))
  let s := Array.ofFn (n := rows) fun r => dot n (fun t => e[r.val * n + t]!) (fun _ => oneW)
  Array.ofFn (n := rows * n) fun q => toWord (Model.div (ofW e[q.val]!) (ofW s[q.val / n]!))

/-- The `softmax` spec: `softmaxWith` run with FloatLib's dot product. -/
def softmaxSpec := softmaxWith dotOf

/-- The logistic sigmoid `1 / (1 + e^(−x))`, correctly rounded (`ExactToolkit.Activation`). -/
abbrev sigmoidW (w : Word) : Word := sigmoidCR w

/-- `tanh x`, correctly rounded (`ExactToolkit.Activation`). -/
abbrev tanhW (w : Word) : Word := tanhCR w

/-- SiLU (swish): `x · sigmoid x`, one more rounding. -/
def siluSpec (x : Array Word) : Array Word :=
  x.map fun w => toWord (Model.mul (ofW w) (ofW (sigmoidW w)))

/-! ## Recurrent layers (`T` steps of `inW` inputs, hidden size `H`, zero initial state)

The input `x` is `T × inW`, row-major (time-major). Weights follow PyTorch's `nn.LSTM`/`nn.GRU`
layout: `wih` is `G·H × inW`, `whh` is `G·H × H`, `bih` and `bhh` are `G·H`, gate blocks in
PyTorch's order (`G = 4`: `i, f, g, o` for the LSTM; `G = 3`: `r, z, n` for the GRU). The output
is every step's hidden state, `T × H`. The initial `h` and `c` are `+0` words, and they are terms
of the dot products like any other value. -/

/-- Pre-activation of gate row `k` at step `t`: the exact dot of `[x_t; h; 1; 1]` with
`[wih[k]; whh[k]; bih[k]; bhh[k]]`, rounded once. -/
@[inline] def gateWith (dot : DotFn) (inW H : Nat) (x wih whh bih bhh h : Array Word)
    (t k : Nat) : Word :=
  dot (inW + H + 2)
    (fun j => if j < inW then x[t * inW + j]! else if j < inW + H then h[j - inW]! else oneW)
    (fun j => if j < inW then wih[k * inW + j]!
      else if j < inW + H then whh[k * H + (j - inW)]!
      else if j = inW + H then bih[k]! else bhh[k]!)

/-- Run `step` for `t = 0, …, T − 1` from `init`; the output is each step's first state component
(the hidden state, `H` words), `T × H`. -/
def recurWith (T H : Nat) (step : Nat → Array Word × Array Word → Array Word × Array Word)
    (init : Array Word × Array Word) : Array Word :=
  let hs := ((List.range T).foldl
    (fun (acc : (Array Word × Array Word) × Array (Array Word)) t =>
      let st := step t acc.1
      (st, acc.2.push st.1)) (init, #[])).2
  Array.ofFn (n := T * H) fun q => (hs[q.val / H]!)[q.val % H]!

/-- One LSTM step on `(h, c)`: gates `i f o = sig a`, `g = tanh a`;
`c' = f·c + i·g` (an exact two-term dot, rounded once); `h' = o · tanh c'` (one rounding). -/
@[specialize] def lstmStepWith (dot : DotFn) (sig tanh : Word → Word) (inW H : Nat)
    (x wih whh bih bhh : Array Word) (t : Nat) (st : Array Word × Array Word) :
    Array Word × Array Word :=
  let pre := Array.ofFn (n := 4 * H) fun k => gateWith dot inW H x wih whh bih bhh st.1 t k.val
  let cell := Array.ofFn (n := H) fun u =>
    dot 2 (fun j => if j = 0 then sig pre[H + u.val]! else sig pre[u.val]!)
      (fun j => if j = 0 then (st.2)[u.val]! else tanh pre[2 * H + u.val]!)
  let hidden := Array.ofFn (n := H) fun u =>
    toWord (Model.mul (ofW (sig pre[3 * H + u.val]!)) (ofW (tanh (cell[u.val]!))))
  (hidden, cell)

/-- An LSTM layer: every step's hidden state, `T × H`. -/
@[specialize] def lstmWith (dot : DotFn) (sig tanh : Word → Word) (T inW H : Nat)
    (x wih whh bih bhh : Array Word) : Array Word :=
  recurWith T H (lstmStepWith dot sig tanh inW H x wih whh bih bhh)
    (Array.replicate H 0, Array.replicate H 0)

/-- One GRU step on `h`: `r z = sig a`; `hn = R(whh[n]·h + bhh[n])`;
`n = tanh (R(wih[n]·x_t + bih[n] + r·hn))` (one exact dot with `hn` as a term);
`h' = R(n − z·n + z·h)` (the exact three-term dot `[n, n, h]·[1, −z, z]`). The second state
component is unused. -/
@[specialize] def gruStepWith (dot : DotFn) (sig tanh : Word → Word) (inW H : Nat)
    (x wih whh bih bhh : Array Word) (t : Nat) (st : Array Word × Array Word) :
    Array Word × Array Word :=
  let h := st.1
  let r := Array.ofFn (n := H) fun u => sig (gateWith dot inW H x wih whh bih bhh h t u.val)
  let z := Array.ofFn (n := H) fun u =>
    sig (gateWith dot inW H x wih whh bih bhh h t (H + u.val))
  let hn := Array.ofFn (n := H) fun u =>
    dot (H + 1) (fun j => if j < H then h[j]! else oneW)
      (fun j => if j < H then whh[(2 * H + u.val) * H + j]! else bhh[2 * H + u.val]!)
  let n := Array.ofFn (n := H) fun u => tanh <| dot (inW + 2)
    (fun j => if j < inW then x[t * inW + j]! else if j = inW then oneW else hn[u.val]!)
    (fun j => if j < inW then wih[(2 * H + u.val) * inW + j]!
      else if j = inW then bih[2 * H + u.val]! else r[u.val]!)
  let h' := Array.ofFn (n := H) fun u =>
    dot 3 (fun j => if j = 2 then h[u.val]! else n[u.val]!)
      (fun j => if j = 0 then oneW else if j = 1 then negW z[u.val]! else z[u.val]!)
  (h', st.2)

/-- A GRU layer: every step's hidden state, `T × H`. -/
@[specialize] def gruWith (dot : DotFn) (sig tanh : Word → Word) (T inW H : Nat)
    (x wih whh bih bhh : Array Word) : Array Word :=
  recurWith T H (gruStepWith dot sig tanh inW H x wih whh bih bhh) (Array.replicate H 0, #[])

/-- The `lstm` spec: FloatLib's dot product and the correctly rounded activations. -/
def lstmSpec := lstmWith dotOf sigmoidW tanhW
/-- The `gru` spec: FloatLib's dot product and the correctly rounded activations. -/
def gruSpec := gruWith dotOf sigmoidW tanhW

/-! ## The kernels, by family -/

/-- The core family: `sum` and `dot`. -/
structure CoreKernels where
  sum : Array Word → Word
  dot : Array Word → Array Word → Word

/-- The dense family: `linear` and `gemm`. -/
structure DenseKernels where
  linear : Nat → Nat → Nat → Bool → Array Word → Array Word → Array Word → Array Word
  gemm : Nat → Nat → Nat → Nat → Array Word → Array Word → Array Word

/-- The convolution family: `conv2d`, `depthwise` and `convTranspose`. -/
structure ConvKernels where
  conv2d : ConvShape → Bool → Array Word → Array Word → Array Word → Array Word
  depthwise : ConvShape → Bool → Array Word → Array Word → Array Word → Array Word
  convTranspose : ConvTShape → Bool → Array Word → Array Word → Array Word → Array Word

/-- The element family: pooling, the residual add and batch norm. -/
structure ElementKernels where
  avgpool : PoolShape → Array Word → Array Word
  maxpool : PoolShape → Array Word → Array Word
  addRelu : Bool → Array Word → Array Word → Array Word
  scaleShift : Bool → Nat → Nat → Array Word → Array Word → Array Word → Array Word

/-- The normalisation family: `rmsnorm` and `layernorm`. -/
structure NormKernels where
  rmsnorm : Nat → Nat → Word → Array Word → Array Word → Array Word
  layernorm : Nat → Nat → Word → Array Word → Array Word → Array Word → Array Word

/-- The attention family: scores, weighted values, softmax and SiLU. -/
structure AttnKernels where
  scores : Nat → Nat → Nat → Bool → Array Word → Array Word → Array Word
  mix : Nat → Nat → Nat → Array Word → Array Word → Array Word
  softmax : Nat → Nat → Array Word → Array Word
  silu : Array Word → Array Word

/-- The attention family's specs. -/
def specAttn : AttnKernels := ⟨scoresSpec, mixSpec, softmaxSpec, siluSpec⟩

/-- A recurrent kernel: `T inW H x wih whh bih bhh`, every step's hidden state. -/
abbrev RecurrentFn :=
  Nat → Nat → Nat → Array Word → Array Word → Array Word → Array Word → Array Word → Array Word

/-- The recurrent family: whole-sequence LSTM and GRU layers. -/
structure RecurrentKernels where
  lstm : RecurrentFn
  gru : RecurrentFn

/-- The recurrent family's specs. -/
def specRnn : RecurrentKernels := ⟨lstmSpec, gruSpec⟩

/-- An entry's 19 kernels, by family. `attn` and `rnn` default to the specs, so an entry that
doesn't name them still compiles and proves its obligation. -/
structure Kernels where
  core : CoreKernels
  dense : DenseKernels
  conv : ConvKernels
  elem : ElementKernels
  norm : NormKernels
  attn : AttnKernels := specAttn
  rnn : RecurrentKernels := specRnn

/-- The core family's specs. -/
def specCore : CoreKernels := ⟨sumSpec, dotSpec⟩
/-- The dense family's specs. -/
def specDense : DenseKernels := ⟨linearSpec, gemmSpec⟩
/-- The convolution family's specs. -/
def specConv : ConvKernels := ⟨conv2dSpec, depthwiseSpec, convTransposeSpec⟩
/-- The element family's specs. -/
def specElem : ElementKernels := ⟨avgpoolSpec, maxpoolSpec, addReluSpec, scaleShiftSpec⟩
/-- The normalisation family's specs. -/
def specNorm : NormKernels := ⟨rmsnormSpec, layernormSpec⟩
/-- All 19 specs: what an entry's kernels must equal. -/
def specKernels : Kernels := ⟨specCore, specDense, specConv, specElem, specNorm, specAttn, specRnn⟩

/-- The obligation an entry proves: each of its kernels equals the spec. -/
def Obligation (K : Kernels) : Prop := K = specKernels

/-- The core family equals its specs (family obligations are for family boards). -/
def CoreObligation (k : CoreKernels) : Prop := k = specCore
/-- The dense family equals its specs. -/
def DenseObligation (k : DenseKernels) : Prop := k = specDense
/-- The convolution family equals its specs. -/
def ConvObligation (k : ConvKernels) : Prop := k = specConv
/-- The element family equals its specs. -/
def ElementObligation (k : ElementKernels) : Prop := k = specElem
/-- The normalisation family equals its specs. -/
def NormObligation (k : NormKernels) : Prop := k = specNorm
/-- The attention family equals its specs. -/
def AttnObligation (k : AttnKernels) : Prop := k = specAttn
/-- The recurrent family equals its specs. -/
def RnnObligation (k : RecurrentKernels) : Prop := k = specRnn

/-- The seven families' proofs together meet the whole obligation. -/
theorem obligation_of_families (K : Kernels) (h1 : CoreObligation K.core)
    (h2 : DenseObligation K.dense) (h3 : ConvObligation K.conv) (h4 : ElementObligation K.elem)
    (h5 : NormObligation K.norm) (h6 : AttnObligation K.attn) (h7 : RnnObligation K.rnn) :
    Obligation K := by
  obtain ⟨c, d, v, e, n, a, r⟩ := K
  simp only [CoreObligation, DenseObligation, ConvObligation, ElementObligation,
    NormObligation, AttnObligation, RnnObligation] at h1 h2 h3 h4 h5 h6 h7
  subst h1 h2 h3 h4 h5 h6 h7
  rfl

/-! ## Models: a list of layers, run on each sample -/

/-- One layer of a model; `save` and `addSaved` make residual connections. `attention` is
multi-head self-attention on `T` tokens: projections by `linear`, then `scores`, `softmax`, `mix`
and the output projection. `lstm` and `gru` turn `T × inW` into every step's `T × H` hidden state;
`lastStep T n` keeps the last of `T` rows of `n` words (no arithmetic). -/
inductive Layer where
  | linear (rows inW outW : Nat) (relu : Bool) (w b : Array Word)
  | gemm (m k n : Nat) (a : Array Word)       -- left-multiply the activation (`k × n`) by `a`
  | conv (s : ConvShape) (relu : Bool) (w b : Array Word)
  | depthwise (s : ConvShape) (relu : Bool) (w b : Array Word)
  | convTranspose (s : ConvTShape) (relu : Bool) (w b : Array Word)
  | bn (relu : Bool) (c hw : Nat) (s t : Array Word)
  | maxpool (s : PoolShape)
  | avgpool (s : PoolShape)
  | rmsnorm (rows n : Nat) (eps : Word) (g : Array Word)
  | layernorm (rows n : Nat) (eps : Word) (g b : Array Word)
  | save                                      -- remember the activation
  | addSaved (relu : Bool)                    -- add the remembered activation (residual)
  | attention (T heads d : Nat) (causal : Bool) (wq bq wk bk wv bv wo bo : Array Word)
  | silu
  | lstm (T inW H : Nat) (wih whh bih bhh : Array Word)
  | gru (T inW H : Nat) (wih whh bih bhh : Array Word)
  | lastStep (T n : Nat)

/-- One layer on `(activation, saved)`. -/
def Layer.apply (K : Kernels) : Layer → Array Word × Array Word → Array Word × Array Word
  | .linear rows inW outW relu w b, (x, s) => (K.dense.linear rows inW outW relu x w b, s)
  | .gemm m k n a, (x, s) => (K.dense.gemm 1 m k n a x, s)
  | .conv sh relu w b, (x, s) => (K.conv.conv2d sh relu x w b, s)
  | .depthwise sh relu w b, (x, s) => (K.conv.depthwise sh relu x w b, s)
  | .convTranspose sh relu w b, (x, s) => (K.conv.convTranspose sh relu x w b, s)
  | .bn relu c hw sc t, (x, s) => (K.elem.scaleShift relu c hw x sc t, s)
  | .maxpool sh, (x, s) => (K.elem.maxpool sh x, s)
  | .avgpool sh, (x, s) => (K.elem.avgpool sh x, s)
  | .rmsnorm rows n eps g, (x, s) => (K.norm.rmsnorm rows n eps x g, s)
  | .layernorm rows n eps g b, (x, s) => (K.norm.layernorm rows n eps x g b, s)
  | .save, (x, _) => (x, x)
  | .addSaved relu, (x, s) => (K.elem.addRelu relu x s, s)
  | .attention T heads d causal wq bq wk bk wv bv wo bo, (x, s) =>
    let c := heads * d
    let q := K.dense.linear T c c false x wq bq
    let k := K.dense.linear T c c false x wk bk
    let v := K.dense.linear T c c false x wv bv
    let p := K.attn.softmax (heads * T) T (K.attn.scores heads T d causal q k)
    (K.dense.linear T c c false (K.attn.mix heads T d p v) wo bo, s)
  | .silu, (x, s) => (K.attn.silu x, s)
  | .lstm T inW H wih whh bih bhh, (x, s) => (K.rnn.lstm T inW H x wih whh bih bhh, s)
  | .gru T inW H wih whh bih bhh, (x, s) => (K.rnn.gru T inW H x wih whh bih bhh, s)
  | .lastStep T n, (x, s) => (x.extract ((T - 1) * n) (T * n), s)

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
