# Kernel contract

The [pinned contract](../lean/contract/ContractExact.lean) defines the 12 kernels, model execution
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
| `conv2d` | NCHW inputs, OIHW filters; cross-correlation without reversing filters, zero padding. |
| `depthwise` | One filter per input channel; multiplier one. |
| `avgpool` | Square, non-overlapping windows; round the exact sum, then FloatLib division by the window count. |
| `maxpool` | Ordered fold of FloatLib's IEEE maximum; incomplete trailing windows are discarded. |
| `addRelu` | FloatLib addition per element, then optional ReLU. |
| `scaleShift` | Channelwise FloatLib fused multiply-add: `x * scale + shift`, one rounding. |
| `rmsnorm`, `layernorm` | Stable rational statistics with the boundaries below; specified fallback outside that domain. |

This is not a claim that every operation has only one rounding. Average pooling and normalization
explicitly have several fixed steps. NaNs, infinities, payload handling and signed zeros follow
the executable definitions. Exact cancellation gives +0; an empty dot is +0; negative-zero-only
nonempty reductions retain −0. See the [mathematical proofs](MATHEMATICAL-PROOFS.md).

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
- Convolution: positive channels, spatial dimensions, filter sizes and stride; filter fits the
  padded image; exact input/filter/bias lengths.
- Depthwise: weights `cin * kh * kw`, bias `cin`. The shared shape record requires positive
  `cout`, although depthwise execution ignores that field.
- Pooling: positive channels/dimensions/window, window fits both spatial dimensions.
- Scale/shift: positive channel/spatial sizes and exact array lengths.
- Residual addition: equal, nonempty activation and residual arrays.
- Normalization: positive rows/width, exact lengths and the finite/epsilon conditions above.

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
