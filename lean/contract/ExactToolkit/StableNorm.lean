import ExactToolkit.Values

/-! Exact rational statistics and a once-rounded square root for normalization.
No host floating-point operations or native proof axioms are used. -/

namespace ExactRuntime

open FloatLib.Floats.Formats.BinaryInterchange FloatLib.Floats

def finiteWord (w : UInt32) : Bool := expOf w != 255

def rationalWord (w : UInt32) : ℚ := (decodeD w).toRat

/-- Round an exact rational once; mathematical zero has the canonical positive sign. -/
def roundRational (q : ℚ) : UInt32 :=
  toWord (Model.roundRat Fmt (decide (q < 0)) q.num.natAbs q.den)

/-- Exact power of two, including negative exponents. -/
def powTwo (e : Int) : ℚ := if e ≥ 0 then (2 ^ e.toNat : Nat) else 1 / (2 ^ (-e).toNat : Nat)

/-- Integer square root of a rational, before midpoint rounding. -/
def sqrtLower (num den : Nat) : Nat := Nat.sqrt (num / den)

/-- Exact enclosure used by the rounding algorithm, for every positive denominator. -/
theorem sqrtLower_bounds (num den : Nat) (hd : 0 < den) :
    sqrtLower num den ^ 2 * den ≤ num ∧ num < (sqrtLower num den + 1) ^ 2 * den := by
  constructor
  · exact (Nat.le_div_iff_mul_le hd).mp (Nat.sqrt_le' (num / den))
  · exact (Nat.div_lt_iff_lt_mul hd).mp (Nat.lt_succ_sqrt' (num / den))

/-- Enough exact binary scaling for a nonzero truncated root, below the binary32 minimum
target exponent. No leading-exponent guess or binary32 packing is implemented here. -/
def sqrtShift (q : ℚ) : Nat := q.den.log2 + 150

def sqrtNumerator (q : ℚ) : Nat := q.num.natAbs * 2 ^ (2 * sqrtShift q)

/-- Exact integer, below-midpoint, midpoint, or above-midpoint certificate. -/
def sqrtAccuracy (num den : Nat) : Float.Model.UnpackedFloat.Accuracy :=
  let root := sqrtLower num den
  if num = root ^ 2 * den then .exact
  else .inexact (if 4 * num < den * (2 * root + 1) ^ 2 then .lt
    else if 4 * num = den * (2 * root + 1) ^ 2 then .eq else .gt)

def sqrtModel (q : ℚ) : Model Fmt :=
  Model.ofModel Fmt (Float.Model.UnpackedFloat.roundWithAccuracy
    (FloatFormat.toModel Fmt) .positive (sqrtLower (sqrtNumerator q) q.den)
      (-(sqrtShift q : Int)) (sqrtAccuracy (sqrtNumerator q) q.den))

/-- Nonpositive inputs map to +0. Positive rationals use an exact accuracy certificate and
FloatLib's proved nearest-even rounder, including its subnormal and overflow behavior. -/
def sqrtRational (q : ℚ) : UInt32 :=
  if q ≤ 0 then 0 else toWord (sqrtModel q)

def rowMean (n : Nat) (x : Array UInt32) (row : Nat) : ℚ :=
  ((List.range n).foldl (fun total j => total + rationalWord x[row * n + j]!) 0) / n

def rowVariance (n : Nat) (x : Array UInt32) (row : Nat) (mean : ℚ) : ℚ :=
  ((List.range n).foldl (fun total j =>
    total + (rationalWord x[row * n + j]! - mean) ^ 2) 0) / n

/-- Conditions for the stable branch. Other inputs retain the legacy specification. -/
def stableNormDomain (rows n : Nat) (eps : UInt32) (x g : Array UInt32) : Bool :=
  rows > 0 && n > 0 && x.size == rows * n && g.size == n &&
    x.all finiteWord && g.all finiteWord && finiteWord eps && rationalWord eps > 0

def rmsnormStable (rows n : Nat) (eps : UInt32) (x g : Array UInt32) : Array UInt32 :=
  let den := Array.ofFn (n := rows) fun r =>
    sqrtRational (rowVariance n x r.val 0 + rationalWord eps)
  Array.ofFn (n := rows * n) fun q =>
    roundRational (rationalWord x[q.val]! * rationalWord g[q.val % n]! /
      rationalWord den[q.val / n]!)

def layernormStable (rows n : Nat) (eps : UInt32) (x g b : Array UInt32) : Array UInt32 :=
  let means := Array.ofFn (n := rows) fun r => rowMean n x r.val
  let den := Array.ofFn (n := rows) fun r =>
    sqrtRational (rowVariance n x r.val means[r.val]! + rationalWord eps)
  Array.ofFn (n := rows * n) fun q =>
    roundRational ((rationalWord x[q.val]! - means[q.val / n]!) *
      rationalWord g[q.val % n]! / rationalWord den[q.val / n]! + rationalWord b[q.val % n]!)

end ExactRuntime
