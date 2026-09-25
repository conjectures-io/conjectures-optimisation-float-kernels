import Bench
import Submission

/-! The multiplication slot's bench binary: the shared core over `Submission.mul`, nothing else. -/

def main : IO UInt32 := Bench.runMul Submission.mul
