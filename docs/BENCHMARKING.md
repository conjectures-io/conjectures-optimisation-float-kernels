# Benchmarking and promotion

## Local reference versus server champion

```bash
source .venv/bin/activate
python gate.py --slot full --bench my-entry
python gate.py --slot full my-entry
```

Local runs use `kernels/full/incumbent` unless `GATE_INCUMBENT_FULL` selects
another source file. The gate worker supplies the current promoted champion on
the server. Repository examples and local measurements do not identify the live
champion.

`--bench` skips policy and proof verification so unfinished implementations can
be timed. Checksum checks still run; the result is not a verification verdict or
promotion decision.

## Current workloads

The full entry measures twelve classes, three per kernel:

| Kernel | Classes | Default work per class |
| --- | --- | --- |
| `add` | `near-one`, `wide-gap`, `cancel` | 500,000 operand pairs |
| `mul` | `near-one`, `spread`, `tiny` | 500,000 operand pairs |
| `sum` | `dense`, `relu`, `wide` | 500 vectors of length 1,000 |
| `bmm` | `dense`, `relu`, `wide` | Two batches of 50×100 by 100×50 matrices |

Tensor classes cover dense data, ReLU-like zeros, and a wide exponent range.
Raw-bit input classes are checksummed but not timed. The proof obligation covers
all inputs and tensor shapes, even though timing uses these particular workloads.
See [BenchFull.lean](../lean/bench/BenchFull.lean),
[Bench.lean](../lean/bench/Bench.lean),
[BenchTensor.lean](../lean/bench/BenchTensor.lean), and
[BenchBmm.lean](../lean/bench/BenchBmm.lean) for generation and work counts.

## What the ratio measures

- The gate chooses a fresh seed for each evaluation unless `BENCH_SEED` is set.
  That evaluation uses the same generated inputs for both algorithms and all rounds.
- Reference and submission run in separate processes over 25 rounds by default.
  Their execution order alternates between rounds.
- Input generation and the checksum/warm-up sweep precede the timed sweeps.
  Each process reports the fastest of three timed sweeps per class.
- For each timed class, the headline score uses the lowest reference time and
  lowest submission time across rounds. It sums those class times separately,
  then divides submission total by reference total.

The headline score is not total gate duration. Compilation, proof checking,
process startup and input generation are outside the kernel timing. Timed loops
include benchmark loop/checksum work, so the ratio describes this harness.

There are no fixed equal kernel weights. A kernel contributes according to its
measured time in the summed classes. Changing scalar arithmetic can also change
`sum` and `bmm` times when they use that arithmetic.

## Statistical admission

The gate records paired round totals in addition to the headline ratio. The
worker promotes an accepted entry only if:

1. Its headline ratio is at most `PROMOTION_BAR` (default `0.97`).
2. There are enough paired rounds (at least five).
3. The fifth percentile of bootstrapped gain is positive.

For each round, admission computes submission total / reference total. It
resamples those paired ratios, takes their median, and evaluates gain as
`1 - median`. Defaults are 2,000 draws and a 0.05 quantile. The resampling seed
is derived deterministically from the recorded comparison evidence.

This differs from the headline minimum-time statistic. It reduces sensitivity
to lucky timing minima, but does not guarantee that noise can never promote an
unchanged implementation. The actual policy is in
[admission.py](../validator/admission.py).

## Comparing changes locally

Use an idle CPU and keep setup, compiler, and benchmark settings constant:

```bash
BENCH_SEED=42 BENCH_CPU=3 python gate.py --slot full --bench my-entry
```

Choose a CPU that exists on your host. Repeat with other seeds and inspect every
class; fixed-seed improvements may not generalise. `BENCH_CPU=` removes pinning
and can increase noise. `BENCH_ROUNDS` and `BENCH_PAIRS` change measurement work;
local overrides do not change the server configuration.

Logs and JSON results include timings, paired rounds, seed, reference hashes and
verifier fingerprint. `validator/tools/timing_study.py` repeats gate comparisons;
use `STUDY_SLOT` for a non-full slot. Hardware-specific timing studies are evidence,
not universal noise bounds or official example performance rankings.

The [benchmark workload proposal](notes/benchmark-workloads.md) is a future
proposal, not the implemented benchmark.
