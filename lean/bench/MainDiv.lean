import BenchDivSqrt
import Submission

/-! The division slot's bench binary: the shared core over `Submission.div`, nothing else. -/

def main : IO UInt32 := Bench.runDiv Submission.div
