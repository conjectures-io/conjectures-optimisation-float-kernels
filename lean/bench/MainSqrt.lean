import BenchDivSqrt
import Submission

/-! The square-root slot's bench binary: the shared core over `Submission.sqrt`, nothing else. -/

def main : IO UInt32 := Bench.runSqrt Submission.sqrt
