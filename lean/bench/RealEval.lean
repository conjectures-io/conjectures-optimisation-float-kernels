import RealSuite
import Submission

/-! Offline evaluation of a real-model fixture over its whole sample pool (not part of the
benchmark or the gate).

    realEval ADAPTER FAMILY WEIGHTS SAMPLES WORDS [FIRST [COUNT]]

prints, for each sample row, `index word₀ word₁ …` (the output's binary32 words in decimal). Rows are read from the raw little-endian float32 sample file (`WORDS` words each). It
runs the entry in `lean/work/Submission.lean`, which is proved equal to the spec, so its outputs
are the spec's: training scripts compare them with PyTorch and score them against labels. -/

open ExactRuntime

def readWord (bytes : ByteArray) (offset : Nat) : Word :=
  bytes[offset]!.toUInt32 ||| (bytes[offset + 1]!.toUInt32 <<< 8) |||
    (bytes[offset + 2]!.toUInt32 <<< 16) ||| (bytes[offset + 3]!.toUInt32 <<< 24)

def main (args : List String) : IO UInt32 := do
  match args with
  | adapter :: family :: weights :: samples :: words :: rest =>
    let some size := words.toNat? | throw (IO.userError "WORDS must be a number")
    let tensors ← match RealSuite.readTensors (← IO.FS.readBinFile weights) with
      | .ok t => pure t
      | .error e => throw (IO.userError e)
    let layers ← match RealSuite.prepare adapter family size tensors with
      | .ok l => pure l
      | .error e => throw (IO.userError e)
    let bytes ← IO.FS.readBinFile samples
    if size == 0 || bytes.size % (4 * size) != 0 then
      throw (IO.userError "sample file is not a whole number of rows")
    let rows := bytes.size / (4 * size)
    let first := (rest.head?.bind String.toNat?).getD 0
    let count := ((rest.drop 1).head?.bind String.toNat?).getD (rows - first)
    let stdout ← IO.getStdout
    for row in [first:min rows (first + count)] do
      let input := Array.ofFn (n := size) fun i => readWord bytes (4 * (row * size + i.val))
      let out := runModel Submission.kernels layers 1 size input
      stdout.putStrLn s!"{row} {" ".intercalate (out.toList.map (toString ·.toNat))}"
      stdout.flush
    return 0
  | _ =>
    IO.eprintln "usage: realEval ADAPTER FAMILY WEIGHTS SAMPLES WORDS [FIRST [COUNT]]"
    return 2
