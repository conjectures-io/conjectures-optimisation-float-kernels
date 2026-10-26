# Benchmarking tensor implementations

A measurement compares two complete kernel sets on one environment. A root seed draws six
workloads: two typical, two scale and two boundary cases (profile weights 50/30/20%). The
coordinator stores every workload's seed, profile and weight in the immutable job request.
Fresh mode draws a root seed per evaluation; retries retain it. Both devices use the same
workload plan for a given root seed.
Both binaries are built locally from immutable source hashes under the same pinned benchmark.
The reference alone calibrates each workload/cell in two passes, best of three, rounding
repetition counts up to the target. Paired alternating rounds use identical workloads and
counts; every output checksum must match. Output keys are `workload|model:batch:kind`.
The generated development configuration uses 11 rounds, best of three, 50 ms cells, a separate
960 s build phase and 1200 s calibration/measurement phase. The ceiling includes
model preparation and process overhead as well as timed inference; it is not a target duration. A full overall job covers 180 cells;
a single-family job covers 36. Production thresholds still require a host timing study.

Each evaluation freezes model-cell weights, required hardware/seed strata, timing settings and
admission thresholds. Hardware timings are compared as ratios within their own stratum, never
as raw times across CPUs. The scoring module aggregates these ratios and performs stratified
bootstrap admission and per-workload/cell regression checks. Each workload is summarized by
the median of its round geometric means; workload and device scores are combined by weighted
geometric mean. Bootstrap resamples workloads within each profile and paired round indices
(shared across the workloads in a batch), retaining the profile weights. Admission requires
score <= the bar, positive lower-bound gain and every guard passing. Each board has its own full reference source.

Reward allocations are separate from performance aggregation. Editing reward weights does not
invalidate proofs or measurements. Changing an active board's required hardware replaces the
pending plan and links compatible completed observations.

To collect real evidence from the distributed workflow, use
[scripts/test-exact-workflow.py](../scripts/test-exact-workflow.py).
For a timing study with frozen binaries and plots, see [EXACT-TIMING.md](EXACT-TIMING.md).
Quick workflow runs do not calibrate production admission thresholds.
