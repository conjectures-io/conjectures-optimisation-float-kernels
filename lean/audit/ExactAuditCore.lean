import ExactSuite

/-!
# Audit of an exact-runtime entry (not used by the gate)

Calls each of the 12 kernels of the entry in `work/Submission.lean` and of the specs on the same
inputs and compares every output word. The inputs cover the suite's data types and the cases the
suite never times: subnormals, huge values, huge exponent gaps, NaNs and infinities, zeros, ties,
exact cancellation, and shapes at the edges (empty, mismatched and short arrays). A proved entry
can only fail here through a gap in what the gate checks.

    lake exe exactAudit [--malformed] [cases per kernel and kind] [seed]
    lake exe exactAudit --selftest       the spec is clean against itself and every mutant is caught
    lake exe exactAudit --mutants [n]    the mutation score: plausible wrong kernels caught
    lake exe exactAudit --coverage [n]   output classes the inputs reach, per kernel

`--malformed` adds short arrays (inputs shorter than their shape says); Lean then prints a panic
message for each read past the end, which is expected.

Exit code 0: every output equal; 1: a mismatch, listed with how to reproduce it.
-/

namespace ExactAudit

open ExactRuntime ExactSuite

/-! ## Inputs -/

/-- The input kinds: the suite's three, the edge cases, and all of them mixed. `tiny` values
(about 2^-63 to 2^-75) have products in the subnormal range, so dot products round to
subnormal results. `long` uses long reductions (sums of up to about 1,100 terms) and `many` many outputs (channels,
batches and rows up to 17), both mixing values near 1 with values near 2^-30, so blocked and tiled
code rounds differently; `normtie` makes the norms' outputs exact ties (normal and subnormal),
`overtie` makes `sum` and `dot` exact ties at the overflow boundary; `sqrttie` builds
normalisation rows whose square root is an exact midpoint, `offset` rows with a mean much larger
than their spread, and `subtie` dot products that tie on the subnormal grid. -/
def kinds : List String :=
  ["normal", "wide", "sparse", "subnormal", "tiny", "huge", "gap", "special", "zeros", "negzero",
    "zerosign", "ties", "cancel", "mixed", "long", "many", "sqrttie", "offset", "subtie", "normtie",
    "overtie"]

/-- A sign and a fraction from `w`, with exponent field `e`. -/
@[inline] def withExp (w : UInt32) (e : UInt32) : UInt32 :=
  (w &&& (0x807FFFFF : UInt32)) ||| (e <<< (23 : UInt32))

/-- One word of an edge-case kind; `w` is random, `c` an independent random choice. -/
def edgeWord (kind : String) (w c : UInt32) : UInt32 :=
  let near := withExp w ((124 : UInt32) + (w >>> 23) % 7)
  match kind with
  | "subnormal" => w &&& 0x807FFFFF
  | "tiny" => withExp w (52 + c % 13)
  | "offset" => withExp (w &&& 3) 157
  | "long" | "many" => if c % 2 == 0 then near else withExp w (97 + c % 8)
  | "huge" => withExp w (250 + c % 5)
  | "gap" => if c % 2 == 0 then withExp w (1 + c % 4) else withExp w (250 + c % 5)
  | "special" =>
    match c % 8 with
    | 0 | 1 => (w &&& 0x807FFFFF) ||| 0x7F800001
    | 2 => (w &&& 0x80000000) ||| 0x7F800000
    | _ => near
  | "zeros" => if c % 4 != 0 then w &&& 0x80000000 else near
  | "negzero" => 0x80000000
  | "ties" => (w &&& 0x80000000) ||| #[0x3F800000, 0x33800000, 0x33000000, 0x33C00000][(c % 4).toNat]!
  | _ => near

