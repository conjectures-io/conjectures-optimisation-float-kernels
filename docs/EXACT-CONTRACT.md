# Exact-runtime contract and local validation

This is a local development contract. It is not integrated with the API, workers, database or
rewards. The existing paid competition is unchanged.

## Valid inputs

`ContractExactChecked` provides explicit shape predicates and a checked runtime:

- Linear: positive row/input/output counts, input `rows*in`, weights `out*in`, bias `out`.
- GEMM: positive batch/m/k/n, arrays `batch*m*k` and `batch*k*n`.
- Convolution: positive channels, spatial dimensions, filter sizes and stride; filter fits the
  padded image. Inputs are NCHW; filters are OIHW. The operation is cross-correlation, without
  reversing the filter. Array lengths must match exactly.
- Depthwise: multiplier one, weights `cin*kh*kw`, bias `cin`; `cout` is ignored by its operation.
  The shared shape record still requires a positive `cout`.
- Pooling: positive channels/dimensions/window, window no larger than either spatial dimension.
  Square non-overlapping windows; incomplete trailing windows are discarded.
- Scale/shift: channel and spatial sizes positive; exact input/scale/shift lengths.
- Residual addition: equal, nonempty activation and saved arrays.
- Normalization: positive rows/width, exact lengths, finite inputs/gains/biases, positive finite
  epsilon. Gains/biases are shared across rows, per position.

`Layer.checked` validates before evaluating a layer. `runModelChecked` validates input length
and every layer using the actual intermediate arrays. Errors are `Except.error`; they are not
silently replaced with partial outputs. These are shape/domain checks, not a proof that an
arbitrary model will avoid numerical overflow.

Raw specification functions retain behavior outside this public domain. They are not checked
public tensor APIs; callers should use the checked boundary.

## Miner correctness

`Obligation K` remains universal equality with `specKernels`. There is no requirement to copy
the reference algorithm or bound internal values. `guarded_eq` proves the standard fast-path
construction: if a fast implementation equals the specification on a valid domain, using the
specification outside that domain yields universal equality.

The shipped incumbent's existing `correct` theorem continues to compile. Stable normalization
currently uses the exact reference arithmetic, leaving optimization opportunities for miners.

## Stable normalization: chosen rounding boundaries

For valid finite inputs, statistics are rational numbers, not binary32 intermediates. Let
`R` be round-to-nearest binary32, ties to even:

```text
RMS norm:
    v = sum(x_i²) / n                       exact
    d = R(sqrt(v + epsilon))
    y_i = R((x_i * gain_i) / d)              expression exact until final rounding

Layer norm:
    mean = sum(x_i) / n                     exact
    centered_i = x_i - mean                 exact
    v = sum(centered_i²) / n                exact
    d = R(sqrt(v + epsilon))
    y_i = R(centered_i * gain_i / d + bias_i)
```

The rounded denominator is decoded back to its exact rational value for the final expression.
An exact final zero is +0. Statistics are not rounded before square root, avoiding the earlier
sum/variance overflow. Final outputs can still overflow for extreme gain/bias values: stable
statistics do not promise bounded outputs for every finite parameter combination.

Outside the stable domain, raw normalization functions retain the prior staged FloatLib
semantics, including special-value behavior. The checked interface rejects that domain.
Other kernels retain their existing rounding and special-value rules. This includes average
pooling: round its exact window sum, then divide by the rounded window size.

`sqrtRational` scales the rational exactly, computes an integer square root,
then compares squared midpoints exactly and passes its proved accuracy certificate to
FloatLib's verified nearest-even rounder. `sqrtRational_eq_math` now proves complete bitwise
agreement with an independent real-square-root specification, including underflow, ties and
overflow. See [the mathematical proof inventory](EXACT-PROOFS.md).

## Benchmark workloads and timing

- Five model families, two batch sizes and three distributions: 30 cases per seed.
- Initial finite magnitudes: normal/sparse `[2^-6,1)`, wide `[2^-40,1)`; sparse has half zeros.
- Weight/bias arrays are divided by `max(1, floor(sqrt(array length)))`, rounded by FloatLib.
  Batch-normalization gains remain in `[1,2)`. Shapes and values vary with the shared seed.
