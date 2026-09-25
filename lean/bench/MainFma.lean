import Bench
import Submission

/-! The fused multiply-add slot's bench binary: the shared core over `Submission.fma`. -/

def main : IO UInt32 := Bench.runFma Submission.fma
