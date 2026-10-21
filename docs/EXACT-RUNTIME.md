# Exact runtime slot (in development)

The `exact` slot has twelve neural-network kernels whose results must equal the pinned
specifications, scored by how fast they run a suite of models. It runs in the
local gate; the central API does not accept submissions for it.

See [the contract and validation guide](EXACT-CONTRACT.md) for stable normalization, valid
shapes, checked execution, numerical limits, and the remaining assurance work.

## What an entry provides

One file, `Kernel.lean`, that imports only `ContractExact` and defines, in namespace
`Submission`:

```lean
def kernels : ExactRuntime.Kernels := …
theorem correct : ExactRuntime.Obligation kernels := …
```

`Obligation K` is `K = specKernels`: every kernel equals its spec as a function. The specs are in
[ContractExact.lean](../lean/contract/ContractExact.lean):

| Family | Kernels | Spec |
| --- | --- | --- |
| core | `sum`, `dot` | FloatLib's `Model.dot`: the exact sum of the products, rounded once |
| dense | `linear`, `gemm` | one exact dot product per output (bias as an extra term), rounded once; optional ReLU |
| conv | `conv2d`, `depthwise` | one exact dot product per output over its window (any size, stride and padding), bias included |
| element | `avgpool`, `maxpool`, `addRelu`, `scaleShift` | exact window sum then FloatLib's `div`; FloatLib's `maximum`, `add`, `fma` |
| norm | `rmsnorm`, `layernorm` | exact rational statistics; one rounded square root and one final rounding per output |

Dot-based specs are written once over a dot product (`linearWith dot`, `conv2dWith dot`, …) and
the spec passes FloatLib's `dotOf`. An entry can reuse those definitions with its own proved dot
product. `runModel_eq` then gives every model the spec's output, bit for bit.

Each family also has its own obligation (`CoreObligation`, …), and `obligation_of_families`
combines them. The gate checks the whole obligation.

The toolkit ([ExactToolkit/](../lean/contract/ExactToolkit/)) provides proved building blocks:
word decoding, an exact limb accumulator, and the final rounding through FloatLib's
`roundDyadic`, with `roundDyadic_congr` (equal values round equally).

## Start from the champion

```bash
cp -r kernels/exact/incumbent my-entry
python gate.py --slot exact --bench my-entry
python gate.py --slot exact my-entry
```

The [starting champion](../kernels/exact/incumbent/Kernel.lean) has an exact dot product
(`dotFn`), proved equal to FloatLib's (`dotFn_eq`), used by every dot-based kernel. Its final
rounding (`finishFast`) works on 32-bit digits and rounds from the top 62 bits and a sticky bit,
which FloatLib's rounding cannot tell apart from the exact total (`roundDyadic_jam`). It also has
a word-level max pooling (`maxW`), proved against FloatLib's `maximum`. The rest are the specs.
The gate stages and source policy are those of the other slots ([Proofs and policy](PROOFS.md));
the statement check is [VerifyExact.lean](../lean/verify/VerifyExact.lean).

`GATE_INCUMBENT_EXACT` selects another champion file for local comparisons.

## Benchmark and score

[ExactSuite.lean](../lean/bench/ExactSuite.lean) builds five models from the evaluation's seed:

| Model | Kernels used |
| --- | --- |
| MLP | `linear` |
| ResNet-style CNN | `conv2d`, `scaleShift`, `addRelu`, `maxpool`, `avgpool`, `linear` |
| MobileNet-style CNN | `conv2d`, `depthwise`, `scaleShift`, `avgpool`, `linear` |
| Normalised MLP | `linear`, `rmsnorm`, `layernorm` |
| Mixer-style block | `layernorm`, `gemm`, `linear`, `addRelu` |

Each runs at two batch sizes on three data types (normal, wide exponent range, half zeros): 30
cells. Every cell's output checksum must match the champion's.

1. **Calibration.** The gate runs the champion once and repeats each cell so it takes about
   `GATE_CELL_MS` (default 100 ms). Both sides use the same repetitions.
2. **Rounds.** Champion and entry alternate over `BENCH_ROUNDS` rounds (default 25). Each cell
   reports the fastest of `BENCH_INNER` runs (default 3). Input/model generation and output hashing are outside timing.
3. **Round score.** The weighted geometric mean of the 30 cell ratios (entry time over champion
   time). Each model weighs one fifth, split equally over its cells.
4. **Score.** The median of the round scores, the statistic admission bootstraps. Rounds are
   recorded so admission's per-round ratios are these round scores.
5. **Regression guard.** For each model and data type, the geometric mean of the best-time ratios
   must be at most `GATE_GUARD` (default 1.10). An entry over the limit is rejected at the score
   stage, whatever its score.

Promotion uses the same admission rule as the other slots: a score at most `PROMOTION_BAR`
(default 0.97) and a positive fifth-percentile bootstrapped gain.

`GATE_CELL_MS` and `GATE_GUARD` are part of the verifier fingerprint.

## Fixtures

Rows in `kernels/expected.tsv`, checked by `selftest.py`:

| Fixture | Expected verdict |
| --- | --- |
| `exact/incumbent` | accepted, ratio 0.97–1.03 |
| `exact/slowdepthwise` | rejected at score by the regression guard |
| `exact/wrong` | rejected at statement |
| `exact/sorryax` | rejected at axioms |
| `exact/forge`, `exact/tamper` | rejected at policy; with policy skipped, at kernel and statement |

`exact/baseline` (FloatLib's kernels, `correct := rfl`) is valid but has no row: it is about 30x
slower, so a calibrated run takes hours.

## Developer tools

Run from `lean/` with an entry in `work/Submission.lean` (the gate leaves its last entry there):

| Command | What it shows |
| --- | --- |
| `lake exe exactDev` | `sum` and `dot` against FloatLib and native `Float32`, three sizes and data types |
| `lake exe exactDev profile cnn` | one sample of a model, layer by layer (`mlp`, `cnn`, `mnet`, `nmlp`, `mixer`) |
| `lake exe exactDev models` | every cell against FloatLib's kernels (slow) |
| `lake exe exactAudit [--malformed] [cases] [seed]` | every kernel against the specs on random and edge-case inputs (subnormals, overflow, exponent gaps, NaN and infinities, zeros, ties, cancellation); `--malformed` adds inputs shorter than their shape |
| `lake exe exactAudit --mutants [cases]` | the mutation score: 37 plausible wrong kernels (wrong rounding mode, ties away from zero, canonical NaN, lost −0, saturation, an off-by-one, a transposed index, clamped padding, no `fma`, blocked or tiled accumulation and outputs, ties at every final rounding (normal, subnormal, square root, overflow), a variance around the rounded mean, the old normalisation, …), each of which the audit must catch |
| `lake exe exactAudit --coverage [cases]` | per kernel, how many outputs are +0, −0, subnormal, normal, ∞ and NaN: a 0 marks a case the inputs never reach |
| `lake exe exactAudit --selftest` | the spec against itself is clean and every mutant is caught |
| `lake exe exactShowcase` | trained 8×8 digit models: time, accuracy, bit identity and ulps for the entry, FloatLib's kernels, a proved left fold and native `Float32` |

The showcase's models and test images are in `lean/showcase/digits.bin`; `train.py` rebuilds
them (numpy and scikit-learn, not needed by the gate).

