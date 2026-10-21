# Exact-runtime timing pilot

This local experiment checks measurement noise and replays the existing score, regression guard
and statistical admission. It does not modify the competition or submit anything to a running
validator.

## Run

After `./setup.sh --service`, from the repository root:

```bash
.venv/bin/python scripts/calibrate-exact-timing.py \
  --output data/exact-calibration/my-pilot --minutes 29 --cpu 3
.venv/bin/python scripts/plot-exact-timing.py data/exact-calibration/my-pilot
```

Plotting additionally requires `numpy` and `matplotlib` in the Python environment. Choose an
available logical CPU and an unused output directory. The collector holds `gate.lock`, builds
the trusted incumbent and `slowdepthwise` fixtures, copies their executables, and restores the
previous `lean/work/Submission.lean` contents. No build runs during timing.

The plan consists of:

- Two seeds, each measured in two nine-round self-comparison sessions on identical inputs.
- The incumbent against `slowdepthwise`, then the reverse comparison, seven rounds each.
- One default 25-round self-comparison.

Self comparisons use 100 ms calibration and best of three inner runs. Different-code controls
use 25 ms and best of two to keep the pilot short. Each pair alternates starting order; repeat
sessions reverse their initial parity. The collector stops between pairs as its budget runs out;
incomplete sessions are retained but excluded from scoring conclusions.

The faster control is the current incumbent compared with a weaker proved reference. It is not
a new optimization of the incumbent, nor a test of a marginal 3% improvement.

## Read the results

Open `index.html` for the plot gallery and `REPORT.md` for results and limitations.

- **Session scores:** median round scores and two-sided 95% round-bootstrap intervals.
- **Round traces:** repeated sessions grouped by their identical input seed.
- **Execution order:** actual rounds grouped by which side ran first.
- **Cell noise:** median paired ratios for each of the 30 cells.
- **Regression guard:** the actual gate's group ratios, computed from separate minima.
- **Calibration and runtime:** achieved cell durations and wall time.
- **Absolute timing drift:** normalized process timings, showing shifts hidden by ratios.

Raw records live in `raw/`, numeric summaries in `summaries/`, and PNG/SVG plots in
`visualizations/`. The raw records retain best-of-inner observations, not individual inner
timings. All outputs under `data/` are ignored by Git; save or archive that directory to retain
the experiment.

Intervals describe repeated rounds of one fixed workload. They do not quantify variation across
seeds or machines, and serial drift can invalidate their exchangeability assumption. A handful
of sessions cannot estimate a rare false-promotion rate. Repeat the study on the intended
validator machine before choosing production thresholds.
