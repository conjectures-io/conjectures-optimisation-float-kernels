import BenchBmm
import Submission

/-! The bmm slot's bench binary: the tensor core over `Submission.bmm`, nothing else. -/

def main : IO UInt32 := Bench.runBmm Submission.bmm
