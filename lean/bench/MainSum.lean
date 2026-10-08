import BenchTensor
import Submission

/-! The sum slot's bench binary: the tensor core over `Submission.sum`, nothing else. -/

def main : IO UInt32 := Bench.runSum Submission.sum
