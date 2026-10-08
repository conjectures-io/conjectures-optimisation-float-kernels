import Contract

/-!
# The tensor contract: what a submitted sum or batched matmul must prove

A module of its own, imported only by the tensor slots: growing `Contract.lean` changed how the
scalar kernels compile and slowed `add/direct` from 0.80x to 0.96x of the incumbent, so the scalar
contract stays byte for byte the calibrated one. Pinned by the operator like `Contract`; a `sum` or
`bmm` submission imports this module and nothing else. Every definition is TorchLean's, restated
over FloatLib so the gate needs no TorchLean; `lean/stage2/Stage2/Link.lean` proves each one equal
to TorchLean's own by definition.
-/

namespace Competition

open FloatLib.Floats

/-- The +0 a sum starts from. -/
abbrev posZero : Binary32 := ExecFloat.Binary.zero false

/-- The sum slot's reference: TorchLean's `reduceSumLeftSpec`, a left fold of `ExecFloat.add` from
+0 over the buffer's indices in order. Restated here so the gate needs no TorchLean;
`lean/stage2/Stage2/Link.lean` proves it is TorchLean's own definition. -/
def sumSpec {n : Nat} (x : Fin n → Binary32) : Binary32 :=
  (List.finRange n).foldl (fun acc i => ExecFloat.add acc (x i)) posZero

/-- The sum slot's obligation: a kernel equals TorchLean's left-to-right sum on every vector of
every length, bit for bit; the order of accumulation is part of the rule. -/
def SumObligation (kernel : {n : Nat} → Vector Binary32 n → Binary32) : Prop :=
  ∀ (n : Nat) (x : Vector Binary32 n), kernel x = sumSpec x.get

/-- TorchLean's total read of a flat buffer: the element in bounds, +0 outside. -/
def getD {n : Nat} (x : Fin n → Binary32) (i : Nat) : Binary32 :=
  if h : i < n then x ⟨i, h⟩ else posZero

/-- Row-major index of `A[b, i, k]` in a `(batch, m, n)` buffer. -/
def bmmAIndex (m n : Nat) (b i k : Nat) : Nat :=
  (b * m + i) * n + k

/-- Row-major index of `B[b, k, j]` in a `(batch, n, p)` buffer. -/
def bmmBIndex (n p : Nat) (b k j : Nat) : Nat :=
  (b * n + k) * p + j

/-- A flat row-major output index of a `(batch, m, p)` buffer, decoded into `(b, i, j)`. -/
def bmmDecodeC (m p : Nat) (q : Nat) : Nat × Nat × Nat :=
  let rowSize := m * p
  let b := if rowSize = 0 then 0 else q / rowSize
  let r := if rowSize = 0 then 0 else q % rowSize
  let i := if p = 0 then 0 else r / p
  let j := if p = 0 then 0 else r % p
  (b, i, j)

/-- The bmm slot's reference: TorchLean's `bmmSpec`, row-major batched matrix multiply where every
output `C[b,i,j]` is a left fold over `k`, from +0, of `add acc (mul A[b,i,k] B[b,k,j])`. -/
def bmmSpec (batch m n p : Nat) (A : Fin (batch * m * n) → Binary32)
    (B : Fin (batch * n * p) → Binary32) : Fin (batch * m * p) → Binary32 :=
  fun q =>
    let (b, i, j) := bmmDecodeC m p q.val
    (List.finRange n).foldl
      (fun acc k =>
        let a := getD A (bmmAIndex m n b i k.val)
        let bVal := getD B (bmmBIndex n p b k.val j)
        ExecFloat.add acc (ExecFloat.mul a bVal))
      posZero

/-- The bmm slot's obligation: every output element equals TorchLean's, on every shape, bit for
bit; the order of accumulation over `k` is part of the rule. -/
def BmmObligation
    (kernel : (batch m n p : Nat) → Vector Binary32 (batch * m * n) →
      Vector Binary32 (batch * n * p) → Vector Binary32 (batch * m * p)) : Prop :=
  ∀ (batch m n p : Nat) (A : Vector Binary32 (batch * m * n)) (B : Vector Binary32 (batch * n * p)),
    (kernel batch m n p A B).get = bmmSpec batch m n p A.get B.get

end Competition
