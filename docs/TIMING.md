# Timing studies

A quick local timing is useful for finding large improvements. Choosing promotion thresholds or
claiming a small gain requires repeated paired measurements with controls.

## Recommended experiment

1. Finish all builds before timing. Record source hashes, benchmark identity, CPU/compiler,
   workload seeds, calibration counts and execution order.
2. Include the reference binary against itself. A copy should score near 1; if it appears to gain
   several percent, investigate noise before interpreting similarly sized optimizations.
3. Alternate starting order. Repeat identical inputs to measure timing variance, then use fresh
   workload draws to measure generalization. Keep these two questions separate.
4. Compare a candidate directly with the relevant board champion, not only the shipped package.
   Each board can have a different complete reference.
5. Confirm promising changes on every required CPU environment using the current
   [multiseed scoring protocol](BENCHMARKING.md). A laptop result is not a distributed score.

Build time, timed inference, process overhead and queue waiting are different durations. Preserve
separate stage timings. A cached architecture-local executable can avoid a rebuild; the proof
check is for the complete submitted package, not repeated separately for each model family.

## Plots and summaries

- Per-family candidate/reference ratios make specialization visible; show 1.0 and the promotion bar.
- Boxes or intervals for timing repeatability must use repeated measurements of **identical inputs**.
  Keep different seeds and devices separate, or explicitly describe a hierarchical analysis.
- Plot admission's lower gain bound separately from runtime spread. It is not a boxplot whisker
  or a two-sided confidence interval.
- Show individual rounds, starting order and the identical-binary control to expose drift.
- Compare benchmark duration as well as sensitivity to genuine improvements and false positives.

Keep raw measurements, summaries and plots in separate directories. Do not discard noisy or failed
runs silently. The [local experiment guide](LOCAL-EXPERIMENTS.md) describes development setup.

## Earlier study tools

`scripts/calibrate-exact-timing.py`, `scripts/plot-exact-timing.py` and
`scripts/compare-model-families.py` retain earlier **single-workload scoring** experiments.
Their automatic fixture substitutions can depend on an earlier incumbent's source layout.
They are research tools, not a reproduction of current production admission; inspect their
assumptions before reusing them with a changed kernel package. Current service rehearsals use
`scripts/test-exact-workflow.py` and the coordinator/benchmark worker path.
