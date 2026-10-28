# Kernel guide

A submission is one file importing only `ContractExact`, with `Submission.kernels` and
`Submission.correct : ExactRuntime.Obligation Submission.kernels`. The obligation is equality
with `specKernels`, for all inputs. Start from [the shipped implementation](../kernels/exact/incumbent/Kernel.lean)
and follow the [manual](../MANUAL.md) to verify and submit it.

## Kernel record

| Family field | Kernels | Meaning |
|---|---|---|
| `core` | `sum`, `dot` | Exact sum / sum of products, rounded once. |
| `dense` | `linear`, `gemm` | Exact dot per output; linear includes bias before rounding and optional ReLU. |
| `conv` | `conv2d`, `depthwise` | Exact window dot plus bias, rounded once; optional ReLU. |
| `elem` | `avgpool`, `maxpool`, `addRelu`, `scaleShift` | Average/max pooling, residual addition, channelwise fused scale and shift. |
| `norm` | `rmsnorm`, `layernorm` | Exact rational statistics with pinned denominator and output rounding. |

The names of these proof/record families are **not** separate reward boards. Boards evaluate
complete inference models. Every entry supplies every kernel, even when only one is changed.
The core functions do not earn independent scores merely by becoming faster; their benefit must
reach the models that use them. See [the benchmark](BENCHMARKING.md).

## Starting implementation

The package combines an exact limb accumulator, digit-based final rounding, word-level maximum
pooling, cached convolution with a direct 1×1 path, fast proved residual addition and
second-moment layer normalization. Other kernels reuse the dot implementation or the specs.

The accumulator sums finite products exactly before final binary32 rounding. Its proof connects
that integer representation to FloatLib's exact dot product. Special inputs follow the pinned
FloatLib semantics. The normalization optimization proves the exact population-variance identity;
it does not replace exact arithmetic with a floating-point approximation.

This is a competitive starting point, not a claim that every kernel is optimal. You may use a
different representation, traversal or fast path, provided the complete equality proof passes.

## Proof structure

`CoreObligation`, `DenseObligation`, `ConvObligation`, `ElemObligation` and `NormObligation`
allow family proofs to be composed with `obligation_of_families`. Alternatively, rewrite individual
kernel equalities to prove `Obligation` directly, as the starting file does.

The [toolkit](../lean/contract/ExactToolkit/) supplies word decoding, exact accumulators and
rounding lemmas. The [contract](CONTRACT.md) documents tensor layouts, valid shapes, special
values and normalization. The [mathematical inventory](MATHEMATICAL-PROOFS.md) connects these
executable definitions to rational/real mathematics and model-shape semantics.

## Useful local tools

After setup, copy your entry to `lean/work/Submission.lean` and build the target in `lean/`.

| Target / command | Purpose |
|---|---|
| `VerifyExact` | Check the complete correctness statement. |
| `benchExact` | Native inference timings/checksums; see the manual. |
| `exactDev profile mlp` | Layer costs; also accepts `cnn`, `mnet`, `nmlp`, `mixer`. |
| `exactDev` | Developer comparisons of exact reductions with reference/native arithmetic. |
| `exactAudit --json 20 971` | Compare all kernels with their specs on generated and directed cases. |
| `exactAudit --malformed 20 971` | Include arrays shorter than their declared shapes. |
| `exactAudit --selftest` | Check the spec against itself and test the audit's planted mutants. |

Direct Lean/native commands are development tools. Run the full [proof gate](PROOFS.md) before
submitting; matching test outputs does not replace a proof.
