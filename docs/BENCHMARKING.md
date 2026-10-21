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

## Suite scoring

When the operator sets `GATE_FULL_SUITE`, the full entry is timed on cases drawn
from a *suite* instead of the twelve legacy classes below. Practice boards keep
their legacy classes.

- **A suite** is a JSON file. It names *groups* (`dense`, `ml`, `sparse`,
  `specials`, `subnormal`, `paired`, `extremes`) and gives each group *families* of
  inputs, with parameter ranges and size distributions.
- **Per evaluation**, the gate draws a fresh plan from the seed: several cases per
  kernel and group, each with its own family, parameters and size. One untimed
  raw-bits case per kernel is only checksummed. Champion and entry run the same plan.
- **The paid board is scored on a private suite.** It has the public suite's
  groups, but its families, parameter ranges and sizes are not published, and
  every evaluation draws new ones.
- **[suites/public-v2.json](../suites/public-v2.json)** is the public suite. It
  uses the same generators and groups, so tune on it, then expect the private
  suite to differ in detail.
- **What is published:** results. The verdict gives each cell's (kernel and
  group's) ratio, and every case's per-round times under its cell. It does not give
  a case's parameters, its size or the plan.
- **What the cases are made of:** [BenchSuite.lean](../lean/bench/BenchSuite.lean)
  builds them. Ordinary values have uniform or normal-like magnitudes. On top of
  those it can place:
  - zeros, in runs, signed or as tiny leaked values;
  - blocks of subnormals;
  - ±∞, NaN, ±largest finite and ±0, at random, on a stride, in bursts or at row
    ends;
  - fractions rounded to bf16 or fp16 precision;
  - small codebooks of repeated values;
  - per-block scales.

  For `add` and `mul`, the second operand can also be tied to the first: equal,
  negated, nearly cancelling, constant, an identity, or a set number of binades
  below it. `sum` lengths and `bmm` shapes are drawn from wide ranges, from
  single elements to long vectors and tall, skinny or degenerate matrices.

**How a suite is scored**
- Per case, the fastest round of each side gives a ratio.
- A cell's ratio is the geometric mean of its cases' ratios.
- The headline ratio is the weighted geometric mean of the cells. Each kernel
  weighs the same, split over its groups by the suite's group weights.

Time does not weigh a case, so a fast path for one kind of input cannot hide a
slowdown on another behind its share of the total time.

**Admission under a suite**
- The headline ratio must be at most the bar.
- No cell may be slower than the champion by more than 5%
  (`REGRESSION_GUARD = 1.05`). An entry that breaks this is `regressed`.
- The bootstrap resamples both the rounds and the cases within each cell, so the
  gain must hold across the kinds of input drawn, not only across timing noise.

The rule is `SUITE_POLICY_VERSION` in [admission.py](../validator/admission.py).

## Legacy workloads

Without a suite, the full entry measures twelve classes, three per kernel:

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
class; fixed-seed improvements may not generalise. To time on the public suite, as
the paid board is timed on its private one:

```bash
GATE_FULL_SUITE=suites/public-v2.json BENCH_SEED=42 python gate.py --slot full --bench my-entry
```

Look at every cell, and at several seeds: each seed draws different cases. `BENCH_CPU=` removes pinning
and can increase noise. `BENCH_ROUNDS` and `BENCH_PAIRS` change measurement work;
local overrides do not change the server configuration.

Logs and JSON results include timings, paired rounds, seed, reference hashes and
verifier fingerprint. `validator/tools/timing_study.py` repeats gate comparisons;
use `STUDY_SLOT` for a non-full slot. Hardware-specific timing studies are evidence,
not universal noise bounds or official example performance rankings.

The [benchmark workload proposal](notes/benchmark-workloads.md) is a future
proposal, not the implemented benchmark.
