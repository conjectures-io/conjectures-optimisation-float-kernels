import BenchFull
import Submission

/-! The full entry's bench binary: all four kernels of `Submission`, nothing else. -/

def main : IO UInt32 :=
  Bench.runFull Submission.add Submission.mul Submission.sum Submission.bmm
