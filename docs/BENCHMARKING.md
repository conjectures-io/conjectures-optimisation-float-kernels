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
| `transformer` | Pre-norm block: causal multi-head attention, SiLU MLP | 1, 4 |
| `overall` | Weighted combination of all six families | As above |

Each model/batch runs on normal, wide-exponent and sparse inputs: six cells per family,
36 for overall. A root seed draws **six workloads**: two typical, two scale and two boundary
cases, with total profile weights **50/30/20%**. Shapes and values vary, including odd boundary
sizes. This gives 36 workload/cell combinations per family, 216 for overall, per environment/root seed.
The promotion suite is synthetic inference, not trained-model accuracy or backpropagation. An
optional, local diagnostic suite can add trained image, UCI-HAR sensor and appliance-energy
time-series fixtures for supported MLP, normalized MLP, ResNet, MobileNet and Mixer boards. Those
rows are reported separately and do not change admission. Transformer remains synthetic until a
compatible real fixture exists.

A kernel affects a score only through the models that call it. No current model uses
`convTranspose`, a convolution shape other than `ConvShape.simple`, or pooling other than
`PoolShape.square`, so those paths do not change any board's score.

Fresh-seed mode chooses a new root seed for an evaluation; retries retain the same workloads.
Candidate and reference receive identical seeds, shapes, parameters and inputs. The required
hardware environments and their weights are part of the published board configuration.
The generator is pinned in [ExactSuite.lean](../lean/bench/ExactSuite.lean).

## Measurement

1. Build the shipped baseline, candidate and current champion once for the target CPU, then reuse
   validated cached binaries across family jobs. Build time is not scored.
2. Each timed cell/workload runs once per fresh subprocess, so submitted global state cannot carry
   from one repetition of that cell into the next. Inputs are prepared outside timing and are
   distinct within and across repetitions. Champion and candidate receive the same matched input
   sequence.
3. Run paired rounds, alternating execution order and using one CPU affinity where supported. The
   reported duration is the median across configured independent subprocesses.
4. Check matching output checksums. The baseline is measured separately on fewer rounds, once per
   fresh process; its timings are diagnostic and do not affect promotion. The exact
   baseline source hash and its per-request timings are stored with each measurement result.
5. Model/input preparation and output hashing are outside kernel timing; process startup and all
   measurement preparation remain visible in the overall stage duration. Real rows report model
   load and input-selection preparation separately, retain sampled fixture indices, and include raw
   inner timings.

The real suite is opt-in and uses local fixtures only. Add one or more switches to
`scripts/exact-config.py --output <path>`: `--real-image-zoo`, `--real-uci-har`, or
`--real-appliance-mixer`. Each switch requires the corresponding local files under
`data/real-model-backtest/`; the generated config records each selected asset's hash. Use no switch
for synthetic-only configuration when real assets are unavailable. Every benchmark worker must
have the same relative asset paths and hashes before real workloads are enabled. The selected
adapter validates tensor counts and dimensions before timing. Assets are mounted read-only into
each fresh benchmark process; no network fetch occurs. The compact image-zoo, UCI-HAR and appliance
fixtures are small task-trained research models, not claims of externally pretrained SOTA coverage.

The development config generator uses **11 paired rounds, 3 fresh processes per candidate and
reference round, and 3 baseline diagnostic rounds**. The baseline runs once per selected round.
These are local configuration defaults, not a promise about production. Read
`/v1/competitions/torchlean/boards`
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

An entry that runs out of time is `timed-out`: either one run passed its own cap (`run_seconds`,
`limit: "invocation"`), or the paired rounds used up the shared budget (`benchmark_seconds`,
`limit: "budget"`) and the entry's runs took longer than the reference's. Sides are compared by
mean time per finished run, not by total time, since one side may have finished one run more
when the budget ran out; the stopped run only bounds its own side's time from below. That is a
measured result, not an error. The decision's `timeout` names the phase, the limit, the
allowance, the cell that was running, how many cells and rounds had finished, each side's
finished runs with their mean time, and how long the stopped run had been going. The failed job
keeps what the worker had measured by then: completed rounds, the round in
progress and the stopped run's finished rows. A build that runs out of time is also `timed-out`,
with `phase: "build"`. `benchmark-failed` is kept for everything else that stops an entry: a
crash, a failed native build, or invalid output.

## Rewards

Each of the six boards has a configurable reward allocation paid entirely to its current eligible
champion. The default rank shares are `[1.0, 0.0]`: previous champions retain their history but
receive no reward. One submission may hold several boards. An unheld board’s allocation goes to
the treasury. Board reward weights are distinct
from the model/hardware weights used to measure performance. Consult the live champion/publication
fields for current shares; a displayed allocation is not proof of an on-chain payment.

For quick measurements see the [manual](../MANUAL.md#5-benchmark-locally).
For controlled local studies and noise interpretation see [TIMING.md](TIMING.md).
