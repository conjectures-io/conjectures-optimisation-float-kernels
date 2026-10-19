# Exact-runtime mathematical proofs

The operator proofs below are separate from the miner's `Obligation K` proof. They connect the
executable contract to real/rational mathematics, tensor coordinates and model shape semantics.
They are compiled by Lean; the audit rejects axioms outside `propext`, `Classical.choice` and
`Quot.sound`. There are no `sorry` or native-decision axioms in these proofs.

## Square-root rounding

File: `lean/contract/ExactToolkit/SqrtProof.lean`.

**`sqrtRational_eq_math` proves bitwise equality for every rational input**, including ties,
subnormal results and overflow. Its right-hand side, `sqrtMathWord`, uses:

1. The mathematical real square root.
2. The independently defined binary32 gradual-underflow grid (`cexp`/`scaledMantissa`).
3. Mathematical nearest-even integer rounding.
4. FloatLib's IEEE packing of the rounded mantissa and exponent.

The mathematical definition has no integer-root algorithm, denominator scaling or accuracy
certificate. Like the executable helper, it explicitly returns +0 on nonpositive arguments.

The executable implementation was changed to avoid maintaining a separate binary32 grid
algorithm. It scales the numerator exactly, computes an integer root and compares squared
midpoints. `sqrtAccuracy_correct` proves the resulting certificate describes the exact real
square root. `sqrtRoot_pos`, `sqrtStep_le_target` and `sqrtScale_correct` discharge the
nonzero-mantissa, permitted-shift and exact-value premises of FloatLib's verified rounder.

`sqrtModel_correct` additionally states the usual rounded-real equality when the encoded result
is finite. Its explicit finiteness premise is necessary because real numbers cannot represent
IEEE infinity; the bitwise theorem above has no such premise.

The reference may use larger integer intermediates than the previous hand-written grid code.
This changes the implementation, not the specified output bits. Independent native comparisons
cover both sides of squared midpoints, ties, extreme exponents and the overflow boundary.

## Tensor and reduction mathematics

File: `lean/contract/ExactToolkit/TensorProofs.lean`, namespace `ExactRuntime.TensorMath`.

| Area | Theorems and meaning |
|---|---|
| Dot product | `dsum_math`, `finiteDot_math`: the scaled integer/FloatLib total equals the rational sum of products. `dotSpec_nonzero_math` and `dotOf_nonzero_math`: one rounding of a dyadic representing that exact sum. |
| Exact zero | `dotSpec_zero_math`: -0 only for a nonempty reduction of exclusively negative-zero products; empty reductions and cancellation give +0. |
| Sum | `sum_total`: dotting with ones is the ordinary rational sum. Together with the dot theorems, it establishes the sum's one-rounding and zero-sign behavior. |
| Linear | `linear_entry`, `linear_total`, `linear_nonzero_math`: row-major input, output-major weight rows, exact row dot plus bias before rounding, then optional ReLU. |
| GEMM | `gemm_entry`: each batch/output cell is the exact dot of its input row and weight column. The generic dot theorems supply its rational/rounding meaning. |
| Convolution | `window_coordinates` uses signed coordinates to characterize zero padding. `conv2d_entry` and `conv_total` establish the nested channel/filter sum plus bias. This is NCHW/OIHW cross-correlation, without filter reversal. |
| Depthwise | `depthwise_entry` and `depthwise_total`: only the matching input channel and its own filter contribute; multiplier one. |
| Average pooling | `pool_coordinates`, `pool_total`, `avgpool_entry`: square non-overlapping windows, exact window sum rounded once, then FloatLib division by window size. This deliberately retains two rounding steps. |
| Maximum pooling | `maxpool_entry` gives the bitwise ordered IEEE fold. `maxpool_math` proves it is a mathematical extended-real maximum on non-NaN windows, including infinities. |
| Elementwise operations | `addRelu_entry`, `scaleShift_entry`: pointwise FloatLib addition/FMA with the intended channel broadcasting. `relu_finite_math` proves finite ReLU is rational `max(0,x)`; `relu_preserves_nan` records its NaN behavior. |
| Normalization | `rowMean_math`, `rowVariance_math`: ordinary indexed rational statistics. `centered_sum_zero`, `variance_nonneg`, `variance_second_moment`: centering, positivity and population-variance identity. `rmsnorm_entry`, `layernorm_entry` expose the chosen denominator and final rounding points. |

Theorems compose: coordinate theorems identify the operands, reduction theorems establish their
exact mathematical total, and the rounding theorems establish the result. This avoids copying
the same reduction proof into every tensor operation.

Finite rational interpretations explicitly require finite operands. NaNs have no rational value;
their behavior remains the pinned FloatLib operation's behavior. The max-pool real theorem
excludes NaNs, while its bitwise entry theorem still describes them. Signed-zero bits are not
distinguishable in real arithmetic and are handled by the bitwise zero and entry theorems.

`linear_input_bound`, `linear_weight_bound`, `conv_input_bound` and `pool_input_bound` prove
array-index bounds under the corresponding validity predicates. Characterization theorems
with row/column hypotheses use actual in-range outputs rather than out-of-bounds defaults.

## Arbitrary model composition

File: `lean/contract/ExactToolkit/ModelProofs.lean`.

- `Layer.shapeStep` independently describes activation and saved-residual lengths.
- `layer_shape` and `layers_shape` prove actual execution follows those symbolic shapes.
- `runModel_shape` proves exact output length for any layer list and correctly sized batch.
- `Layer.acceptsShape` and `ModelWellShaped` define static compatibility between successive
  layers, including residuals.
- `model_preserves_valid_shapes` proves every actual layer receives compatible shapes whenever
  the initial program is statically well-shaped.
- `accepted_model_shape` and `accepted_preserves_valid_shapes` transfer those guarantees to
  every implementation satisfying the universal miner obligation.

These results apply to arbitrary layer lists, not just the five benchmark builders. They do not
claim that every arbitrary list is well-shaped. Nor do shape proofs establish numerical safety:
finiteness, positive epsilon and useful activation distributions are separate conditions.

## Check and reproduce

```bash
.venv/bin/python scripts/check-exact-proofs.py
.venv/bin/python scripts/check-exact-proofs.py --replay
```

The command builds the three proof modules, checks the audit's theorem axiom reports, and fails
if a report is missing or an additional axiom appears. `--replay` also checks all three modules
with `leanchecker`. The existing nightly/manual Lean CI job runs the axiom audit after setup.

For independent executable comparisons, build `exactSpecProbe` and run:

```bash
.venv/bin/python scripts/audit-exact-spec.py --native --cases-per-kernel 200
```

## What remains outside these claims

- Independent human review that the written mathematical definitions express the intended API.
- Whole-model numerical bounds for every possible generated seed.
- Compiler/runtime/hardware correctness beyond the independent native execution checks.
- Timing/admission calibration. Reduced development timings are not evidence of a real speedup.

These are distinct assurance tasks, not missing premises silently assumed by the theorems above.
