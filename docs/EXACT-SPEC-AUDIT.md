# Exact-runtime specification audit

The reviews below preceded the later fan-in workload refinement. For the current generator's
paired input-sensitivity results and reproduction commands, see
[workload quality](EXACT-CONTRACT.md#workload-quality-and-input-sensitivity).
The arithmetic observations about average pooling and extreme final overflow still apply.
Universal builder-shape proofs were subsequently added for all scored cells and every UInt32
seed; see [the proof inventory](EXACT-PROOFS.md#generated-benchmark-models-every-seed).

## Follow-up review: stable contract and generated models

The revised contract was reviewed against its mathematical characterizations and independently
tested on 19 October 2026. The results below apply to the stable-normalization/checked-boundary
implementation, rather than the original contract discussed later in this document.

### Results

- **70,701 bitwise comparisons, no mismatches:** two native runs of 29,567 cases and one
  interpreted run of 11,567 cases. Directed cases repeat across runs; these are comparisons,
  not 70,701 distinct random inputs.
- **135 small-model traces:** all intermediate outputs and residual wiring agree with the
  independent reference. These include minimum dimensions, odd image sides, rectangular token
  matrices and multiple rows, for all five builders and three distributions.
- **1,050 generated models across 35 seeds:** all 9,240 layer-shape transitions, parameter
  lengths, saved-residual sizes, finite parameters and positive normalization epsilons passed.
  A separate Python checker derives tensor sizes independently from the Lean metadata.
  Each complete model produces exactly ten outputs per batch item.
- **360 full-size model cases across 12 seeds:** all 7,056 actual checked-layer evaluations
  succeeded, with no nonfinite intermediate activations.
- **117 Python tests passed**, 138 skipped, 5 slow tests deselected. Ruff passed. The 33-theorem
  allowed-axiom audit passed; the modified model-proof module also passed kernel replay.

The edge suite covers signed zeros, gradual underflow, every binary32 exponent transition's
square-root midpoints, overflow, quiet/signaling NaNs with signed payloads, infinities, FMA
cancellation, asymmetric/strided/padded convolution, independent GEMM batches, normalization
extremes and legacy fallbacks. Checked layers are tested with missing/extra array elements,
zero dimensions, oversized integer dimensions, invalid epsilon and nonfinite norm parameters.
Large dimensions are tested only where validation rejects them before an unbounded allocation.

### Review findings

**No remaining implementation/specification mismatch was found in these tests.** Four initial
discrepancies came from the Python oracle's subtraction rule: FloatLib negates the right operand
before NaN selection, including that operand's NaN sign. The oracle was corrected and all runs
repeated. Runtime semantics were not changed to accommodate the tests.

The tensor conventions agree with the written contract: row-major matrices, output-major linear
weights, NCHW/OIHW cross-correlation, multiplier-one depthwise filters, floor-sized nonoverlapping
pool windows, bias inside the exact dot, and explicit normalization rounding points. The
generated residual branches have matching lengths. CNN/MobileNet builders require side at least
two; the generated sides are 12 or 16. Direct builder functions remain unrestricted helpers,
so arbitrary external arguments still need validation.

Three boundaries remain important:

1. **Some generated workloads become locally trivial.** There were 26 all-zero intermediate
   arrays at seeds 1 and 3735928559, in ReLU CNN/MobileNet paths and a following pooling layer.
   No final classifier output was entirely zero. These are mathematically valid, but can make
   benchmark work less representative. Finiteness alone is not a workload-quality criterion.
   Inspect activation sparsity and input sensitivity before freezing the benchmark; no workload
   distribution was changed during this review.
2. **Average pooling deliberately rounds its sum before dividing.** A 2×2 window filled with
   maximum-finite values returns infinity, although its exact average is maximum finite.
   This is the specified two-step arithmetic, not a proof failure. Changing it to an exact
   rational average followed by one rounding would be a separate contract decision.
3. **Valid finite normalization inputs need not produce finite outputs.** Extreme finite gains
   can overflow the final result; 20 directed cases demonstrate this and match the independent
   reference. The checked boundary validates shapes and the normalization input domain, not
   arbitrary whole-model numerical safety. Generated bounded workloads avoided such overflow
   in the tested seeds.

The model tests establish validity of the tested generated instances. They do **not** constitute
a universal proof; that was supplied separately by the later builder-shape theorems linked above.
The general model theorems establish shape preservation once a program is well-shaped.
Cross-architecture compiler/hardware validation
and timing/admission calibration also remain separate work.

### Reproduce the follow-up

```bash
(cd lean && lake build exactSpecProbe auditExactShapes validateExact)
.venv/bin/python scripts/audit-exact-spec.py --native --edge-cases --cases-per-kernel 1000 \
  --seed 20261019 --output data/spec-audit/edge-20261019.json
.venv/bin/python scripts/audit-exact-spec.py --native --edge-cases --cases-per-kernel 1000 \
  --seed 20261020 --output data/spec-audit/edge-20261020.json
.venv/bin/python scripts/audit-exact-spec.py --edge-cases --cases-per-kernel 0 \
  --seed 42 --output data/spec-audit/edge-interpreted-42.json
.venv/bin/python scripts/audit-exact-models.py
lean/.lake/build/bin/validateExact 0 1 2147483648 4294967295 42 20261019 20261020 \
  7 12345 987654321 305419896 3735928559 > data/spec-audit/edge-workloads.jsonl
```

These checks are local developer tools; they do not submit work or touch the competition DB,
API, workers or rewards. Native and interpreted result files retain requests, expected/actual
words, model traces and source hashes. The nightly/manual CI job runs a smaller version.

## Original audit

**Historical audit:** the findings below describe the original specification. The follow-up
implementation introduces stable normalization and a checked runtime boundary; see
[the current contract and validation guide](EXACT-CONTRACT.md). Original recorded results
must not be interpreted as tests of the revised contract.

## Status

This is an initial independent audit of the specification at commit `1a90cdf`, not a
claim that the contract has been fully verified against an external mathematical standard.
The contract and incumbent were not changed by this audit.

The executable specifications agree with an independent rational-arithmetic reference on
the tested, well-shaped inputs. Two specification boundaries need decisions before freezing
the contract: invalid tensor shapes and intermediate rounding in normalization.

Completed runs: **5,636 comparisons across seeds 20261019 and 20261020, no mismatches**
(2,818 per seed, including repeated directed edge cases). Both runs reproduced the diagnostic
examples below. The five oracle sanity tests and Ruff lint/format checks also passed.

## What was checked

`scripts/audit-exact-spec.py` generates requests for `lean/audit/SpecProbe.lean`, which imports
the actual `ContractExact` definitions. It does not import the incumbent or benchmark.

The Python reference independently decodes IEEE binary32 fields into rational numbers.
It computes exact sums/products, then finds the nearest binary32 result by searching ordered
encodings and comparing exact midpoints. Square root uses comparisons against squared rational
midpoints. It does not call FloatLib, reuse its limb/dyadic algorithms, or use host floating-point
arithmetic to decide expected bits.

Coverage:

| Operation | Intended interpretation checked |
|---|---|
| sum/dot | Exact sum/products, one ties-to-even rounding; cancellation, subnormal and overflow cases; signed zero |
| linear | Row-major input, output-major weight rows, bias inside the exact dot, optional ReLU |
| GEMM | Two independent batches, non-square dimensions, row-by-column multiplication |
| convolution | NCHW, OIHW filters, cross-correlation (no filter reversal), zero padding, stride, bias |
| depthwise | Separate input/filter per channel, multiplier one; `cout` intentionally ignored |
| pooling | Square, non-overlapping windows; incomplete trailing windows discarded; sum rounded before average division |
| add/ReLU | Rounded elementwise addition; negative non-NaNs including -0 become +0 |
| scale/shift | Channel broadcasting, fused multiply-add, optional ReLU |
| RMS norm | Per-row squared reduction, rounded division/add/sqrt/reciprocal, then two rounded multiplies |
| layer norm | Per-row mean, rounded centering, squared reduction, same scale sequence, separate final multiplies/add |
| model composition | Two independent samples, linear layer and saved residual; no state leakage between samples |

Dense/pooling/norm inputs use small rational values; dot/sum also use arbitrary finite exponent
ranges and directed exceptional cases. Convolution cases have asymmetric images/filters, multiple
channels, and unequal input/output channel counts. Dot checks include signaling/quiet NaN
priority, payload quieting, infinities and invalid products. NaN policy is signaling-NaN priority,
then first quiet NaN, rather than simply the first NaN of either class.

The oracle has five sanity tests with literal rounding, FMA, square-root and hand-calculated
convolution answers. Matching the oracle remains test evidence, not a universal proof.

## Findings requiring a contract decision

### 1. Shape validity is not expressed

`ConvShape` contains unrestricted natural numbers. Output dimensions use natural-number
subtraction, which saturates at zero. An unpadded 2×2 input with a 5×5 filter therefore reports
1×1 output dimensions. Zero stride also reports 1×1 rather than being rejected.

These were reproduced through the Lean definitions. A conventional tensor API would reject
these shapes, or explicitly define empty outputs for an oversized filter. The contract currently
does neither. Tensor operations also use unchecked `array[index]!` accesses without proving
that array lengths match their shapes; runtime failure behavior is not a useful tensor semantics.

Recommendation: define well-formedness predicates for shapes, array lengths and model-layer
composition. Decide whether invalid inputs are rejected by checked wrappers or given an explicit
result. Prove the intended tensor equations under those predicates. Keep universal miner
equivalence if desired, but give the public runtime a checked boundary.

### 2. Normalization has intermediate rounding and avoidable overflow

The following describes the implemented finite arithmetic, with `R` meaning binary32 rounding:

```text
RMS norm:
    v = R(sum(x_i * x_i))
    scale = R(1 / R(sqrt(R(R(v / R(n)) + eps))))
    y_i = R(R(x_i * scale) * g_i)

Layer norm:
    mean = R(R(sum(x_i)) / R(n))
    centered_i = R(x_i - mean)
    v = R(sum(centered_i * centered_i))
    scale = same sequence as above
    y_i = R(R(R(centered_i * scale) * g_i) + b_i)
```

The sums/products inside each reduction are exact, but their returned values are rounded
*before* division. Mean, variance and normalization are not exact real expressions rounded
only once. Subtraction, reciprocal and multiplication also have observable rounding steps.

Reproduced examples with positive epsilon and unit gain:

- RMS norm of `[2^64, 2^64]` returns `[+0, +0]`: its squared reduction overflows to infinity,
  the reciprocal becomes zero. The mathematical normalized values are close to one.
- Layer norm of `[maxFinite, maxFinite]`, zero bias, returns NaNs: the sum overflows although
  the mean itself is representable. The mathematical centered row is zero.

These agree with the existing fixed-operation specification. They are numerical design
limitations, not evidence that a miner's proof is wrong. The comments describing means and
variance as exact are too broad. This also matters for average pooling, which explicitly
rounds the sum before division.

Recommendation: either retain these deterministic staged semantics and describe their limits
precisely, or choose a more stable/exact normalization specification before freezing it.
Changing evaluation order changes output bits and requires updating incumbent proofs.

### 3. Model equality does not independently validate model meaning

`runModel_eq` substitutes kernel-record equality. That legitimately proves every accepted
kernel record runs the same model program as the specification record. It does not establish
that the program has the intended layer shapes, indexing or architecture. The independent
composition check adds evidence for one small residual model, not all five benchmark builders.

Recommendation: add shape-preservation theorems for layers and validate every generated suite
model. Add separate mathematical index/sum characterizations for dense and convolution specs.
Existing `finiteDotSlice_eq` is already a useful arithmetic bridge, but it does not provide
those tensor-level characterizations.

## Reproduce

After setup has built `ContractExact`:

```bash
.venv/bin/pytest -q tests/test_exact_spec_oracle.py
.venv/bin/python scripts/audit-exact-spec.py --cases-per-kernel 200 \
  --seed 20261019 --output data/spec-audit/seed-20261019.json
.venv/bin/python scripts/audit-exact-spec.py --cases-per-kernel 200 \
  --seed 20261020 --output data/spec-audit/seed-20261020.json
```

The script fails on a mismatch or Lean execution error. Diagnostic examples are recorded
separately; they expose contract choices and are not counted as oracle mismatches.
The latest script saves inputs, expected/actual words and source hashes in its result file.
These checks require no database, blockchain, or submission worker.

## Remaining assurance work

- Prove independent mathematical characterizations of each tensor specification.
- Review those statements and the floating-point/special-value policy independently.
- Resolve shape and normalization decisions above.
- Extend special-value coverage beyond dot/add to pooling and normalization, and test malformed
  shapes at a future checked boundary.
- Check all five model builders and compare full intermediate model outputs independently.
- Test native compiled executables across architectures/compiler settings. This probe uses
  `lake env lean --run`; it does not audit C compilation, the runtime, or hardware.

Passing this suite improves confidence in the current definitions. It does not remove the
need for the remaining proofs/review or justify claiming that arbitrary models are correct.
