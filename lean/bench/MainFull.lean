import BenchFull
import BenchSuite
import Submission

/-! The full entry's bench binary: all four kernels of `Submission`, nothing else. With a plan in
`BENCH_PLAN` it times the suite's cases (`BenchSuite`), without one the legacy classes. -/

def main : IO UInt32 := do
  if (← IO.getEnv "BENCH_PLAN").isSome then
    Bench.Suite.run Submission.add Submission.mul Submission.sum Submission.bmm
  else
    Bench.runFull Submission.add Submission.mul Submission.sum Submission.bmm
