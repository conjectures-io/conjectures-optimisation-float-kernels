# Kernel contract

The [pinned contract](../lean/contract/ContractExact.lean) defines the 19 kernels, model execution
and `Obligation K := K = specKernels`. The [checked interface](../lean/contract/ContractExactChecked.lean)
adds tensor shape/domain checks. Every submitted implementation must match the raw specification
universally, including behavior outside the checked interface's accepted domain.

## Arithmetic and layouts

Inputs and outputs are binary32 words. Dot products compute the exact sum of exact products,
then round once to nearest, ties to even. Linear and convolution include the bias as an exact term
before that rounding. ReLU acts on the rounded output: negative non-NaNs, including −0, become +0.

| Kernel | Semantics |
|---|---|
| `dot`, `sum` | FloatLib exact dot; sum is a dot with ones. |
| `linear` | Row-major activations, output-major weight rows, exact dot plus bias; optional ReLU. |
| `gemm` | Batched matrix multiplication; exact row/column dot per output. |
| `conv2d` | NCHW input, OIHW weights; cross-correlation without reversing filters. Groups, dilation, per-axis stride and per-side zero padding (below). |
| `depthwise` | `conv2d` with `groups = cout = cin`: one filter per input channel, multiplier one. |
| `convTranspose` | ONNX `ConvTranspose`: each output is the exact dot of the taps that reach it, plus bias (below). |
| `avgpool` | Exact sum of the window's image cells, rounded once, then FloatLib division by the cell count (below). |
| `maxpool` | Ordered fold of FloatLib's IEEE maximum over the window's image cells. |
| `addRelu` | FloatLib addition per element, then optional ReLU. |
| `scaleShift` | Channelwise FloatLib fused multiply-add: `x * scale + shift`, one rounding. |
| `rmsnorm`, `layernorm` | Stable rational statistics with the boundaries below; specified fallback outside that domain. |
| `scores` | Attention scores per head: exact dot of a query row and a key row; with `causal`, `−∞` above the diagonal. The `1/√d` scale belongs in the query weights. |
| `mix` | Per head, exact dot of a row of attention weights and a column of values. |
| `softmax` | Per row: IEEE maximum, `exp(x − max)`, the exact sum, then FloatLib division of each term by it. A masked (`−∞`) entry gives +0. |
| `silu` | `x · sigmoid(x)`, one rounding after the correctly rounded sigmoid (below). |
| `lstm`, `gru` | Whole-sequence recurrent layers with exact gate dot products and correctly rounded `sigmoid`/`tanh` (below). |

`exp` is FloatLib's correctly rounded binary32 exponential (`CorrectlyRounded.exp`): the
nearest-even rounding of `e^x` for every input, proved in FloatLib. `sigmoid` and `tanh` are the
nearest-even binary32 roundings of `1 / (1 + e^(−x))` and `tanh x` for every finite input
([`ExactToolkit/Activation.lean`](../lean/contract/ExactToolkit/Activation.lean), proved in
`ActivationProof.lean`). A NaN is quieted with its payload kept; `tanh(±∞) = ±1`,
`tanh(±0) = ±0`; `sigmoid(+∞) = 1`, `sigmoid(−∞) = +0`, `sigmoid(±0) = 1/2`. The `attn` and `rnn`
families' fields have defaults, the specs, so an entry that does not name them still compiles and
proves its obligation; those kernels are then the (slow) specs.

This is not a claim that every operation has only one rounding. Average pooling and normalization
explicitly have several fixed steps. NaNs, infinities, payload handling and signed zeros follow
the executable definitions. Exact cancellation gives +0; an empty dot is +0; negative-zero-only
nonempty reductions retain −0. A convolution tap in the zero padding is a +0 term, so it
takes part in these rules (`+0·∞` is NaN; a +0 product makes an otherwise −0 sum +0). Transposed
convolution taps that reach no input, and pooling padding, are not terms or values at all.
See the [mathematical proofs](MATHEMATICAL-PROOFS.md).

## Convolution and pooling shapes

A `ConvShape` has `cin h w cout kh kw` and `strideH strideW padT padL padB padR dilH dilW groups`,
with defaults 1, 1, 0, 0, 0, 0, 1, 1, 1. `ConvShape.simple cin h w cout kh kw stride pad` is the
square case with the same padding on every side. Padding is explicit per side; a 1-D convolution
has `h = kh = 1`. The dilated filter is `ekh = dilH·(kh−1)+1` by `ekw = dilW·(kw−1)+1`.

