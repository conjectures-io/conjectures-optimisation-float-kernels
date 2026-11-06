import ExactToolkit.Decode
import FloatLib.Floats.Formats.BinaryInterchange.Transcendentals.CorrectlyRounded.Runtime

/-!
# Correctly rounded `sigmoid` and `tanh`

`sigmoidCR x` is the binary32 value nearest to `1 / (1 + e^(−x))` and `tanhCR x` the one nearest
to `tanh x`, ties to even, for every finite input. Both are decided by exact comparisons, the same
fallback FloatLib's correctly rounded `exp` uses: `AlgebraicRounding.round` asks whether the real
result lies below, at or above rational candidates, and each question becomes a comparison of
the input with the logarithm of a rational, which FloatLib decides exactly
(`ElementaryComparison.compareLog`):

```text
0 < b < 1:   tanh a < b  ⟺  2a < log ((1 + b) / (1 − b))      (a ≥ 0; artanh b)
0 < b < 1:   σ x < b    ⟺  x < log (b / (1 − b))              (logit b)
```

`tanh` is odd, so it rounds `tanh |x|` with the sign of `x`. Neither result can overflow:
`|tanh x| < 1` and `0 < σ x < 1` for finite `x`.

The comparisons are exact but slow, so each function first tries an enclosure search, as FloatLib's
correctly rounded `exp` does: FloatLib's certified enclosure of `e = exp (−2|x|)` (or `exp (−x)`)
is mapped through `(1 − e) / (1 + e)` (or `1 / (1 + e)`), both decreasing in `e`, and the result is
accepted only when both ends of the enclosure round to the same binary32 value, which is then the
rounding of the true value inside it. When the search does not settle, the comparisons decide.

