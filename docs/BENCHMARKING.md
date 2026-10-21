# Benchmarking tensor implementations

A measurement compares two complete kernel sets on one environment and seed.
Both binaries are built locally from immutable source hashes under the same pinned benchmark.
The reference calibrates cell repetitions. Paired alternating rounds use identical cells,
repetitions and seed; checksums must match.

Each evaluation freezes model-cell weights, required hardware/seed strata, timing settings and
admission thresholds. Hardware timings are compared as ratios within their own stratum, never
as raw times across CPUs. The scoring module aggregates these ratios and performs stratified
bootstrap admission and per-cell regression checks. Each board has its own full reference source.

Reward allocations are separate from performance aggregation. Editing reward weights does not
invalidate proofs or measurements. Changing an active board's required hardware replaces the
pending plan and links compatible completed observations.

To collect real evidence from the distributed workflow, use
[scripts/test-exact-workflow.py](../scripts/test-exact-workflow.py).
For a timing study with frozen binaries and plots, see [EXACT-TIMING.md](EXACT-TIMING.md).
Quick workflow runs do not calibrate production admission thresholds.
