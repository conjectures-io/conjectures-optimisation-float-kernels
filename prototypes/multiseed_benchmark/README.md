# Six-workload benchmark prototype

This branch preserves the implementation exercised locally on 23 October 2026.
It is an isolated prototype; worker/coordinator integration remains to be done.

## Included code

- `lean/bench/ExactSuite.lean`: seeded typical, scale/aspect-ratio and boundary
  shape profiles. Numerical inputs retain normal/wide/sparse distributions.
- `lean/bench/MainExact.lean`: batched workloads, prepared outside timed inference.
- `lean/audit/ValidateSuite.lean`: checks all three shape profiles and records
  profile names in its intermediate-output diagnostics.
- `calibration.py`: the experiment's repetition-count calculation and its
  reference-only two-pass, best-of-three warm calibration.
- `scoring.py`: weighted per-workload median scores and a stratified bootstrap
  over workload seeds and shared paired timing rounds.

The calibration/scoring helpers have no database side effects.

## Experiment settings

Six independent seeds: two workloads per profile. Profile weights are typical
50%, scale 30% and boundary 20%; each seed receives half its profile's weight.
The experiment used 11 alternating paired rounds, best of three inner runs,
and a nominal 50 ms target per cell. The overall board weights the five model
families equally. Each model-family board can also be summarized separately.

Admission retains a 0.97 score bar, a positive one-sided 95% bootstrap gain,
and a 1.10 maximum cell-median ratio. Bootstrap intervals are 5th–95th ratio
percentiles; their upper endpoint supplies the one-sided admission bound.
A paired round's resampled indices are shared across workloads to preserve
batch-level timing covariance.

## Batch protocol

`BENCH_WORKLOADS` contains comma-separated `id:uint32_seed:profile` entries,
for example `s0:42:typical,s1:43:typical,s2:44:scale,s3:45:scale,s4:46:boundary,s5:47:boundary`.
The allowed profiles are `legacy`, `typical`, `scale` and `boundary`.

`BENCH_CELLS` selects model cells. `BENCH_REPS` contains entries such as
`s0|mlp:b1:normal=4`; absent entries default to one repetition. Output rows are
`workload|cell true total_nanoseconds checksum`.

`scoring.summarize(session, family)` consumes the local collector's completed
JSON session: `workloads` contains `id`, `seed`, `profile` and `weight`;
`rounds` contains `cells` keyed by `workload|cell`, with `reference_ns` and
`candidate_ns`. The collector validates complete cell sets and equal
candidate/reference checksums before scoring.

The native program defaults to `s0:42:legacy` when `BENCH_WORKLOADS` is absent.
It does not implement the live worker's existing single-seed protocol. Do not
deploy this branch's benchmark without adapting its caller and parser.

## Local evidence

The main checkout retains Git-ignored artifacts under
`data/six-seed-benchmark/20261023/`: raw data, binaries, tests, collection and
plot scripts, `EXPERIMENT.md` and `REPORT.md`. They are not included in this
branch. The sources and native program were built against commit
`a93ca6ecd0cfec2dae1d89cd2005c31a18de5c43`.

All 16 confirmation sessions completed, with 288 paired rounds and 21,840
matching checksum comparisons. Four packages passed Lean kernel replay;
3,078 generated model executions passed shape and intermediate-output checks.
Fourteen local tests passed. The final comparison excludes an archived cold
calibration trial and records the WSL restart between parts of the collection.

The new benchmark repeatedly detected both convolution improvements and the
pointwise MobileNet improvement. The pointwise Mixer win did not repeat.
No incumbent copy qualified, but two copy sessions per version cannot establish
a rare false-positive rate. Full-suite runtime averaged 13.34 minutes versus
7.06 minutes previously; the new maximum was 14.28 minutes.

Before deployment: integrate job/request formats, benchmark identities and
coordinator scoring; validate family-only worker runs and budget headroom;
collect more independent controls. The new shape profiles have runtime
validation, but no additional formal sampler-validity proofs yet.
