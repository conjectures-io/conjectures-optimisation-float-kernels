import BenchBmm
import Lean.Data.Json

/-!
# The suite benchmark: the full entry timed on cases drawn from a suite

`benchmark_suite.py` draws every case (its kind of input, its parameters and its size) and the gate
hands the plan over in `BENCH_PLAN`; this module only turns a case into operands and times one
kernel over them. Operands are built before the clock starts. Every value comes from a SplitMix64
finaliser over a counter, so a case is the same on both sides of a round, and each property (a
value's magnitude, sign, fraction, whether it is zero, subnormal or special) has its own stream.

A case's operands are ordinary normal values unless a rule picks a position, in this order:

- special values (±∞, NaN, ±largest finite, subnormals, ±0) at a rate, on a stride, in bursts or
  at the ends of each row;
- zeros (+0, -0, either, or tiny "leaked" normals), alone or in runs;
- subnormals, in blocks.

Ordinary values have a uniform or geometric ("normal-like") exponent around a centre, an optional
per-block scale, a fraction rounded to fewer bits, and can repeat from a small codebook. For `add`
and `mul` the second operand can be tied to the first: equal, negated, nearly cancelling, constant,
an identity, or a set number of binades below it.

Each case prints `name timed ns checksum`, the line format of every other bench.
-/

namespace Bench.Suite
open FloatLib.Floats Competition
open FloatLib.Floats.Formats.BinaryInterchange
open Lean

structure Case where
  name : String
  kernel : String
  timed : Bool
  seed : Nat
  mag : Nat
  center : Nat
  spread : Nat
  positive : Nat
  mantBits : Nat
  codebook : Nat
  scaleBlock : Nat
  scaleShift : Nat
  zeroPct : Nat
  zeroCluster : Nat
  zeroKind : Nat
  subPct : Nat
  subBlock : Nat
  specRate : Nat
  specPlace : Nat
  specPeriod : Nat
  specBurst : Nat
  specMask : Nat
  relation : Nat
  relParam : Nat
  length : Nat
  m : Nat
  n : Nat
  p : Nat
  batch : Nat
  work : Nat
  deriving FromJson

structure Plan where
  version : String
  cases : Array Case
  deriving FromJson

/-- SplitMix64 finaliser over a counter: one independent word per (seed, index, stream). -/
@[inline] def word (seed index stream : Nat) : UInt32 :=
  let z := UInt64.ofNat seed + UInt64.ofNat index * 0x9e3779b97f4a7c15 +
    UInt64.ofNat stream * 0xd1b54a32d192ed03
  let z := (z ^^^ (z >>> 30)) * 0xbf58476d1ce4e5b9
  let z := (z ^^^ (z >>> 27)) * 0x94d049bb133111eb
  (z ^^^ (z >>> 31)).toUInt32

/-- Stream `k` of operand `role` (0 the first operand or matrix, 1 the second). -/
@[inline] def draw (c : Case) (i role k : Nat) : UInt32 := word c.seed i (role * 16 + k)

@[inline] def pct (w : UInt32) : Nat := w.toNat % 100

@[inline] def perLakh (w : UInt32) : Nat := w.toNat % 100000

/-- Trailing zero bits, 32 for zero: k with probability 2^-(k+1), a geometric magnitude. -/
def ctz (w : UInt32) : Nat := Id.run do
  for k in [0:32] do
    if (w >>> UInt32.ofNat k) &&& 1 == 1 then return k
  return 32

/-- A biased exponent clamped to the normal range. -/
@[inline] def clampExp (e : Int) : UInt32 := UInt32.ofNat (max 1 (min 254 e)).toNat

/-- An ordinary normal value: index `j` draws its magnitude, sign and fraction (a codebook reuses a
few `j`), position `i` its block's scale. -/
def ordinary (c : Case) (i j role : Nat) : UInt32 :=
  let w := draw c j role 1
  let centre : Int := Int.ofNat c.center
  let e : Int := if c.mag == 1 then centre - Int.ofNat (min (ctz w) c.spread)
    else centre - Int.ofNat c.spread + Int.ofNat (w.toNat % (2 * c.spread + 1))
  let shift : Int := if c.scaleBlock == 0 then 0
    else Int.ofNat ((draw c (i / c.scaleBlock) role 2).toNat % (2 * c.scaleShift + 1)) -
      Int.ofNat c.scaleShift
  let keep : UInt32 := if c.mantBits ≥ 23 then 0x7fffff
    else (0x7fffff : UInt32) ^^^ (((1 : UInt32) <<< UInt32.ofNat (23 - c.mantBits)) - 1)
  let frac := draw c j role 3 &&& keep
  let sign : UInt32 := if pct (draw c j role 4) < c.positive then 0 else 0x80000000
  sign ||| (clampExp (e + shift) <<< 23) ||| frac

/-- The `r`-th set bit of the five-bit special-kinds mask (NaN when the mask is empty). -/
def nthKind (mask r : Nat) : Nat := Id.run do
  let mut seen := 0
  for b in [0:5] do
    if (mask >>> b) % 2 == 1 then
      if seen == r then return b
      seen := seen + 1
  return 1

def special (c : Case) (i role : Nat) : UInt32 :=
  let count := (List.range 5).countP fun b => (c.specMask >>> b) % 2 == 1
  let kind := nthKind c.specMask ((draw c i role 5).toNat % max 1 count)
  let sign : UInt32 := draw c i role 6 &&& 0x80000000
  let low := draw c i role 7 &&& 0x7fffff
  match kind with
  | 0 => sign ||| 0x7f800000
  | 1 => sign ||| 0x7f800000 ||| (low ||| 1)
  | 2 => sign ||| 0x7f7fffff
  | 3 => sign ||| (low ||| 1)
  | _ => sign

/-- Whether position `i` (in rows of `row`) holds a special value. -/
def isSpecial (c : Case) (i role row : Nat) : Bool :=
  c.specRate > 0 && match c.specPlace with
  | 1 => (i + c.seed + 7 * role) % c.specPeriod == 0
  | 2 => perLakh (draw c (i / c.specBurst) role 8) < c.specRate
  | 3 => (i % row == 0 || i % row == row - 1) && perLakh (draw c i role 9) < c.specRate
  | _ => perLakh (draw c i role 9) < c.specRate

def zero (c : Case) (i role : Nat) : UInt32 :=
  let w := draw c i role 10
  match c.zeroKind with
  | 1 => 0x80000000
  | 2 => w &&& 0x80000000
  | 3 => (w &&& (0x807fffff : UInt32)) ||| (UInt32.ofNat (1 + w.toNat % 30) <<< 23)
  | _ => 0

/-- Operand `role`'s value at position `i`, in rows of `row` values. -/
def element (c : Case) (i role row : Nat) : UInt32 :=
  if c.mag == 2 then draw c i role 0
  else if isSpecial c i role row then special c i role
  else if c.zeroPct > 0 && pct (draw c (i / c.zeroCluster) role 11) < c.zeroPct then
    zero c i role
  else if c.subPct > 0 && pct (draw c (i / c.subBlock) role 12) < c.subPct then
    (draw c i role 13 &&& (0x807fffff : UInt32)) ||| 1
  else
    let j := if c.codebook > 0 then (draw c i role 14).toNat % c.codebook else i
    ordinary c i j role

/-- The second operand of a scalar step, tied to the first by the case's relation. -/
def partner (c : Case) (i : Nat) (a : UInt32) : UInt32 :=
  let w := draw c i 1 15
  match c.relation with
  | 1 => a
  | 2 => a ^^^ 0x80000000
  | 3 | 7 =>
    let mask : UInt32 := ((1 : UInt32) <<< UInt32.ofNat (min 23 c.relParam)) - 1
    let b := (a &&& ~~~mask) ||| (w &&& mask)
    if c.relation == 3 then b ^^^ 0x80000000 else b
  | 4 => element c 0 1 31
  | 5 =>
    if pct w < c.relParam then
      if c.kernel == "add" then w &&& 0x80000000 else (w &&& 0x80000000) ||| 0x3f800000
    else element c i 1 31
  | 6 =>
    let ea := (a >>> 23) &&& 0xff
    if ea == 0 || ea == 255 then element c i 1 31
    else (w &&& (0x807fffff : UInt32)) ||| (clampExp (Int.ofNat ea.toNat - Int.ofNat c.relParam) <<< 23)
  | _ => element c i 1 31

@[inline] def float (w : UInt32) : Binary32 := Configured.NativeFPU.ofBinary32Bits w

/-- A checksum sweep that doubles as warm-up, then the fastest of `inner` timed sweeps. -/
def measure (c : Case) (inner : Nat) (pass : UInt32 → IO UInt32) : IO Unit := do
  let checksum ← pass 0
  let mut ns := 0
  if c.timed then
    for r in [0:inner] do
      let t0 ← IO.monoNanosNow
      let sink ← pass (UInt32.ofNat (r + 1))
      let elapsed := (← IO.monoNanosNow) - t0
      if sink == 0xffffffff then IO.println "unlikely"   -- keeps `sink` observed
      ns := if ns == 0 || elapsed < ns then elapsed else ns
  IO.println s!"{c.name} {c.timed} {ns} {checksum}"

def runScalar (c : Case) (inner : Nat) (kernel : Binary32 → Binary32 → Binary32) : IO Unit := do
  let xs := Array.ofFn (n := c.work) fun i =>
    let a := element c i.val 0 31
    (float a, float (partner c i.val a))
  measure c inner fun seed => do
    let mut sink := seed
    for (a, b) in xs do
      sink := (sink ^^^ (kernel a b).raw.1) * 16777619
    return sink

def runSum (c : Case) (inner : Nat)
    (kernel : {n : Nat} → Vector Binary32 n → Binary32) : IO Unit := do
  let xs := Array.ofFn (n := c.batch) fun b =>
    Vector.ofFn (n := c.length) fun i => float (element c (b.val * c.length + i.val) 0 c.length)
  measure c inner fun seed => do
    let mut sink := seed
    for x in xs do
      sink := (sink ^^^ (kernel x).raw.1) * 16777619
    return sink

def runBmm (c : Case) (inner : Nat)
    (kernel : (batch m n p : Nat) → Vector Binary32 (batch * m * n) →
      Vector Binary32 (batch * n * p) → Vector Binary32 (batch * m * p)) : IO Unit := do
  let a := Vector.ofFn (n := c.batch * c.m * c.n) fun i => float (element c i.val 0 c.n)
  let b := Vector.ofFn (n := c.batch * c.n * c.p) fun i => float (element c i.val 1 c.p)
  measure c inner fun seed => do
    let mut sink := seed
    for x in (kernel c.batch c.m c.n c.p a b).toArray do
      sink := (sink ^^^ x.raw.1) * 16777619
    return sink

def run (add mul : Binary32 → Binary32 → Binary32)
    (sum : {n : Nat} → Vector Binary32 n → Binary32)
    (bmm : (batch m n p : Nat) → Vector Binary32 (batch * m * n) →
      Vector Binary32 (batch * n * p) → Vector Binary32 (batch * m * p)) : IO UInt32 := do
  let some text ← IO.getEnv "BENCH_PLAN"
    | throw (IO.userError "BENCH_PLAN is required for the suite benchmark")
  let plan ← match Json.parse text >>= fromJson? (α := Plan) with
    | .ok p => pure p
    | .error e => throw (IO.userError s!"invalid BENCH_PLAN: {e}")
  if plan.version != "suite-v2" then throw (IO.userError "unsupported benchmark plan")
  let inner ← envNat "BENCH_INNER" 3
  if inner == 0 then throw (IO.userError "BENCH_INNER must be positive")
  for c in plan.cases do
    if c.work == 0 || c.length == 0 || c.batch == 0 || c.specPeriod == 0 || c.specBurst == 0
        || c.zeroCluster == 0 || c.subBlock == 0 then
      throw (IO.userError s!"empty benchmark case {c.name}")
    match c.kernel with
    | "add" => runScalar c inner add
    | "mul" => runScalar c inner mul
    | "sum" => runSum c inner sum
    | "bmm" => runBmm c inner bmm
    | _ => throw (IO.userError "unknown benchmark kernel")
  return 0

end Bench.Suite
