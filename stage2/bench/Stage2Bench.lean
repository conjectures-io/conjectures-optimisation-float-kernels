import Stage2.Kernels

/-!
# Stage-2 spike bench: reference ops vs stage 1's winners, same fold order

`bmmLoops` is unproved: plain nested row-major loops with no index decoding, the winners' scalar
ops, to see what loop structure alone is worth beyond composition. Checksums must agree with the
reference.
-/

open FloatLib.Floats FloatLib.Floats.Formats.BinaryInterchange Stage2.Kernels

def lcg (n : Nat) (seed : UInt32) : Array UInt32 := Id.run do
  let mut a : Array UInt32 := Array.mkEmpty n
  let mut s := seed
  for _ in [0:n] do
    s := s * 1664525 + 1013904223
    a := a.push s
  return a

def nearOne (w : UInt32) : Binary32 :=
  Configured.NativeFPU.ofBinary32Bits
    ((w &&& (0x807FFFFF : UInt32)) ||| (((124 : UInt32) + (w >>> 23) % 7) <<< 23))

def operands (n : Nat) (seed : UInt32) : Vector Binary32 n :=
  let a := lcg n seed
  Vector.ofFn fun i => nearOne (a.getD i 0)

def bits (x : Binary32) : UInt32 := x.raw.1

def hash {n : Nat} (v : Vector Binary32 n) : UInt32 :=
  v.foldl (fun h x => (h ^^^ bits x) * 16777619) 0

/-- Unproved probe: `bmmSpec`'s order for batch 1, as nested loops over a flat output. -/
def bmmLoops (m n p : Nat) (A B : Array Binary32) : Array Binary32 := Id.run do
  let mut C : Array Binary32 := Array.mkEmpty (m * p)
  for i in [0:m] do
    for j in [0:p] do
      let mut acc : Binary32 := zero
      for k in [0:n] do
        acc := TunedAdd.add acc (FastMul.mul A[i * n + k]! B[k * p + j]!)
      C := C.push acc
  return C

def best (reps : Nat) (f : Unit → IO UInt32) : IO (Nat × UInt32) := do
  let mut b := 0
  let mut last := 0
  for _ in [0:reps] do
    let t0 ← IO.monoNanosNow
    last ← f ()
    let t1 ← IO.monoNanosNow
    b := if b == 0 || t1 - t0 < b then t1 - t0 else b
  return (b, last)

def report (name : String) (ops : Nat) (base : Nat) (r : Nat × UInt32) : IO Unit := do
  let ratio := if base == 0 then "" else s!", ratio {(r.1 * 1000 / base).toFloat / 1000}"
  IO.println s!"{name}: {r.1 / 1000} us, {r.1 / ops} ns/op, checksum {r.2}{ratio}"

def main (args : List String) : IO Unit := do
  let reps := (args.head? >>= String.toNat?).getD 5
  for n in [16384, 262144] do
    let x := operands n 0x1234
    let r ← best reps fun _ => pure (bits (sumRef x))
    report s!"sum ref  n={n}" n 0 r
    report s!"sum fast n={n}" n r.1 (← best reps fun _ => pure (bits (sumFast x)))
  for s in [64, 128] do
    let A := operands (1 * s * s) 0x9abc
    let B := operands (1 * s * s) 0xdef0
    let ops := s * s * s
    let r ← best reps fun _ => pure (hash (bmmRef 1 s s s A B))
    report s!"bmm ref   {s}³" ops 0 r
    report s!"bmm fast  {s}³" ops r.1 (← best reps fun _ => pure (hash (bmmFast 1 s s s A B)))
    report s!"bmm loops {s}³ (unproved)" ops r.1 (← best reps fun _ =>
      pure ((bmmLoops s s s A.toArray B.toArray).foldl (fun h x => (h ^^^ bits x) * 16777619) 0))
