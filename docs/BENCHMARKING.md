# Benchmarking and scoring

One entry contains all kernels. Each board compares the complete entry with its own current
champion; winning one board does not substitute individual kernels into another entry.

## Boards and workloads

| Board | Workload | Supported batch sizes |
|---|---|---|
| `mlp` | Fully connected classifier | 1, 8 |
| `resnet` | Residual CNN | 1, 4 |
| `mobilenet` | Depthwise/pointwise CNN | 1, 4 |
| `normalized_mlp` | MLP with RMS and layer normalization | 1, 8 |
| `mixer` | Token/channel mixing block | 1, 4 |
| `recurrent` | LSTM → GRU sequence classifier (real fixtures: LSTM or GRU, one or two layers) | 1, 4 |
| `overall` | Weighted combination of the six scored families | As above |

The scored set targets small devices: MLP, ResNet, MobileNet, normalized MLP, Mixer and
Recurrent. Transformer remains available as an optional synthetic-only local research board
(`--board transformer`), but the default config generator omits it from boards and rewards. To
leave time for the real-model suite, the default synthetic plan scores batch 1 only, across
normal, wide-exponent and sparse cells: three cells per family and 18 for Overall. `--synthetic-full` opts into all supported batch sizes, or
repeat `--synthetic-cell` to specify an exact custom cell set. Config validation rejects a board
whose family has no selected synthetic cell.

A root seed draws **six workloads**: two typical, two scale and two boundary cases, with total
profile weights **50/30/20%**. Shapes and values vary, including odd boundary sizes. The compact
default gives 18 workload/cell combinations per family and 108 for Overall, per environment/root
seed; the full batch matrix doubles that synthetic cost. A local full-matrix orchestration pilot
with three paired rounds and three baseline rounds exhausted its 20-minute benchmark budget before
finishing, so batch-size expansion is opt-in while mixed real/synthetic timing is calibrated.
The synthetic suite measures compact generated inference workloads. When enabled, the real suite
also participates in promotion using the fixed `benchmark.suite_weights` (equal weights by
default). Its trained image, UCI-HAR sensor and appliance-energy time-series fixtures exercise
inference for supported MLP, normalized MLP, ResNet, MobileNet and Mixer boards, and trained
LSTM/GRU sequence models (adapter `recurrent-v1`, `--real-recurrent`) the Recurrent board. Both suite scores
are reported separately. Transformer remains synthetic until a compatible real fixture exists.

Recurrent inference time is dominated by the correctly rounded `sigmoid` and `tanh` (several per
hidden unit per step), so recurrent models are small: hidden sizes up to 64 (fixtures) and
sequences of at most 256 steps (adapter limits), with synthetic cells sized well below that.

A kernel affects a score only through the models that call it. No current model uses
`convTranspose`, a convolution shape other than `ConvShape.simple`, or pooling other than
`PoolShape.square`, so those paths do not change any board's score.

Fresh-seed mode chooses a new root seed for an evaluation; retries retain the same workloads.
Candidate and reference receive identical seeds, shapes, parameters and inputs. The required
hardware environments and their weights are part of the published board configuration.
The generator is pinned in [ExactSuite.lean](../lean/bench/ExactSuite.lean).

## Measurement

1. Prepare shared Lean modules, then build the baseline, candidate and current champion/reference
   under separately recorded stage clocks. Each stage has its own configurable cap, while the
   environment's overall build limit remains a final ceiling. Valid cached binaries skip their
   corresponding work. Build time is not scored.
2. Each repeated measurement pass runs in a fresh subprocess. That pass evaluates each selected
   cell/workload once; before each scored inference, it runs one unscored warm-up with a separately
   selected input in the same process. The next repetition gets a new process, so submitted state
   cannot carry between repetitions of the same row. The coordinator persists the hidden synthetic
   generator seeds and real sample IDs, including warm-up selections, in the evaluation request.
   Candidate, champion and baseline use the same matched scored inputs; retries reuse the same
   request. Warm-up inputs are distinct from scored inputs. Inputs are prepared outside inference
   timing and are one real example per batch-1 inference. Sample IDs may recur across fresh
   processes when the fixture has fewer examples than observations. Model weights are reloaded on
   each pass; model-load time is reported separately.
3. The harness emits flushed `BENCH_READY` and `BENCH_DONE` markers around each inference. The
   trusted parent measures elapsed time between receiving those markers with its monotonic clock;
   the child-reported duration is diagnostic only. Model loading, input preparation and output
   hashing remain outside inference timing. Run paired rounds, alternating execution order and
   using one CPU affinity where supported. Report the median across configured independent passes.
4. Check matching output checksums. The baseline is measured separately on fewer rounds, once per
   fresh process; its timings are diagnostic and do not affect promotion. The exact
   baseline source hash and its per-request timings are stored with each measurement result.