/-- `n` words of `kind` from `seed`. For `cancel`, the second half repeats the first, negated
when `seed` is odd: one such array sums to exactly zero, and an odd-seed array dotted with an
even-seed one cancels term by term. For `zerosign`, all −0 for an odd seed and all +0 for an even
one: every product of the two is −0, so dot products round to exactly −0. -/
def input (kind : String) (n : Nat) (seed : UInt32) : Array UInt32 :=
  if kind == "normal" || kind == "wide" || kind == "sparse" then gen kind n seed else
  if kind == "zerosign" then Array.replicate n (if seed % 2 == 1 then 0x80000000 else 0) else
  if kind == "subtie" then
    -- odd seeds: ±k·2^-75 with k odd (1, 3, 5, 7); even seeds: ±2^-75; products are odd multiples
    -- of 2^-150, halfway between subnormal words
    (lcg n seed).map fun w => (w &&& (0x80000000 : UInt32)) |||
      (if seed % 2 == 1 then (#[0x1A000000, 0x1AC00000, 0x1B200000, 0x1B600000] : Array UInt32)[(w % 4).toNat]!
       else (0x1A000000 : UInt32))
  else
  let ws := lcg n seed
  let cs := lcg n (seed ^^^ 0x9E3779B9)
  let pick (i : Nat) : UInt32 :=
    if kind == "mixed" then
      let k := #["subnormal", "huge", "gap", "special", "zeros", "ties", "normal"][(cs[i]! % 7).toNat]!
      edgeWord k ws[i]! (cs[i]! >>> 3)
    else edgeWord kind ws[i]! cs[i]!
  if kind != "cancel" then Array.ofFn (n := n) fun i => pick i.val else
  let h := n / 2
  Array.ofFn (n := n) fun i =>
    if i.val < h || i.val ≥ 2 * h then pick i.val
    else if seed % 2 == 1 then pick (i.val - h) ^^^ 0x80000000 else pick (i.val - h)

/-! ## Cases -/

/-- The word for `t · 2^e`, exact for small `t`. -/
def dyW (t : Nat) (e : Int) : UInt32 :=
  toWord (FloatLib.Floats.Formats.BinaryInterchange.Model.roundDyadic Fmt ⟨false, t, e⟩)

/-- One kernel call on both records: the shape it was called with, and the two outputs. -/
structure Outcome where
  shape : String
  entry : Array UInt32
  spec : Array UInt32

/-- With `malformed`, drop the last word in about one case in eight: the kernels then read past
the end, which Lean answers with 0 (and a panic message on stderr). -/
def maybeShort (malformed : Bool) (r : UInt32) (a : Array UInt32) : Array UInt32 :=
  if malformed && r % 8 == 0 then a.pop else a

/-- Every kernel on one case: inputs of `kind` and small random shapes from `seed`. -/
def runCase (E S : Kernels) (malformed : Bool) (kernel kind : String) (seed : UInt32) : Outcome :=
  let r := lcg 16 (seed * 2654435761 + 1)
  let d (i lo span : Nat) : Nat := lo + (r[i]! % span.toUInt32).toNat
  -- `long` cases use large shapes: blocked or tiled accumulation shows only past a block
  let dl (i lo span bigLo bigSpan : Nat) : Nat := if kind == "long" then d i bigLo bigSpan else d i lo span
  -- `many` cases use many outputs (channels, batches, rows): tiled code shows only past a tile
  let dm (i lo span manyLo manySpan : Nat) : Nat := if kind == "many" then d i manyLo manySpan else d i lo span
  let sgn : UInt32 := if r[11]! % 2 == 0 then 0 else 0x80000000
  let relu := r[14]! % 2 == 0
  let arr (n : Nat) (j : UInt32) := input kind n (seed * 16 + j)
  match kernel with
  | "sum" =>
    -- `overtie`: the largest finite value plus 2^103 is 2^128 - 2^103, the overflow tie
    let x := if kind == "overtie" then #[0x7F7FFFFF ||| sgn, 0x73000000 ||| sgn] else arr (dl 0 0 64 64 1037) 1
    ⟨s!"n={x.size}", #[E.core.sum x], #[S.core.sum x]⟩
  | "dot" =>
    -- `subtie`: an odd number of odd multiples of 2^-150 sums to a tie on the subnormal grid
    let n := if kind == "subtie" then 1 + 2 * d 0 0 3 else dl 0 0 64 64 1037
    -- `overtie`: 31 · 1082401·2^103 is 2^128 - 2^103, the overflow tie
    let a := if kind == "overtie" then #[0x41F80000 ||| sgn] else arr n 1
    let b := if kind == "overtie" then #[0x7D042108]
      else if r[15]! % 8 == 0 then arr (n + 1) 2 else arr n 2
    ⟨s!"n={a.size},{b.size}", #[E.core.dot a b], #[S.core.dot a b]⟩
  | "linear" =>
    let (rows, i, o) := if kind == "many" then (d 0 1 9, d 1 1 8, d 2 1 17)
      else (dl 0 1 3 1 2, dl 1 0 24 24 1077, dl 2 1 6 1 3)
    let X := maybeShort malformed r[15]! (arr (rows * i) 1)
    let W := arr (o * i) 2
    let b := arr o 3
    ⟨s!"{rows}x{i}->{o} relu={relu}", E.dense.linear rows i o relu X W b,
      S.dense.linear rows i o relu X W b⟩
  | "gemm" =>
    let (bt, m, k, n) := if kind == "many" then (d 0 1 9, d 1 1 9, d 2 1 6, d 3 1 17)
      else (d 0 1 2, dl 1 1 4 1 3, dl 2 0 12 12 289, dl 3 1 4 1 3)
    let A := maybeShort malformed r[15]! (arr (bt * m * k) 1)
    let B := arr (bt * k * n) 2
    ⟨s!"{bt}x{m}x{k}x{n}", E.dense.gemm bt m k n A B, S.dense.gemm bt m k n A B⟩
  | "conv2d" | "depthwise" =>
    -- `long`: conv2d windows of 270–405 terms (30–45 input channels, 3×3, on a small image),
    -- depthwise windows up to 9×9; `many`: up to 17 output channels (depthwise: 17 channels)
    let s : ConvShape :=
      if kind == "long" && kernel == "conv2d" then ⟨d 0 30 16, d 1 2 3, d 2 2 3, d 3 1 3, 3, 3, d 6 1 2, d 7 0 3⟩
      else if kind == "long" then ⟨d 0 1 3, d 1 3 8, d 2 3 8, d 3 1 3, d 4 1 9, d 5 1 9, d 6 1 3, d 7 0 4⟩
      else if kind == "many" then ⟨if kernel == "conv2d" then d 0 1 2 else d 0 1 17, d 1 1 5, d 2 1 5, d 3 1 17,
        d 4 1 3, d 5 1 3, d 6 1 2, d 7 0 2⟩
      else ⟨d 0 1 3, d 1 1 6, d 2 1 6, d 3 1 3, d 4 1 3, d 5 1 3, d 6 1 2, d 7 0 2⟩
    let inp := maybeShort malformed r[15]! (arr (s.cin * s.h * s.w) 1)
    let shape := s!"cin={s.cin} {s.h}x{s.w} cout={s.cout} k={s.kh}x{s.kw} stride={s.stride} pad={s.pad}"
    if kernel == "conv2d" then
      let wts := arr (s.cout * s.cin * s.kh * s.kw) 2
      let b := arr s.cout 3
      ⟨shape, E.conv.conv2d s relu inp wts b, S.conv.conv2d s relu inp wts b⟩
    else
      let wts := arr (s.cin * s.kh * s.kw) 2
      let b := arr s.cin 3
      ⟨shape, E.conv.depthwise s relu inp wts b, S.conv.depthwise s relu inp wts b⟩
  | "avgpool" | "maxpool" =>
    let (c, h, w, k) := (dm 0 1 3 1 17, dl 1 1 7 1 16, dl 2 1 7 1 16, dl 3 1 3 1 5)
    let inp := maybeShort malformed r[15]! (arr (c * h * w) 1)
    let shape := s!"c={c} {h}x{w} k={k}"
    if kernel == "avgpool" then ⟨shape, E.elem.avgpool c h w k inp, S.elem.avgpool c h w k inp⟩
    else ⟨shape, E.elem.maxpool c h w k inp, S.elem.maxpool c h w k inp⟩
  | "addRelu" =>
    let n := dl 0 0 32 32 1069
    let a := arr n 1
    let b := maybeShort malformed r[15]! (arr n 2)
    ⟨s!"n={n} relu={relu}", E.elem.addRelu relu a b, S.elem.addRelu relu a b⟩
  | "scaleShift" =>
    let (c, hw) := (dm 0 1 4 1 17, dl 1 1 8 8 289)
    let x := maybeShort malformed r[15]! (arr (c * hw) 1)
    let s := arr c 2
    let t := arr c 3
    ⟨s!"c={c} hw={hw} relu={relu}", E.elem.scaleShift relu c hw x s t,
      S.elem.scaleShift relu c hw x s t⟩
  | "rmsnorm" | "layernorm" =>
    let (rows, n) := if kind == "sqrttie" || kind == "normtie" then (d 0 1 3, 2 * d 1 1 8)
      else if kind == "many" then (d 0 1 17, d 1 1 8) else (dl 0 1 3 1 2, dl 1 0 24 24 1077)
    -- `sqrttie`: rows of ±t·2^-24 (t odd) and eps = 1 + t·2^-23, so mean square + eps (and the
    -- variance + eps, the mean being 0) is exactly (1 + t·2^-24)², a midpoint of the binary32 grid
    let t := 2 * (r[12]! % 32).toNat + 1
    -- `normtie`: rows of ±a (mean 0, mean square a²) with mean square + eps exactly 1, so the
    -- denominator is 1 and the output a·g is an exact tie: a = 0.75, eps = 0.4375, g = 1 + t·2^-23
    -- (t odd); or, on the subnormal grid, a = 2^-75, eps = 1, g = t·2^-75
    let sub := r[10]! % 2 == 0
    let a : UInt32 := if kind == "normtie" then (if sub then 0x1A000000 else 0x3F400000) else dyW t (-24)
    let e := if kind == "sqrttie" then dyW (2 ^ 23 + t) (-23)
      else if kind == "normtie" then (if sub then 0x3F800000 else 0x3EE00000)
      else if r[13]! % 4 == 0 then (arr 1 4)[0]! else eps
    let x := if kind == "sqrttie" || kind == "normtie" then
        Array.ofFn (n := rows * n) fun q => a ||| (if q.val % 2 == 0 then 0x80000000 else 0)
      else maybeShort malformed r[15]! (arr (rows * n) 1)
    let g := if kind == "normtie" then
        Array.ofFn (n := n) fun j => if sub then dyW (2 * ((r[9]! + j.val.toUInt32) % 4).toNat + 1) (-75)
          else (0x3F800000 : UInt32) + (2 : UInt32) * ((r[9]! + j.val.toUInt32) % 8) + 1
      else arr n 2
    let shape := s!"{rows}x{n}"
    if kernel == "rmsnorm" then ⟨shape, E.norm.rmsnorm rows n e x g, S.norm.rmsnorm rows n e x g⟩
    else
      let b := if kind == "normtie" then Array.replicate n 0 else arr n 3
      ⟨shape, E.norm.layernorm rows n e x g b, S.norm.layernorm rows n e x g b⟩
  | _ => ⟨"unknown kernel", #[], #[0]⟩

/-- The 12 kernels, as `runCase` names them. -/
def kernelNames : List String :=
  ["sum", "dot", "linear", "gemm", "conv2d", "depthwise", "avgpool", "maxpool", "addRelu",
    "scaleShift", "rmsnorm", "layernorm"]

/-! ## The audit -/

/-- One mismatch, with what is needed to reproduce it. -/
structure Mismatch where
  kernel : String
  kind : String
  seed : UInt32
  shape : String
  detail : String

/-- Where two outputs first differ. -/
def firstDiff (a b : Array UInt32) : String :=
  if a.size != b.size then s!"entry has {a.size} words, spec {b.size}" else
  match (List.range a.size).find? (fun i => a[i]! != b[i]!) with
  | some i => s!"word {i}: entry {a[i]!.toNat}, spec {b[i]!.toNat}"
  | none => "equal"

/-- Every kernel × kind × case: the case count and the mismatches. -/
def audit (E S : Kernels) (malformed : Bool) (cases : Nat) (seed : UInt32) :
    Nat × Array Mismatch := Id.run do
  let mut found := #[]
  let mut total := 0
  for (kernel, ki) in kernelNames.zipIdx do
    for (kind, ti) in kinds.zipIdx do
      for c in [0:cases] do
        let s := seed + (ki * 1000003 + ti * 10007 + c).toUInt32
        let o := runCase E S malformed kernel kind s
        total := total + 1
        if o.entry != o.spec then
          found := found.push ⟨kernel, kind, s, o.shape, firstDiff o.entry o.spec⟩
  return (total, found)

/-- Mismatch counts per kernel, one line each. -/
def summary (found : Array Mismatch) : List String :=
  kernelNames.filterMap fun k =>
    let mine := found.filter (·.kernel == k)
    if mine.isEmpty then none else
      some s!"  {k}: {mine.size} mismatches, kinds {(mine.map (·.kind)).toList.eraseDups}"

/-! ## Planted bugs (also mutants below) -/

section
open FloatLib.Floats.Formats.BinaryInterchange

/-- `sum` that flushes subnormal inputs to zero. -/
def flushSum : Kernels :=
  { specKernels with core := { specCore with
      sum := fun x => sumSpec (x.map fun w => if expOf w == 0 then w &&& 0x80000000 else w) } }

/-- `maxpool` that loses the sign of a −0 result. -/
def zeroSignMaxpool : Kernels :=
  { specKernels with elem := { specElem with
      maxpool := fun c h w k inp => (maxpoolSpec c h w k inp).map fun v =>
        if v == 0x80000000 then 0 else v } }

/-- `dot` that rounds after every product and every sum, left to right. -/
def foldDot : Kernels :=
  { specKernels with core := { specCore with
      dot := fun a b => toWord <| (List.range (min a.size b.size)).foldl (fun acc i =>
        Model.add acc (Model.mul (ofW a[i]!) (ofW b[i]!))) (ofW 0) } }

end

/-! ## Mutants: plausible wrong kernels (loop L1, mutation testing)

Each mutant changes one kernel the way a real bug or a careless implementation would. The audit
should catch every one; a mutant it misses shows a gap in the inputs. -/

section
open FloatLib.Floats.Formats.BinaryInterchange

/-- A word is a NaN. -/
@[inline] def isNaNW (w : UInt32) : Bool := expOf w == 255 && fracOf w != 0

/-- FloatLib's dot product in another rounding mode. -/
def dotMode (mode : Model.IEEERoundingMode) (a b : Array UInt32) : UInt32 :=
  match Model.dot Fmt (a.map (Model.ofUInt32 (fmt := Fmt))) (b.map (Model.ofUInt32 (fmt := Fmt)))
      mode with
  | .ok z => toWord z
  | .error _ => 0

/-- The dot product with ties rounded away from zero instead of to even. -/
def dotTiesAway (a b : Array UInt32) : UInt32 :=
  let r := dotSpec a b
  if !(a.all finiteWord && b.all finiteWord) then r else
  let exact := (List.range (min a.size b.size)).foldl
    (fun acc i => acc + rationalWord a[i]! * rationalWord b[i]!) (0 : ℚ)
  let lo := dotMode .towardNegativeInfinity a b
  let hi := dotMode .towardPositiveInfinity a b
  if lo != hi && finiteWord lo && finiteWord hi &&
      2 * exact == rationalWord lo + rationalWord hi then (if exact > 0 then hi else lo) else r

/-- A dot product rounded after every product and every sum, left to right. -/
def foldDotFn : DotFn := fun n fa fb => toWord <| (List.range n).foldl (fun acc i =>
  Model.add acc (Model.mul (ofW (fa i)) (ofW (fb i)))) (ofW 0)

/-- ReLU that leaves −0 as −0. -/
@[inline] def actKeepsNegZero (relu : Bool) (w : UInt32) : UInt32 :=
  if relu && w != 0x80000000 then reluW w else w

/-- Element `t` of a window with out-of-image reads clamped to the edge instead of zero. -/
def windowClamp (s : ConvShape) (inp : Array UInt32) (y x t : Nat) : UInt32 :=
  let c := t / (s.kh * s.kw)
  let yy := min (s.h - 1) ((y * s.stride + t / s.kw % s.kh) - s.pad)
  let xx := min (s.w - 1) ((x * s.stride + t % s.kw) - s.pad)
  inp[(c * s.h + yy) * s.w + xx]!

/-- One mutant: a name, the kernel it changes, and the kernels with the change. -/
structure Mutant where
  name : String
  kernel : String
  kernels : Kernels

/-- Each block of `k` words summed exactly and rounded, then the block results summed and rounded. -/
def blockedSum (k : Nat) (x : Array UInt32) : UInt32 :=
  if x.size ≤ k then sumSpec x else
  sumSpec ((List.range ((x.size + k - 1) / k)).toArray.map fun i => sumSpec (x.extract (k * i) (k * i + k)))

/-- A dot product over blocks of `k` terms: each block exact, the partial results added with
rounding. -/
def blockedDot (k : Nat) : DotFn := fun n fa fb =>
  toWord <| (List.range ((n + k - 1) / k)).foldl (fun acc i =>
    let p := dotOf (min k (n - i * k)) (fun t => fa (i * k + t)) (fun t => fb (i * k + t))
    if i == 0 then ofW p else Model.add acc (ofW p)) (ofW 0)

/-- The dot product, with an exact tie rounding to a nonzero subnormal going away from zero. -/
def subnormalTiesAway (a b : Array UInt32) : UInt32 :=
  let r := dotSpec a b
  if expOf r == 0 && fracOf r != 0 then dotTiesAway a b else r

/-- `sqrtRational`, rounding an exact midpoint up instead of to even. -/
def sqrtTiesUp (q : ℚ) : UInt32 :=
  let r := sqrtRational q
  if q ≤ 0 || r &&& 0x7F800000 == 0x7F800000 then r else
  let mid := (rationalWord r + rationalWord (r + 1)) / 2
  if q == mid * mid then r + 1 else r

/-- `sqrtRational`, rounding an exact midpoint down instead of to even. -/
def sqrtTiesDown (q : ℚ) : UInt32 :=
  let r := sqrtRational q
  if q ≤ 0 || r == 0 || r &&& 0x7F800000 == 0x7F800000 then r else
  let mid := (rationalWord (r - 1) + rationalWord r) / 2
  if q == mid * mid then r - 1 else r

/-- The stable `rmsnorm` with another square root for its denominator. -/
def rmsnormSqrt (sqrt : ℚ → UInt32) (rows n : Nat) (eps : UInt32) (x g : Array UInt32) :
    Array UInt32 :=
  if !stableNormDomain rows n eps x g then rmsnormSpec rows n eps x g else
  let den := Array.ofFn (n := rows) fun r => sqrt (rowVariance n x r.val 0 + rationalWord eps)
  Array.ofFn (n := rows * n) fun q =>
    roundRational (rationalWord x[q.val]! * rationalWord g[q.val % n]! / rationalWord den[q.val / n]!)

/-- The stable `layernorm`, except that the variance is centred on the rounded mean. -/
def layernormRoundedMean (rows n : Nat) (eps : UInt32) (x g b : Array UInt32) : Array UInt32 :=
  if stableNormDomain rows n eps x g && b.size == n && b.all finiteWord then
    let means := Array.ofFn (n := rows) fun r => rowMean n x r.val
    let den := Array.ofFn (n := rows) fun r =>
      sqrtRational (rowVariance n x r.val (rationalWord (roundRational means[r.val]!)) +
        rationalWord eps)
    Array.ofFn (n := rows * n) fun q =>
      roundRational ((rationalWord x[q.val]! - means[q.val / n]!) * rationalWord g[q.val % n]! /
        rationalWord den[q.val / n]! + rationalWord b[q.val % n]!)
  else layernormSpec rows n eps x g b

/-- `roundRational` with exact ties sent away from zero instead of to even, at any scale. -/
def roundTiesAway (q : ℚ) : UInt32 :=
  let r := roundRational q
  let mag := roundRational |q|
  if q == 0 || mag &&& 0x7F800000 == 0x7F800000 then r else
  let below := if rationalWord mag > |q| then mag - 1 else mag
  if below + 1 &&& 0x7F800000 == 0x7F800000 then r else
  if 2 * |q| == rationalWord below + rationalWord (below + 1) then
    (if q < 0 then 0x80000000 else 0) ||| (below + 1)
  else r

/-- The stable `rmsnorm` with another final rounding. -/
def rmsnormRound (round : ℚ → UInt32) (rows n : Nat) (eps : UInt32) (x g : Array UInt32) :
    Array UInt32 :=
  if !stableNormDomain rows n eps x g then rmsnormSpec rows n eps x g else
  let den := Array.ofFn (n := rows) fun r => sqrtRational (rowVariance n x r.val 0 + rationalWord eps)
  Array.ofFn (n := rows * n) fun q =>
    round (rationalWord x[q.val]! * rationalWord g[q.val % n]! / rationalWord den[q.val / n]!)

/-- The stable `layernorm` with another final rounding. -/
def layernormRound (round : ℚ → UInt32) (rows n : Nat) (eps : UInt32) (x g b : Array UInt32) :
    Array UInt32 :=
  if !(stableNormDomain rows n eps x g && b.size == n && b.all finiteWord) then
    layernormSpec rows n eps x g b else
  let means := Array.ofFn (n := rows) fun r => rowMean n x r.val
  let den := Array.ofFn (n := rows) fun r =>
    sqrtRational (rowVariance n x r.val means[r.val]! + rationalWord eps)
  Array.ofFn (n := rows * n) fun q =>
    round ((rationalWord x[q.val]! - means[q.val / n]!) * rationalWord g[q.val % n]! /
      rationalWord den[q.val / n]! + rationalWord b[q.val % n]!)

/-- Outputs processed in tiles of 4: in a full tile, every output reads the first one's bias. -/
def tileBias (count : Nat) (b : Array UInt32) : Array UInt32 :=
  Array.ofFn (n := count) fun o => if (o.val / 4 + 1) * 4 ≤ count then b[o.val / 4 * 4]! else b[o.val]!

/-- An exact sum of `±(2^128 - 2^103)`, the overflow tie, rounded down to the largest finite value. -/
def overflowTieDown (exact : ℚ) (r : UInt32) : UInt32 :=
  let mid : ℚ := (2 : ℚ) ^ 128 - (2 : ℚ) ^ 103
  if r == 0x7F800000 && exact == mid then 0x7F7FFFFF
  else if r == 0xFF800000 && exact == -mid then 0xFF7FFFFF else r

/-- Replace the core family's `dot`. -/
def withDot (d : Array UInt32 → Array UInt32 → UInt32) : Kernels :=
  { specKernels with core := { specCore with dot := d } }

/-- Replace the core family's `sum`. -/
def withSum (f : Array UInt32 → UInt32) : Kernels :=
  { specKernels with core := { specCore with sum := f } }

/-- The mutants, one plausible bug each. -/
def mutants : List Mutant := [
  ⟨"sum flushes subnormal inputs", "sum", flushSum⟩,
  ⟨"sum flushes a subnormal result", "sum", withSum fun x =>
    let r := sumSpec x; if expOf r == 0 && fracOf r != 0 then r &&& 0x80000000 else r⟩,
  ⟨"dot rounds every step", "dot", foldDot⟩,
  ⟨"dot flushes a subnormal result", "dot", withDot fun a b =>
    let r := dotSpec a b; if expOf r == 0 && fracOf r != 0 then r &&& 0x80000000 else r⟩,
  ⟨"dot rounds toward zero", "dot", withDot (dotMode .towardZero)⟩,
  ⟨"dot rounds ties away from zero", "dot", withDot dotTiesAway⟩,
  ⟨"dot returns the canonical NaN", "dot", withDot fun a b =>
    let r := dotSpec a b; if isNaNW r then 0x7FC00000 else r⟩,
  ⟨"dot returns +0 for every zero", "dot", withDot fun a b =>
    let r := dotSpec a b; if r == 0x80000000 then 0 else r⟩,
  ⟨"dot saturates overflow", "dot", withDot fun a b =>
    let r := dotSpec a b; if r &&& 0x7FFFFFFF == 0x7F800000 then (r &&& 0x80000000) ||| 0x7F7FFFFF else r⟩,
  ⟨"dot drops the last term", "dot", withDot fun a b =>
    if a.size == b.size && a.size > 0 then dotSpec a.pop b.pop else dotSpec a b⟩,
  ⟨"linear rounds before adding the bias", "linear", { specKernels with dense := { specDense with
    linear := fun rows inW outW relu X W b => Array.ofFn (n := rows * outW) fun q =>
      let i := q.val / outW
      let j := q.val % outW
      act relu <| toWord (Model.add (ofW (dotOf inW (fun t => X[i * inW + t]!)
        (fun t => W[j * inW + t]!))) (ofW b[j]!)) } }⟩,
  ⟨"linear's ReLU keeps −0", "linear", { specKernels with dense := { specDense with
    linear := fun rows inW outW relu X W b =>
      (linearSpec rows inW outW false X W b).map (actKeepsNegZero relu) } }⟩,
  ⟨"gemm reads B transposed", "gemm", { specKernels with dense := { specDense with
    gemm := fun batch m k n A B => Array.ofFn (n := batch * (m * n)) fun q =>
      let β := q.val / (m * n)
      let i := q.val / n % m
      let j := q.val % n
      dotOf k (fun t => A[(β * m + i) * k + t]!) (fun t => B[(β * n + j) * k + t]!) } }⟩,
  ⟨"conv2d clamps padding to the edge", "conv2d", { specKernels with conv := { specConv with
    conv2d := fun s relu inp wts b =>
      let k := s.cin * s.kh * s.kw
      Array.ofFn (n := s.cout * (s.ho * s.wo)) fun q =>
        let o := q.val / (s.ho * s.wo)
        let y := q.val / s.wo % s.ho
        let x := q.val % s.wo
        act relu <| dotOf (k + 1) (fun t => if t < k then windowClamp s inp y x t else oneW)
          (fun t => if t < k then wts[o * k + t]! else b[o]!) } }⟩,
  ⟨"depthwise uses the first channel's filter", "depthwise", { specKernels with conv := { specConv with
    depthwise := fun s relu inp wts b =>
      depthwiseSpec s relu inp (Array.ofFn (n := s.cin * (s.kh * s.kw)) fun i =>
        wts[i.val % (s.kh * s.kw)]!) b } }⟩,
  ⟨"avgpool divides by k, not k²", "avgpool", { specKernels with elem := { specElem with
    avgpool := fun c h w k inp => Array.ofFn (n := c * (h / k * (w / k))) fun q =>
      let ch := q.val / (h / k * (w / k))
      let y := q.val / (w / k) % (h / k)
      let x := q.val % (w / k)
      toWord (Model.div (ofW (dotOf (k * k) (poolAt h w k inp ch y x) (fun _ => oneW)))
        (ofW (natW k))) } }⟩,
  ⟨"avgpool rounds every step of its sum", "avgpool", { specKernels with elem := { specElem with
    avgpool := avgpoolWith foldDotFn } }⟩,
  ⟨"maxpool loses the sign of −0", "maxpool", zeroSignMaxpool⟩,
  ⟨"maxpool ignores NaNs", "maxpool", { specKernels with elem := { specElem with
    maxpool := fun c h w k inp => Array.ofFn (n := c * (h / k * (w / k))) fun q =>
      let ch := q.val / (h / k * (w / k))
      let y := q.val / (w / k) % (h / k)
      let x := q.val % (w / k)
      toWord <| (List.range (k * k)).foldl (fun acc t =>
        Model.maximumNumber acc (ofW (poolAt h w k inp ch y x t))) (ofW (poolAt h w k inp ch y x 0)) } }⟩,
  ⟨"addRelu returns the canonical NaN", "addRelu", { specKernels with elem := { specElem with
    addRelu := fun relu a b => (addReluSpec relu a b).map fun r => if isNaNW r then 0x7FC00000 else r } }⟩,
  ⟨"scaleShift rounds twice (no fma)", "scaleShift", { specKernels with elem := { specElem with
    scaleShift := fun relu c hw x s t => Array.ofFn (n := c * hw) fun i =>
      let ch := i.val / hw
      act relu <| toWord (Model.add (Model.mul (ofW x[i.val]!) (ofW s[ch]!)) (ofW t[ch]!)) } }⟩,
  ⟨"rmsnorm uses the old staged spec", "rmsnorm", { specKernels with norm := { specNorm with
    rmsnorm := rmsnormLegacyWith dotOf } }⟩,
  ⟨"layernorm uses the old staged spec", "layernorm", { specKernels with norm := { specNorm with
    layernorm := layernormLegacyWith dotOf } }⟩,
  -- found by the autoresearch pilot (2026-10-20), each missed before the input kinds they named
  ⟨"sum rounds per block of 64", "sum", withSum (blockedSum 64)⟩,
  ⟨"gemm accumulates k in blocks of 16", "gemm", { specKernels with dense := { specDense with
    gemm := gemmWith (blockedDot 16) } }⟩,
  ⟨"linear accumulates in blocks of 32", "linear", { specKernels with dense := { specDense with
    linear := linearWith (blockedDot 32) } }⟩,
  ⟨"dot rounds subnormal ties away from zero", "dot", withDot subnormalTiesAway⟩,
  ⟨"rmsnorm's square root rounds midpoints up", "rmsnorm", { specKernels with norm := { specNorm with
    rmsnorm := rmsnormSqrt sqrtTiesUp } }⟩,
  ⟨"rmsnorm's square root rounds midpoints down", "rmsnorm", { specKernels with norm := { specNorm with
    rmsnorm := rmsnormSqrt sqrtTiesDown } }⟩,
  ⟨"layernorm centres the variance on the rounded mean", "layernorm", { specKernels with norm :=
    { specNorm with layernorm := layernormRoundedMean } }⟩,
  -- found by the second autoresearch round (2026-10-21), and predicted variants (marked)
  ⟨"conv2d's full 4-channel tiles use the first channel's bias", "conv2d", { specKernels with conv :=
    { specConv with conv2d := fun s relu inp w b => conv2dSpec s relu inp w (tileBias s.cout b) } }⟩,
  ⟨"conv2d accumulates in tiles of 256 terms", "conv2d", { specKernels with conv := { specConv with
    conv2d := conv2dWith (blockedDot 256) } }⟩,
  ⟨"rmsnorm's final rounding sends ties away from zero", "rmsnorm", { specKernels with norm :=
    { specNorm with rmsnorm := rmsnormRound roundTiesAway } }⟩,
  ⟨"dot rounds the overflow tie down", "dot", withDot fun a b =>
    overflowTieDown ((List.range (min a.size b.size)).foldl
      (fun acc i => acc + rationalWord a[i]! * rationalWord b[i]!) 0) (dotSpec a b)⟩,
  ⟨"predicted: layernorm's final rounding sends ties away from zero", "layernorm",
    { specKernels with norm := { specNorm with layernorm := layernormRound roundTiesAway } }⟩,
  ⟨"predicted: linear's full 4-output tiles use the first output's bias", "linear",
    { specKernels with dense := { specDense with
      linear := fun rows i o relu X W b => linearSpec rows i o relu X W (tileBias o b) } }⟩,
  ⟨"predicted: sum rounds the overflow tie down", "sum", withSum fun x =>
    overflowTieDown (x.foldl (fun acc w => if finiteWord w then acc + rationalWord w else acc) 0) (sumSpec x)⟩
]

end

/-- Run the audit on every mutant: which are caught, by which kinds; exit 1 if any is missed. -/
def mutationRun (cases : Nat) : IO UInt32 := do
  let mut caught := 0
  for m in mutants do
    let (_, found) := audit m.kernels specKernels false cases 11
    let mine := found.filter (·.kernel == m.kernel)
    let ok := mine.size > 0
    if ok then caught := caught + 1
    IO.println s!"{if ok then "caught" else "MISSED"}  {m.name}: {mine.size} cases, kinds {(mine.map (·.kind)).toList.eraseDups}"
  IO.println s!"mutation score: {caught}/{mutants.length}"
  return if caught == mutants.length then 0 else 1

/-! ## Coverage of output classes (loop L2) -/

/-- The class of an output word. -/
def outClass (w : UInt32) : String :=
  if w == 0 then "+0" else if w == 0x80000000 then "-0"
  else if expOf w == 0 then "subnormal" else if expOf w != 255 then "normal"
  else if fracOf w == 0 then "inf" else "NaN"

/-- For every kernel, how many spec outputs fall in each class; a 0 marks an untested class. -/
def coverage (cases : Nat) : IO Unit := do
  let classes := ["+0", "-0", "subnormal", "normal", "inf", "NaN"]
  IO.println s!"| kernel | {" | ".intercalate classes} |"
  IO.println s!"|---|{"".intercalate (classes.map fun _ => "---:|")}"
  for (kernel, ki) in kernelNames.zipIdx do
    let mut counts := classes.map fun _ => 0
    for (kind, ti) in kinds.zipIdx do
      for c in [0:cases] do
        let o := runCase specKernels specKernels false kernel kind (1 + (ki * 1000003 + ti * 10007 + c).toUInt32)
        for w in o.spec do
          counts := counts.zipWith (fun n cl => if cl == outClass w then n + 1 else n) classes
    IO.println s!"| {kernel} | {" | ".intercalate (counts.map toString)} |"

/-- The self-test: the spec against itself is clean, and every mutant is caught. -/
def selftest : IO UInt32 := do
  let (_, clean) := audit specKernels specKernels false 6 7
  IO.println s!"spec against itself: {clean.size} mismatches"
  let code ← mutationRun 6
  IO.println (if clean.isEmpty && code == 0 then "selftest passed" else "selftest FAILED")
  return if clean.isEmpty && code == 0 then 0 else 1

/-- A string as a JSON string literal. -/
def jsonStr (s : String) : String :=
  "\"" ++ (s.replace "\\" "\\\\" |>.replace "\"" "\\\"" |>.replace "\n" " ") ++ "\""

/-- Evaluate one candidate mutant for the autoresearch loop: is it a real bug (its witness
differs from the spec), and does the audit catch it? Prints one JSON line. -/
def evaluate (m : Mutant) (witness : Outcome) (cases : Nat := 20) : IO UInt32 := do
  let valid := witness.entry != witness.spec
  let (_, found) := audit m.kernels specKernels false cases 11
  let mine := found.filter (·.kernel == m.kernel)
  let kindsHit := (mine.map (·.kind)).toList.eraseDups
  let fields := [("name", jsonStr m.name), ("kernel", jsonStr m.kernel), ("valid", toString valid),
    ("caught", toString (decide (mine.size > 0))), ("cases", toString mine.size),
    ("kinds", "[" ++ ", ".intercalate (kindsHit.map jsonStr) ++ "]"),
    ("witness", jsonStr (witness.shape ++ ": " ++ firstDiff witness.entry witness.spec))]
  IO.println ("{" ++ ", ".intercalate (fields.map fun (k, v) => jsonStr k ++ ": " ++ v) ++ "}")
  return 0

end ExactAudit