- Model construction, input generation and output hashing are outside inference timing.
- Repetition counts are calibrated on the reference and shared with the candidate.
- `BENCH_INNER` defaults to 3 and is explicitly forwarded to child processes; calibration uses 1.

This generator uses deterministic conservative scaling, not a claim of variance-preserving
trained-model initialization. Inspect diagnostics before selecting final workload distributions.

## Reproduce locally

`./setup.sh --exact` builds the local exact-runtime targets, including their native dependencies.
On an already configured checkout, rebuild directly:

```bash
cp kernels/exact/incumbent/Kernel.lean lean/work/Submission.lean
(cd lean && lake build ExactToolkit.ModelProofs VerifyExact benchExact validateExact exactSpecProbe)

.venv/bin/python scripts/audit-exact-spec.py --native --cases-per-kernel 200 \
  --output data/spec-audit/stable-native.json

lean/.lake/build/bin/validateExact 42 20261019 20261020 \
  > data/spec-audit/workloads.jsonl

.venv/bin/python scripts/check-exact-proofs.py --replay

BENCH_ROUNDS=5 BENCH_INNER=1 GATE_CELL_MS=10 BENCH_SEED=42 \
  .venv/bin/python gate.py --slot exact kernels/exact/incumbent
```

The reduced gate settings are a development smoke test, not statistical calibration.
The independent oracle also checks small complete models from all five builders, for all three
data distributions, comparing every intermediate output and residual wiring. `--native` uses
the compiled executable; omit it to exercise `lean --run`.

`validateExact` executes checked layers for full-size generated models using the incumbent,
rejects nonfinite/empty activations, and records zero counts per layer. It reports observations;
it does not prove safety for every possible seed or identify every numerically uninformative model.

For the larger exceptional-value and generated-shape audit, see the
[follow-up review and reproduction commands](EXACT-SPEC-AUDIT.md#follow-up-review-stable-contract-and-generated-models).
The wider seed sample includes all-zero intermediate ReLU paths; generated finiteness alone
does not establish that every benchmark case contains useful work.

## Local evidence from this implementation

- The incumbent's proof compiled. The latest full isolated gate passed statement checking,
  `leanchecker` replay, the allowed-axiom audit and all output checksum comparisons.
  Its reduced timing run failed the regression guard on an identical incumbent; see below.
- The fast Python suite passed 114 tests; 138 tests were skipped and 5 slow tests deselected.
- Three full-size seeds (42, 20261019, 20261020), all 30 cases: 1,764 layer evaluations,
  no nonfinite values and no entirely zero intermediate arrays.
- Independent native comparisons include all 12 kernels, exact square-root midpoint tests,
  rejected invalid inputs and per-layer traces for 15 small model/distribution combinations.
  Across two seeds: 7,676 comparisons and 30 small-model runs, no mismatches.
  Raw records and source hashes are under `data/spec-audit/`.
- All three mathematical proof modules compiled and passed `leanchecker` replay.
  The audit checked 33 theorem axiom reports, using only the three allowed standard axioms.

Reduced five-round timing smoke tests gave an identical incumbent about 0.968× in an earlier
run and 0.989× in the latest run, whose worst regression group measured 1.194× and failed
the 1.10 guard. These settings must not be used to calibrate admission or claim an improvement.
Proof and checksum checks passed; timing acceptance remains unvalidated and needs normal-budget
studies on an otherwise idle validator.

## Additional assurance before freezing

- Independent review of the normalization policy and mathematical theorem statements.
- Broader exceptional-value/fallback tests and useful arithmetic bounds where practical.
- Production-machine timing calibration, in a later step.

The square-root rounding, tensor-index/sum and general model shape-preservation theorems are
implemented; see [their statements, assumptions and check commands](EXACT-PROOFS.md).
These formal results do not replace the independent review and numerical/timing studies above.
