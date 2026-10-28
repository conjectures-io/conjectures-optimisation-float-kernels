# Benchmarking and scoring

One entry contains all kernels. Each board compares the complete entry with its own current
champion; winning one board does not substitute individual kernels into another entry.

## Boards and workloads

| Board | Workload | Batch sizes |
|---|---|---|
| `mlp` | Fully connected classifier | 1, 8 |
| `resnet` | Residual CNN | 1, 4 |
| `mobilenet` | Depthwise/pointwise CNN | 1, 4 |
| `normalized_mlp` | MLP with RMS and layer normalization | 1, 8 |
| `mixer` | Token/channel mixing block | 1, 4 |
| `overall` | Weighted combination of all five families | As above |

Each model/batch runs on normal, wide-exponent and sparse inputs: six cells per family,
30 for overall. A root seed draws **six workloads**: two typical, two scale and two boundary
cases, with total profile weights **50/30/20%**. Shapes and values vary, including odd boundary
sizes. This gives 36 workload/cell combinations per family, 180 for overall, per environment/root seed.
The suite is synthetic inference, not trained-model accuracy or backpropagation.

Fresh-seed mode chooses a new root seed for an evaluation; retries retain the same workloads.
Candidate and reference receive identical seeds, shapes, parameters and inputs. The required
hardware environments and their weights are part of the published board configuration.
The generator is pinned in [ExactSuite.lean](../lean/bench/ExactSuite.lean).

## Measurement

1. Build candidate and reference once for the target CPU, then reuse validated cached binaries
   across family jobs. Build time is not scored.
2. Calibrate repetitions on the reference, using two warmed best-of-three passes. Candidate and
   reference use the same repetition counts for every workload/cell.
3. Run paired rounds, alternating which binary runs first and using one CPU affinity where supported.
   Each reported duration is the best of the configured inner repetitions.
4. Require matching output checksums. Model/input preparation and output hashing are outside timing.

The development config generator uses **11 rounds, best of 3, 50 ms cells**. These are local
configuration defaults, not a promise about production. Read `/v1/competitions/torchlean/boards`
and submission reports for live policy and frozen evaluation settings. Full processing also includes
proof checking, native builds and queue waiting; it is longer than the timed inference.
Default measurement ceilings are 20 minutes for overall and 6 minutes for one family;
[worker budgets](WORKERS.md#independent-build-and-measurement-budgets) distinguish cold and cached preparation.

## Score and promotion

For each hardware/root-seed stratum:

- Cell ratio = candidate time / reference time. Below 1 is faster.
- For each workload and round, combine cell ratios by weighted geometric mean.
- Take the median across rounds for that workload.
- Combine workload medians using their profile weights, then combine hardware/root-seed scores
  by weighted geometric mean. Raw nanoseconds from different CPUs are never pooled.

The overall board's model weights are configurable; the development default weights models equally.
A family board scores only that family's cells.

Promotion requires **all** configured checks:

- Enough paired rounds and a score at or below the promotion bar (development default `0.97`).
- Positive lower-bound gain from a stratified bootstrap (default 2,000 resamples, fifth percentile).
  It resamples workloads within profiles and paired round indices, preserving weights.
- Every workload/cell/environment median ratio within the regression guard (default `1.10`).

Thus a fast average can still fail due to a regression or insufficient confidence. `gain_lower`
is a one-sided bound, not a two-sided confidence interval. Proof acceptance and promotion are
separate decisions. Stored outcomes include `qualifies`, `above-bar`, `guard-failed` and
`not-significant`; the report is authoritative for a particular evaluation.

## Rewards

Each board has a configurable reward allocation, shared by its current and previous eligible
champion. Rank shares are configurable too (the development default is 80/20). One submission may
hold several boards. Missing/ineligible ranks go to the treasury. Board reward weights are distinct
from the model/hardware weights used to measure performance. Consult the live champion/publication
fields for current shares; a displayed allocation is not proof of an on-chain payment.

For quick measurements see the [manual](../MANUAL.md#5-benchmark-locally).
For controlled local studies and noise interpretation see [TIMING.md](TIMING.md).
