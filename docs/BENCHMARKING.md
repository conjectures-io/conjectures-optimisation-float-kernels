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
from a *suite* instead of the twelve legacy classes below. Only the inputs
change: the ratio and admission are the legacy ones, described further down.
Practice boards keep their legacy classes.

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
  group's) times and ratio, the per-round totals, and the suite's version and file
  hash. It does not give a case's parameters, its size or the plan.
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

**How a suite is scored:** like the legacy classes. Each case is one class: the
fastest round of each side, summed over all timed cases, submission total over
reference total ([What the ratio measures](#what-the-ratio-measures)). A cell's
ratio in the verdict is its cases' total over total. Admission reads the same
per-round totals as for the legacy classes ([Statistical
admission](#statistical-admission)).

**How cases are sized:** so that each takes about as long as any other on the
shipped baseline, as the legacy classes do. Then a kind of input counts by how
many cases the suite gives it, not by how slow the baseline happens to be on it.
- A kernel's work (the suite's `work`, a multiple of `BENCH_PAIRS`) is split
  evenly over its cases.
- A case that sends accumulations down the slow path gets proportionally fewer
  steps. Its estimated cost is `1 + fallback_cost × slow_share`. `slow_share` is
  the expected share of its steps that are slow: special operands for `add` and
  `mul`, and for `sum` and `bmm` the rest of each accumulation after a ±∞, NaN or
  ±largest value, or after an overflow. Once poisoned, the shipped baseline's
  steps cost 30–100 times a clean one's.
- The estimate comes from the case's drawn parameters
  ([benchmark_suite.py](../benchmark_suite.py)), never from a timing, so a plan
  is the same for champion and entry and for every run with the same seed.

An entry that is as slow as the baseline on poisoned inputs while its champion
is fast there pays for it on every such case.

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
