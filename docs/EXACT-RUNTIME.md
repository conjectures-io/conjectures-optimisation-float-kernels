# Exact tensor runtime

One complete kernel set is proved once, then compared on model-family boards and an overall board.
The coordinator and hardware workers run locally; central API rollout is a separate task.

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
python scripts/test-exact-workflow.py --help
PYTHONPATH=.:validator python -m verification my-entry --verdict results/trial.json
```

The [starting champion](../kernels/exact/incumbent/Kernel.lean) has an exact dot product
(`dotFn`), proved equal to FloatLib's (`dotFn_eq`), used by every dot-based kernel. Its final
rounding (`finishFast`) works on 32-bit digits and rounds from the top 62 bits and a sticky bit,
which FloatLib's rounding cannot tell apart from the exact total (`roundDyadic_jam`). It also has
a word-level max pooling (`maxW`), proved against FloatLib's `maximum`. The rest are the specs.
The proof stages and source policy are documented in ([Proofs and policy](PROOFS.md));
the statement check is [VerifyExact.lean](../lean/verify/VerifyExact.lean).

Each board reference is selected through the coordinator configuration and promotion history.

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

1. The reference calibrates repetitions for the selected cells (100 ms by default).
2. Both sides use the same seed and repetitions over alternating paired rounds (25 by default,
   best of 3 inner runs). Input generation and output hashing remain outside timing.
3. Cell ratios are combined with the evaluation's frozen model weights. The median paired-round
   score is calculated separately for each environment/seed stratum.
4. Stratum medians are combined geometrically with configured hardware/seed weights.
5. A per-cell median-ratio regression guard and stratified bootstrap decide admission.
   Defaults are a score at most 0.97, guard at most 1.10, and positive fifth-percentile gain.

All thresholds and weights live in the coordinator TOML. The overall board defaults to equal
model weights; a family board selects that model's six cells. Reward allocations are independent.
A guard failure is an evaluation outcome; it does not invalidate the submitted proof.

## Proof fixtures

selftest.py checks incumbent, baseline and slowdepthwise as mathematically valid; wrong is rejected
at statement, sorryax at axioms, and forge/tamper at policy. Bypassing policy deliberately must still
reject forge at kernel and tamper at statement. Benchmark outcomes have separate automated tests
and the real disposable-DB rehearsal.

## Developer tools

Run from `lean/` with an entry in `work/Submission.lean` (copy an entry there for developer commands):

| Command | What it shows |
| --- | --- |
| `lake exe exactDev` | `sum` and `dot` against FloatLib and native `Float32`, three sizes and data types |
| `lake exe exactDev profile cnn` | one sample of a model, layer by layer (`mlp`, `cnn`, `mnet`, `nmlp`, `mixer`) |
| `lake exe exactDev models` | every cell against FloatLib's kernels (slow) |
| `lake exe exactAudit [--malformed] [cases] [seed]` | every kernel against the specs on random and edge-case inputs (subnormals, overflow, exponent gaps, NaN and infinities, zeros, ties, cancellation); `--malformed` adds inputs shorter than their shape |
| `lake exe exactAudit --mutants [cases]` | the mutation score: 23 plausible wrong kernels (wrong rounding mode, ties away from zero, canonical NaN, lost −0, saturation, an off-by-one, a transposed index, clamped padding, no `fma`, the old normalisation, …), each of which the audit must catch |
| `lake exe exactAudit --coverage [cases]` | per kernel, how many outputs are +0, −0, subnormal, normal, ∞ and NaN: a 0 marks a case the inputs never reach |
| `lake exe exactAudit --selftest` | the spec against itself is clean and every mutant is caught |
| `lake exe exactShowcase` | trained 8×8 digit models: time, accuracy, bit identity and ulps for the entry, FloatLib's kernels, a proved left fold and native `Float32` |

The showcase's models and test images are in `lean/showcase/digits.bin`; `train.py` rebuilds
them (numpy and scikit-learn, not needed by the gate).