5. Result telemetry reports shared preparation and baseline, reference and candidate build times
   separately, each role's cache lookup separately from actual build work, plus the overall build
   duration and configured phase caps. Process startup and all measurement preparation remain
   visible in the overall stage duration.
   Real rows report model load and input-selection preparation separately, retain sampled fixture
   indices, and include raw per-pass timings.

The real suite is opt-in and uses local fixtures only. Add one or more switches to
`scripts/exact-config.py --output <path>`: `--real-image-zoo`, `--real-uci-har`, or
`--real-appliance-mixer`. Use `--synthetic-full` for all batch sizes, or `--synthetic-cell` (repeat
the option) to define a custom cell set. By default, fixtures are read from
`data/benchmark/assets/`; choose another repository-local bundle with `--assets-dir PATH` or
`TORCHLEAN_BENCHMARK_ASSETS=PATH`. The generated config records each selected asset's hash. Use no
real-suite switch for synthetic-only configuration when assets are unavailable. Every benchmark
worker must have the same relative asset paths and hashes before real workloads are enabled. The
selected adapter validates tensor counts and dimensions before timing. Assets are mounted read-only
into each fresh benchmark process; workers do not fetch assets from the network. The compact
image-zoo, UCI-HAR and appliance fixtures are small task-trained research models, not claims of
externally pretrained SOTA coverage.

The current local bundle contains complete held-out input pools: 360 sklearn Digits examples,
10,000 official MNIST test images, 10,000 official Fashion-MNIST test images, 2,947 UCI-HAR test
windows, and 2,932 chronological Appliance Energy test windows. A submission still draws only its
configured number of examples (12 rounds by default); the larger pools expand the hidden sampling
space without increasing the number of timed inferences.

The local bundle is also prepared as the root of a Hugging Face dataset repository. Its per-fixture
manifests pin paths and checksums; `scripts/index-benchmark-assets.py` verifies those files and
regenerates the top-level catalog. To download the private repo, set
`HF_TOKEN_TORCHLEAN_BENCHMARK` in the environment or ignored `.env` file, then run
`scripts/download-benchmark-assets.py --revision COMMIT`. It downloads to
`data/benchmark/assets/` by default and verifies every asset checksum. Use `--destination PATH` for
a separate local copy. Pin an immutable Hub commit for deployment rather than `main`.

The initial development profile uses **12 paired rounds**, one fresh process per side per round,
and **3 baseline diagnostic rounds**. A three-round self-comparison was too noisy for admission;
the 11-round control was substantially more stable, and an even count balances which side runs
first. Repetition counts are configurable with
`--rounds`, `--inner`, `--baseline-rounds` and `--min-rounds`; `--quick` emits a one-round smoke
configuration. These are pilot defaults, not final production policy. Read
`/v1/competitions/tensor-kernels/boards`
and submission reports for live policy and frozen evaluation settings. Full processing also includes
proof checking, native builds and queue waiting; it is longer than the timed inference.
Default measurement ceilings are 20 minutes for overall and 6 minutes for one family;
[worker budgets](WORKERS.md#independent-build-and-measurement-budgets) distinguish cold and cached preparation.

## Score and promotion

For each hardware/root-seed stratum:

- Cell ratio = candidate time / reference time. Below 1 is faster.
- For each workload and round, combine cell ratios by weighted geometric mean.
- Take the median across rounds for that workload.
- Combine synthetic workload medians using their profile weights. For real workloads, take the
  median paired candidate/champion ratio per model, combine models equally within each family,
  then weight families using the board's model weights. Combine enabled suite scores using
  `benchmark.suite_weights`, then combine hardware/root-seed scores by weighted geometric mean.
  Raw nanoseconds from different CPUs are never pooled.

The overall board's model weights are configurable; the development default weights models equally.
A family board scores only that family's cells.

Promotion requires **all** configured checks:

- Enough paired rounds and a score at or below the promotion bar (development default `0.97`).
- Positive lower-bound gain from a stratified bootstrap (default 2,000 resamples, fifth percentile).
  It resamples workloads within profiles and paired round indices, preserving weights.
- Every synthetic workload/cell and real model/environment median ratio within the regression
  guard (default `1.10`).

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

Each of the seven boards has a configurable reward allocation paid entirely to its current eligible
champion. The default rank shares are `[1.0, 0.0]`: previous champions retain their history but
receive no reward. One submission may hold several boards. An unheld board’s allocation goes to
the treasury. Board reward weights are distinct
from the model/hardware weights used to measure performance. Consult the live champion/publication
fields for current shares; a displayed allocation is not proof of an on-chain payment.

For quick measurements see the [manual](../MANUAL.md#5-benchmark-locally).
For controlled local studies and noise interpretation see [TIMING.md](TIMING.md).
