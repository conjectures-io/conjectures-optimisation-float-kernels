import Bench
import Submission

/-! The submission's bench binary: the shared core over `Submission.add`, nothing else. -/

def main : IO UInt32 := Bench.run Submission.add
