# Mathematical proofs

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

## Correctly rounded sigmoid and tanh

Files: `lean/contract/ExactToolkit/Activation.lean` (the definitions) and `ActivationProof.lean`.

**`toReal_tanhModel` and `toReal_sigmoidModel` prove that for every finite input the result is
finite and is the nearest-even rounding of `Real.tanh x` and of `1 / (1 + e^(−x))`**, for every
IEEE descriptor, so in particular binary32 (`tanhCR`, `sigmoidCR`, the contract's `tanhW` and
`sigmoidW`). Specials (NaN, ±∞, ±0) are pinned by the definitions themselves.

Each definition tries an enclosure search first and falls back to exact comparisons; both paths
are covered:

- `compareTanh_eq_real` and `compareSigmoid_eq_real`: the comparators order the true value
  against every rational candidate. For `0 < b < 1`, `tanh a < b ⟺ 2a < log((1+b)/(1−b))`
  (through Mathlib's `artanh`: `strictMonoOn_artanh`, `artanh_tanh`, `artanh_eq_half_log`) and
  `σ x < b ⟺ x < log(b/(1−b))` (`realSigmoid_logit`, `realSigmoid_strictMono`). FloatLib's
  `compareLog_eq_real` decides the logarithm comparison exactly, and
  `AlgebraicRounding.toReal_round_eq_roundAt` turns an exact comparator into a nearest-even
  rounding. Finiteness follows from `|tanh| < 1`, `0 < σ < 1` and FloatLib's
  `one_lt_overflowThreshold`.
- `contains_tanhEnclosure` and `contains_sigmoidEnclosure`: every enclosure the search builds
  contains the true value. FloatLib's `contains_exp` encloses `e^(−2|x|)` or `e^(−x)`, and
  `tanh a = (1 − e^(−2a)) / (1 + e^(−2a))` (`tanh_eq_exp_neg_two`) and `σ = 1 / (1 + e)` are
  decreasing in `e`. Far from zero the enclosures are the constant bounds `[0, 2^(−46)]` for
  `e^(−t)` with `t ≥ 32` (`exp_neg_le_inv_two_pow`, from `log 2 < 0.6931471808`) and
  `[0, 2^(−184)]` for `σ x` with `x < −128`. `tanhSearch_spec` and `sigmoidSearch_spec` then
  use FloatLib's `toReal_eq_roundAt_of_round?_eq_some`: an accepted search rounds every value in
  its enclosure, in particular the true one.

## Tensor and reduction mathematics

File: `lean/contract/ExactToolkit/TensorProofs.lean`, namespace `ExactRuntime.TensorMath`.

| Area | Theorems and meaning |
|---|---|
| Dot product | `dsum_math`, `finiteDot_math`: the scaled integer/FloatLib total equals the rational sum of products. `dotSpec_nonzero_math` and `dotOf_nonzero_math`: one rounding of a dyadic representing that exact sum. |
| Exact zero | `dotSpec_zero_math`: -0 only for a nonempty reduction of exclusively negative-zero products; empty reductions and cancellation give +0. |
| Sum | `sum_total`: dotting with ones is the ordinary rational sum. Together with the dot theorems, it establishes the sum's one-rounding and zero-sign behavior. |
| Linear | `linear_entry`, `linear_total`, `linear_nonzero_math`: row-major input, output-major weight rows, exact row dot plus bias before rounding, then optional ReLU. |
| GEMM | `gemm_entry`: each batch/output cell is the exact dot of its input row and weight column. The generic dot theorems supply its rational/rounding meaning. |
| Convolution | `window_coordinates` takes the group `g` and uses signed coordinates to characterize zero padding: tap `(c, dy, dx)` is channel `g·cpg + c` at `(y·strideH + dy·dilH − padT, x·strideW + dx·dilW − padL)`, zero outside the image. `conv2d_entry` and `conv_total` establish the nested sum over the group's channels and the filter, plus bias. This is NCHW/OIHW cross-correlation, without filter reversal. |
| Depthwise | `depthwise_taps`: for `0 < cin`, the depthwise geometry has `kh·kw` taps and one output channel per group. `depthwise_entry` and `depthwise_total` (which needs `0 < cin`): only the matching input channel and its own filter contribute; multiplier one. |
| Transposed convolution | `convTranspose_entry`: each output is the exact dot of the listed contributing taps (`convTTaps`), with `convTInput`/`convTWeight` as operands and the bias last, then optional ReLU. `tapSource_eq_some_iff`, `tapSource_isSome_iff` (for a positive stride): `tapSource` returns exactly the input index `i < n` with `i·stride + d·dil = p + pad`, the only one. `sum_filter_range` turns the sum over the filtered tap array into a `Finset` sum, `sum_tapSource` picks out the one source position, and `tap_coordinates` decodes flat tap `(c·kh + dy)·kw + dx`. `convTranspose_total` (for positive strides) proves that the flat reduction equals `convTValue`: the bias plus, over the group's input channels `c` and filter taps `(dy, dx)`, every input position `(iy, ix)` inside the image with `iy·strideH + dy·dilH = y + padT` and `ix·strideW + dx·dilW = x + padL`, times `w[g·cpg + c, o mod opg, dy, dx]`. This is ONNX `ConvTranspose` with `cin × (cout/groups) × kh × kw` weights. Taps that reach no input contribute nothing. The generic dot theorems supply the rounding meaning. |
| Recurrent layers | File `RecurrentProofs.lean`. `lstm_entry`, `gru_entry` (through `recurWith_entry` and `recur_fold`): output `(t, u)` is unit `u` of the hidden state after `t + 1` steps (`stateAfter`) from the zero state. `lstm_cell`, `lstm_hidden`, `gru_hidden` expose one step unit by unit. `gate_total`: a gate's dot product is exactly `gateValue`, `Σ_j x[t, j]·w_ih[k, j] + Σ_j h[j]·w_hh[k, j] + b_ih[k] + b_hh[k]`; `gate_nonzero_math`: with finite operands and a nonzero value, the gate is that rational rounded once. `cell_total`: the LSTM cell's exact sum is `f·c + i·g`. `gru_update_total`: the GRU update's is `n − z·n + z·h` (`rational_negW`: flipping the sign bit negates the value). `gru_candidate_total`: the candidate's is `Σ x·w_in + b_in + r·hn`. With `dotOf_nonzero_math` each is rounded once, and with the activation theorems above every value in a step is an exact rational expression of finite words, rounded once. |
| Average pooling | `pool_coordinates`, `pool_window_inside`, `pool_total`, `avgpool_entry` on `PoolShape` windows: a window's cells are a rectangle of image cells that never reaches the padding; its exact sum is rounded once, then divided with FloatLib by `PoolShape.divisor`. This deliberately retains two rounding steps. |
| Maximum pooling | `maxpool_entry` gives the bitwise ordered IEEE fold over the window's image cells. `maxpool_math` proves it is a mathematical extended-real maximum on nonempty non-NaN windows, including infinities. |
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
array-index bounds under the corresponding validity predicates; `pool_input_bound` uses
`pool_window_inside`. Characterization theorems
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

<a id="generated-benchmark-models-every-seed"></a>

## Original cell selector: every seed

File: `lean/audit/ExactSuiteProofs.lean`, namespace `ExactSuite`.

The main theorem closes the gap between the model builders and the general shape-preservation
theorem:

```lean
theorem all_cells_valid (c : Cell) (hc : c ∈ cells) (seed : UInt32) :
    (prepareCell c seed).ShapeValid c.batch
```

This quantifies over every UInt32 seed and every scored cell; it does not enumerate seeds or
use native execution as a proof. `ShapeValid` establishes:

- Positive sample count and input size, with exactly `samples * size` input words.
- Compatible dimensions and exact parameter-array lengths at every layer.
- Matching saved-residual and activation lengths.
- Exactly `batch * 10` classifier output words in total.

The proof starts with `lcg_size`, an invariant over the actual generator's loop, then establishes
the lengths of `gen`, `scales`, `wts` and `biases`. Each builder has a separate `*_wellShaped`
theorem: arbitrary nonempty positive-width MLP lists; positive normalized-MLP and Mixer
dimensions; positive CNN/MobileNet channels/classes with spatial side at least two. The spatial
proofs include floor-sized pooling, so odd image sides are covered too.

`cellModel_valid` proves the seeded selector always satisfies these premises for positive
batches. `cells_batch_pos` discharges that premise for the actual suite. `prepareCell_valid`
then connects the selected program to its generated input.

`accepted_cell_sample_shapes` transfers the result to every actual sample's layer execution
for any kernel set satisfying `Obligation`; `accepted_cell_output_size` proves its exact total
output length. These statements are about shapes. They do not prove that every seed avoids
numerical overflow, dead ReLU paths or poor timing behavior.

The theorem above is about `prepareCell`. The current multiseed benchmark uses `prepareProfile`;
that selector is not quantified by `all_cells_valid`. The generic builder and model-shape
lemmas remain available, but a separate bridge is needed to prove every profile/seed reaches
their premises. Executable validation of current profiles complements these narrower proofs.

## Check and reproduce

```bash
.venv/bin/python scripts/check-exact-proofs.py
.venv/bin/python scripts/check-exact-proofs.py --replay
```

The command builds the proof modules (square root, tensors, models, activations, recurrent layers
and the suite), checks the audit's theorem axiom reports, and fails
if a report is missing or an additional axiom appears. `--replay` also checks every module
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