Special values: a NaN is quieted (FloatLib's `quietNaN`, payload kept); `tanh (±∞) = ±1`,
`tanh (±0) = ±0`; `σ (+∞) = 1`, `σ (−∞) = +0`, `σ (±0) = 1/2`.

The correctness theorems are in `ExactToolkit.ActivationProof`.
-/

namespace ExactRuntime

open FloatLib.Floats.Formats.BinaryInterchange FloatLib.Floats FloatLib.Numerics

/-- Compare `tanh a` with a rational boundary `b`, for `a ≥ 0`. -/
def compareTanh (a b : ℚ) : Ordering :=
  if hb : b ≤ 0 then (if a = 0 then cmp 0 b else .gt)
  else if h1 : 1 ≤ b then .lt
  else
    (ElementaryComparison.compareLog ((1 + b) / (1 - b)) (2 * a)
      (div_pos (by linarith [not_le.mp hb]) (by linarith [not_le.mp h1]))).swap

/-- Compare `σ x = 1 / (1 + e^(−x))` with a rational boundary `b`. -/
def compareSigmoid (x b : ℚ) : Ordering :=
  if hb : b ≤ 0 then .gt
  else if h1 : 1 ≤ b then .lt
  else
    (ElementaryComparison.compareLog (b / (1 - b)) x
      (div_pos (not_le.mp hb) (by linarith [not_le.mp h1]))).swap

/-- An enclosure of `tanh a` from an enclosure of `e = exp (−2a)`, `e > 0`:
`(1 − e) / (1 + e)` is decreasing in `e ≥ 0`. Negative endpoints are raised to `0`. -/
def tanhOfExp (interval : RationalInterval) : RationalInterval :=
  let lo := max 0 interval.lo
  let hi := max 0 interval.hi
  ⟨(1 - hi) / (1 + hi), (1 - lo) / (1 + lo)⟩

/-- An enclosure of `σ x` from an enclosure of `e = exp (−x)`: `1 / (1 + e)` is decreasing. -/
def sigmoidOfExp (interval : RationalInterval) : RationalInterval :=
  ⟨1 / (1 + max 0 interval.hi), 1 / (1 + max 0 interval.lo)⟩

/-- An enclosure of `tanh x` at a Taylor degree and a grid precision. For `|x| > 16`,
`e^(−2|x|) < e^(−32) < 2^(−46)`, so `[0, 2^(−46)]` encloses it without evaluating an exponential
whose binary grid would need about `3|x|` bits; the result then rounds to `±1`. -/
def tanhEnclosure (x : ℚ) (degree precision : Nat) : RationalInterval :=
  let e : RationalInterval := if 16 < |x| then ⟨0, 1 / 2 ^ 46⟩
    else FloatLib.Numerics.Enclosure.BinaryGrid.exp (-2 * |x|) degree precision
  let positive := tanhOfExp e
  if x < 0 then positive.neg else positive

/-- An enclosure of `σ x` at a Taylor degree and a grid precision. Far from zero no exponential is
evaluated: for `x > 32`, `e^(−x) < 2^(−46)` and `σ x` rounds to `1`; for `x < −128`,
`σ x < e^x < 2^(−184)`, below half the least subnormal, and it rounds to `+0`. -/
def sigmoidEnclosure (x : ℚ) (degree precision : Nat) : RationalInterval :=
  if 32 < x then sigmoidOfExp ⟨0, 1 / 2 ^ 46⟩
  else if x < -128 then ⟨0, 1 / 2 ^ 184⟩
  else sigmoidOfExp (FloatLib.Numerics.Enclosure.BinaryGrid.exp (-x) degree precision)

/-- Search for the rounding of `tanh x`: FloatLib's refinement loop (degree 8, 16 attempts), from
a precision that allows for the cancellation in `1 − e` near `x = 0`. -/
def tanhSearch (fmt : FloatFormat) (x : ℚ) : Option (Model fmt) :=
  Model.Transcendentals.Certified.refine fmt (tanhEnclosure x) 8
    (FloatLib.Numerics.Enclosure.BinaryGrid.expPrecision (fmt.fracWidth + 1) (-2 * |x|) 8 true) 16

/-- Search for the rounding of `σ x`, as `tanhSearch`. -/
def sigmoidSearch (fmt : FloatFormat) (x : ℚ) : Option (Model fmt) :=
  Model.Transcendentals.Certified.refine fmt (sigmoidEnclosure x) 8
    (FloatLib.Numerics.Enclosure.BinaryGrid.expPrecision (fmt.fracWidth + 1) (-x) 8) 16

/-- `tanh`, correctly rounded for every finite input. -/
def tanhModel {fmt : FloatFormat} (input : Model fmt) : Model fmt :=
  if Model.isNaN input then Model.quietNaN input
  else if Model.isInf input then
    if Model.signBit input then Model.negOne fmt else Model.posOne fmt
  else if Model.isZero input then input
  else
    match Model.toRat? input with
    | some x =>
      match tanhSearch fmt x with
      | some result => result
      | none => Model.AlgebraicRounding.round fmt (decide (x < 0)) (compareTanh |x|)
    | none => Model.invalidResult fmt

/-- The logistic sigmoid `1 / (1 + e^(−x))`, correctly rounded for every finite input. -/
def sigmoidModel {fmt : FloatFormat} (input : Model fmt) : Model fmt :=
  if Model.isNaN input then Model.quietNaN input
  else if Model.isInf input then
    if Model.signBit input then Model.posZero fmt else Model.posOne fmt
  else
    match Model.toRat? input with
    | some x =>
      match sigmoidSearch fmt x with
      | some result => result
      | none => Model.AlgebraicRounding.round fmt false (compareSigmoid x)
    | none => Model.invalidResult fmt

/-- `tanh` on a binary32 word, correctly rounded. -/
def tanhCR (w : UInt32) : UInt32 :=
  UInt32.ofNat (tanhModel (Model.ofUInt32 w : Model Fmt)).toNatBits

/-- The logistic sigmoid on a binary32 word, correctly rounded. -/
def sigmoidCR (w : UInt32) : UInt32 :=
  UInt32.ofNat (sigmoidModel (Model.ofUInt32 w : Model Fmt)).toNatBits

end ExactRuntime
