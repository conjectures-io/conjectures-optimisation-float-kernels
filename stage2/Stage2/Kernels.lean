import Scalar.TunedAdd
import Scalar.FastMul

/-!
# Stage-2 spike kernels: TorchLean's accumulation order over stage 1's scalar winners

Same fold order as the specs, with `ExecFloat.add`/`mul` swapped for the best proven stage-1
kernels (`tuned` add, `mul-fast` mul). Imports FloatLib only, so a bench links without TorchLean.
Two variants of each, the reference ops and the fast ones, to see what composition alone buys.
-/

namespace Stage2.Kernels

open FloatLib.Floats

abbrev Binary32 := ExecFloat.Binary (exponentBits := 8) (fractionBits := 23)

/-- The +0 every fold starts from. -/
abbrev zero : Binary32 := ExecFloat.Binary.zero false

/-- A left sum with the given adder. -/
@[inline] def sumWith (add : Binary32 → Binary32 → Binary32) {n : Nat} (x : Vector Binary32 n) :
    Binary32 :=
  x.foldl add zero

def sumRef {n : Nat} (x : Vector Binary32 n) : Binary32 := sumWith ExecFloat.add x
def sumFast {n : Nat} (x : Vector Binary32 n) : Binary32 := sumWith TunedAdd.add x

/-- The spec's decoding of a flat output index into `(b, i, j)`. -/
@[inline] def decode (m p : Nat) (q : Nat) : Nat × Nat × Nat :=
  let rowSize := m * p
  let b := if rowSize = 0 then 0 else q / rowSize
  let r := if rowSize = 0 then 0 else q % rowSize
  let i := if p = 0 then 0 else r / p
  let j := if p = 0 then 0 else r % p
  (b, i, j)

/-- Batched matmul in `bmmSpec`'s order: each output a left fold over `k` from +0. -/
@[inline] def bmmWith (add mul : Binary32 → Binary32 → Binary32) (batch m n p : Nat)
    (A : Vector Binary32 (batch * m * n)) (B : Vector Binary32 (batch * n * p)) :
    Vector Binary32 (batch * m * p) :=
  Vector.ofFn fun q =>
    let (b, i, j) := decode m p q.val
    Fin.foldl n
      (fun acc k =>
        add acc (mul (A.toArray.getD ((b * m + i) * n + k.val) zero)
          (B.toArray.getD ((b * n + k.val) * p + j) zero)))
      zero

def bmmRef := bmmWith ExecFloat.add ExecFloat.mul
def bmmFast := bmmWith TunedAdd.add FastMul.mul

end Stage2.Kernels