**`conv2d`.** Weights are `cout × (cin/groups) × kh × kw`; bias `cout`. Output channel `o` belongs
to group `g = o / (cout/groups)` and reads only that group's `cin/groups` input channels,
starting at channel `g·(cin/groups)`. Tap `(c, dy, dx)` of output `(o, y, x)` reads row
`y·strideH + dy·dilH − padT` and column `x·strideW + dx·dilW − padL` of the group's channel `c`. The terms are taken in `(c, dy, dx)` order, bias last, rounded once,
then optional ReLU. A tap in the padding is a +0 term: it is multiplied like any other value.
Output size:

```text
ho = (h + padT + padB − ekh) / strideH + 1        (floor)
wo = (w + padL + padR − ekw) / strideW + 1
```

**`depthwise`** runs `conv2d` on the same shape with `cout = groups = cin`; the shape's own `cout`
and `groups` are ignored. Weights `cin × kh × kw`, bias `cin`, `cin` output channels.

**`convTranspose`** takes a `ConvTShape`: a `ConvShape` read as in ONNX `ConvTranspose`, plus
`outPadH outPadW` (default 0). `cin h w` describe the input; padding is removed from the output.
Weights are `cin × (cout/groups) × kh × kw`; bias `cout`. Output `(o, y, x)` sums
`in[c, iy, ix] · w[c, o mod (cout/groups), dy, dx]` over the channels `c` of `o`'s group and the
taps with

```text
iy·strideH + dy·dilH = y + padT,   0 ≤ iy < h
ix·strideW + dx·dilW = x + padL,   0 ≤ ix < w
```

in `(c, dy, dx)` order, bias last, rounded once, then optional ReLU. Taps that reach no input are
not terms: they add no zero, so they cannot produce `0·∞ = NaN` or change the sign of a zero
result. An output that no tap reaches is the dot of the bias alone. Output size:

```text
ho = strideH·(h−1) + outPadH + ekh − padT − padB
wo = strideW·(w−1) + outPadW + ekw − padL − padR
```

**Pooling** takes a `PoolShape`: `c h w kh kw strideH strideW`, `padT padL padB padR` (default 0),
`ceilMode` and `countIncludePad` (default false). `PoolShape.square c h w k` is a `k × k` window
with stride `k` and no padding. `PoolShape.global c h w` is one window over the whole `h × w`
image. Along each axis, with `n` cells, padding `lo`/`hi`, window `k` and stride `st`:

```text
floor mode: out = (n + lo + hi − k) / st + 1
ceil mode:  out = ceil((n + lo + hi − k) / st) + 1, less one if the last window would start
            at or after n + lo (in the bottom/right padding)
```

This is the ONNX Runtime and PyTorch rule. Window `i` covers padded positions `[i·st, i·st + k)`.
Its cells are the image cells inside that range (padded position `p` is image position `p − lo`):
a `rows × cols` rectangle, read row-major. Padding is never a value.

- `maxpool` folds FloatLib's IEEE `maximum` over those cells, starting from the first cell (which
  is also folded in first, so a lone signalling NaN comes out quiet).
- `avgpool` rounds the exact sum of those cells once, then divides with FloatLib `div` by
  `rows·cols`. With `countIncludePad`, the divisor is instead the number of window positions inside
  the padded input (`lo + n + hi` cells per axis); positions past it, from ceil mode, never
  count.

## Recurrent layers

`lstm` and `gru` take `T inW H x wih whh bih bhh`: `T` steps of `inW` inputs, row-major
(time-major), and hidden size `H`. Weights use PyTorch's `nn.LSTM`/`nn.GRU` layouts: `wih` is
`G·H × inW`, `whh` is `G·H × H`, `bih` and `bhh` are `G·H`, with the gate blocks in PyTorch's
order (`G = 4`: `i, f, g, o`; `G = 3`: `r, z, n`). The initial `h` (and LSTM `c`) is `+0`, and
those zeros are dot terms like any other value. The output is every step's `h`, `T × H`. With
`R` the nearest-even rounding and `σ`, `tanh` the correctly rounded activations above:

```text
gate k:  a_k = R(Σ_j wih[k, j]·x_t[j] + Σ_j whh[k, j]·h[j] + bih[k] + bhh[k])   (one exact dot)

LSTM:    i, f, o = σ(a_i), σ(a_f), σ(a_o);   g = tanh(a_g)
         c' = R(f·c + i·g)                                                       (exact, one rounding)
         h' = R(o · tanh(c'))

GRU:     r, z = σ(a_r), σ(a_z)
         hn = R(Σ_j whh[n, j]·h[j] + bhh[n])
         n  = tanh(R(Σ_j wih[n, j]·x_t[j] + bih[n] + r·hn))                     (one exact dot)
         h' = R(n − z·n + z·h)                                                    (exact, one rounding)
```

Dot terms are taken in the order written (inputs, hidden state, then biases), which fixes the
zero-sign and NaN rules as for every other dot. The GRU is PyTorch's (`linear_before_reset`:
`r` multiplies the rounded `hn`). `Layer.lastStep T n` keeps the last of `T` rows of `n` words
(no arithmetic); a recurrent classifier is `lstm`/`gru` layers, `lastStep`, then `linear`.

