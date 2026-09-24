import Bench
import FloatLib.Floats.Formats.BinaryInterchange.Configured.NativeFPU.Unchecked

/-! Where the nanoseconds go: the public add, the word kernel, the host FPU, and the loop alone,
on two operand classes: random bit patterns (mostly far-apart exponents) and values near one. -/

open FloatLib.Floats Competition
open FloatLib.Floats.Formats.BinaryInterchange

/-- Operands of one `Bench` class: its generator over an LCG sweep of `n` words. -/
def gen (f : Nat → UInt32 → UInt32) (n : Nat) (seed : UInt32) : Array UInt32 :=
  (Bench.lcg n seed).mapIdx f

/-- The fastest of `reps` sweeps, in nanoseconds per operation. -/
def perOp (kernel : Binary32 → Binary32 → Binary32) (xs : Array UInt32) (reps : Nat) : IO Float := do
  return (← Bench.best kernel xs reps).toFloat / (xs.size - 1).toFloat

/-- A textbook binary32 adder in plain words: guard, round and sticky bits, nearest-even. Normal
operands only; zeros, subnormals, infinities, NaNs, subnormal results and overflow fall back to
the certified kernel. Unproved: this is a speed probe, checked against `ExecFloat.add` by checksum. -/
@[inline] def fastAdd (x y : Binary32) : Binary32 :=
  let a : UInt32 := x.raw.1
  let b : UInt32 := y.raw.1
  let ea := (a >>> 23) &&& 0xff
  let eb := (b >>> 23) &&& 0xff
  if ea == 0 || ea == 0xff || eb == 0 || eb == 0xff then ExecFloat.add x y else
  -- order so that (ea, ma) is the larger magnitude
  let swap := ea < eb || (ea == eb && (a &&& 0x7fffff) < (b &&& 0x7fffff))
  let (hi, lo) := if swap then (b, a) else (a, b)
  let eh := (hi >>> 23) &&& 0xff
  let el := (lo >>> 23) &&& 0xff
  let sh := hi >>> 31
  let sl := lo >>> 31
  let mh : UInt64 := (((hi &&& 0x7fffff) ||| 0x800000).toUInt64) <<< 3
  let ml0 : UInt64 := (((lo &&& 0x7fffff) ||| 0x800000).toUInt64) <<< 3
  let d := (eh - el).toUInt64
  if d > 27 then Configured.NativeFPU.ofBinary32Bits hi else
  let ml := (ml0 >>> d) ||| (if (ml0 &&& ((1 <<< d) - 1)) != 0 then 1 else 0)
  if sh == sl then
    let m := mh + ml
    let (m, e) := if m >= 0x8000000 then ((m >>> 1) ||| (m &&& 1), eh + 1) else (m, eh)
    let r := m &&& 7
    let q := m >>> 3
    let q := if r > 4 || (r == 4 && (q &&& 1) == 1) then q + 1 else q
    let (q, e) := if q == 0x1000000 then (q >>> 1, e + 1) else (q, e)
    if e >= 255 then ExecFloat.add x y else
    Configured.NativeFPU.ofBinary32Bits ((sh <<< 31) ||| (e <<< 23) ||| (q.toUInt32 &&& 0x7fffff))
  else
    let m := mh - ml
    if m == 0 then Configured.NativeFPU.ofBinary32Bits 0 else
    let lz := 26 - m.log2
    let m := m <<< lz
    if lz.toUInt32 >= eh then ExecFloat.add x y else
    let e := eh - lz.toUInt32
    let r := m &&& 7
    let q := m >>> 3
    let q := if r > 4 || (r == 4 && (q &&& 1) == 1) then q + 1 else q
    let (q, e) := if q == 0x1000000 then (q >>> 1, e + 1) else (q, e)
    Configured.NativeFPU.ofBinary32Bits ((sh <<< 31) ||| (e <<< 23) ||| (q.toUInt32 &&& 0x7fffff))

def report (name : String) (xs : Array UInt32) : IO Unit := do
  let reps := 7
  let loop ← perOp (fun x _ => x) xs reps
  let host ← perOp (fun x y => Configured.NativeFPU.Unchecked.add32 x y) xs reps
  let word ← perOp (fun x y => Configured.Backend.wordAdd x y) xs reps
  let pub ← perOp (fun x y => ExecFloat.add x y) xs reps
  let fast ← perOp fastAdd xs reps
  let cPub ← Bench.sweep (fun x y => ExecFloat.add x y) xs 0
  let cFast ← Bench.sweep fastAdd xs 0
  IO.println s!"{name}: loop-only {loop} ns  host-fpu {host} ns  wordAdd {word} ns  ExecFloat.add {pub} ns  fastAdd {fast} ns  agree={cPub == cFast}"

def agree (name : String) (xs : Array UInt32) : IO Unit := do
  let a ← Bench.sweep (fun x y => ExecFloat.add x y) xs 0
  let b ← Bench.sweep fastAdd xs 0
  IO.println s!"agreement {name}: {a == b} ({xs.size - 1} pairs)"

def main : IO Unit := do
  report "random-bits" (Bench.lcg 500001 0x9E3779B9)
  report "near-one   " (gen Bench.nearOne 500001 7)
  agree "random seed 2" (Bench.lcg 2000001 0x12345678)
  agree "near-one 2M " (gen Bench.nearOne 2000001 99)
  agree "wide gaps   " (gen Bench.wideGap 2000001 11)
  agree "cancellation" (gen Bench.cancel 2000001 13)