## Stable normalization

For finite inputs/gains/biases, positive finite epsilon and valid positive dimensions, statistics
are exact rationals. Let `R` round to binary32, nearest-even:

```text
RMS norm:
    v = sum(x_i²) / n
    d = R(sqrt(v + epsilon))
    y_i = R(x_i * gain_i / d)

Layer norm:
    mean = sum(x_i) / n
    centered_i = x_i - mean
    v = sum(centered_i²) / n
    d = R(sqrt(v + epsilon))
    y_i = R(centered_i * gain_i / d + bias_i)
```

All expressions before `R` are exact. The rounded denominator is decoded back to an exact
rational before the final expression. Gains and biases are per position, shared across rows.
An exact final zero becomes +0. Exact statistics avoid premature sum/variance overflow;
final outputs can still overflow for extreme gains or biases.

`sqrtRational_eq_math` proves the square-root helper against an independent real-square-root
specification, including ties, underflow and overflow. Outside the stable domain, raw
normalization retains its pinned staged FloatLib fallback; the checked interface rejects that domain.

## Valid inputs and checked execution

- Linear: positive rows/input/output widths, exact input/weight/bias lengths.
- GEMM: positive batch/m/k/n and exact matrix lengths.
- Convolution: positive `cin`, `cout`, `h`, `w`, `kh`, `kw`, strides, dilations and `groups`;
  `groups` divides `cin` and `cout`; the dilated filter fits the padded input
  (`ekh ≤ h + padT + padB`, `ekw ≤ w + padL + padR`); input `cin·h·w`, weights
  `cout·(cin/groups)·kh·kw`, bias `cout`.
- Depthwise: the convolution rules for the shape with `cout = groups = cin`, so the record's
  `cout` and `groups` are not checked. Weights `cin·kh·kw`, bias `cin`.
- Transposed convolution: the same positive sizes and divisibility; at least one output row and
  column (`padT + padB < strideH·(h−1) + outPadH + ekh`, likewise for columns); per axis, output
  padding below the stride or below the dilation (`outPadH < strideH ∨ outPadH < dilH`), PyTorch's
  rule. ONNX Runtime is stricter (below the stride), so every model it runs is valid here. Input `cin·h·w`, weights `cin·(cout/groups)·kh·kw`, bias `cout`.
- Pooling: positive `c`, `h`, `w`, `kh`, `kw` and strides; the window fits the padded input
  (`kh ≤ h + padT + padB`, `kw ≤ w + padL + padR`); each side's padding is smaller than the window
  along that axis (`padT, padB < kh`; `padL, padR < kw`), so every window holds at least one image
  cell; input `c·h·w`.
- Scale/shift: positive channel/spatial sizes and exact array lengths.
- Residual addition: equal, nonempty activation and residual arrays.
- Normalization: positive rows/width, exact lengths and the finite/epsilon conditions above.
- Recurrent: positive `T`, `inW` and `H`; input `T·inW`, `wih` `G·H·inW`, `whh` `G·H·H`, both
  biases `G·H`. `lastStep`: positive `T` and `n` and an input of `T·n`.

`Layer.checked` checks the input before a layer; `runModelChecked` checks the initial length
and every intermediate layer. Invalid inputs return `Except.error`. These checks establish a
usable tensor boundary, not a numerical-overflow guarantee for arbitrary model parameters.
Raw functions retain their defined behavior outside this domain.

You may prove a fast path only on a particular domain and use the specification elsewhere;
`guarded_eq` supports this construction. There is no obligation to use the reference algorithm,
prove machine intermediates stay in binary32 range, or restrict the universal equality claim
to the benchmark's shapes.

## Workload quality and input sensitivity

The synthetic suite uses bounded inputs and fan-in-scaled weights, separate small biases and
three input distributions. The seed controls shapes and values. The model builders and original cell selector have
universal shape-validity proofs. The newer `prepareProfile` selector is not covered by the
original `all_cells_valid` theorem; current profiles are additionally exercised by executable
validation. Finiteness and input sensitivity are tested on selected seeds. Shape validity
does not prove every seed avoids overflow or uninformative outputs.

To inspect shapes and how outputs respond to fresh/sign-flipped inputs:

```bash
(cd lean && ~/.elan/bin/lake build auditExactWorkloads)
.venv/bin/python scripts/audit-exact-workloads.py --label my-workload-check --check-quality
```

These diagnostics are developer checks, not admission rules. See [specification audits](SPEC-AUDIT.md)
and the [historical specification review](research/SPECIFICATION-REVIEW-20261019.md) for evidence
and its limits. Current benchmark generation and scoring are described in [BENCHMARKING.md](BENCHMARKING.md).
