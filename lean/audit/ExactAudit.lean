import ExactSuite
import Submission

/-!
# Audit of an exact-runtime entry (not used by the gate)

Calls each of the 12 kernels of the entry in `work/Submission.lean` and of the specs on the same
inputs and compares every output word. The inputs cover the suite's data types and the cases the
suite never times: subnormals, huge values, huge exponent gaps, NaNs and infinities, zeros, ties,
exact cancellation, and shapes at the edges (empty, mismatched and short arrays). A proved entry
can only fail here through a gap in what the gate checks.

    lake exe exactAudit [--malformed] [cases per kernel and kind] [seed]
    lake exe exactAudit --selftest       planted wrong kernels must be caught

`--malformed` adds short arrays (inputs shorter than their shape says); Lean then prints a panic
message for each read past the end, which is expected.

Exit code 0: every output equal; 1: a mismatch, listed with how to reproduce it.
-/

namespace ExactAudit

open ExactRuntime ExactSuite

/-! ## Inputs -/

/-- The input kinds: the suite's three, the edge cases, and all of them mixed. -/
def kinds : List String :=
  ["normal", "wide", "sparse", "subnormal", "huge", "gap", "special", "zeros", "negzero", "ties",
    "cancel", "mixed"]

/-- A sign and a fraction from `w`, with exponent field `e`. -/
@[inline] def withExp (w : UInt32) (e : UInt32) : UInt32 :=
  (w &&& (0x807FFFFF : UInt32)) ||| (e <<< (23 : UInt32))

/-- One word of an edge-case kind; `w` is random, `c` an independent random choice. -/
def edgeWord (kind : String) (w c : UInt32) : UInt32 :=
  let near := withExp w ((124 : UInt32) + (w >>> 23) % 7)
  match kind with
  | "subnormal" => w &&& 0x807FFFFF
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
even-seed one cancels term by term. -/
def input (kind : String) (n : Nat) (seed : UInt32) : Array UInt32 :=
  if kind == "normal" || kind == "wide" || kind == "sparse" then gen kind n seed else
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
  let relu := r[14]! % 2 == 0
  let arr (n : Nat) (j : UInt32) := input kind n (seed * 16 + j)
  match kernel with
  | "sum" =>
    let x := arr (d 0 0 64) 1
    ⟨s!"n={x.size}", #[E.core.sum x], #[S.core.sum x]⟩
  | "dot" =>
    let n := d 0 0 64
    let a := arr n 1
    let b := if r[15]! % 8 == 0 then arr (n + 1) 2 else arr n 2
    ⟨s!"n={a.size},{b.size}", #[E.core.dot a b], #[S.core.dot a b]⟩
  | "linear" =>
    let (rows, i, o) := (d 0 1 3, d 1 0 24, d 2 1 6)
    let X := maybeShort malformed r[15]! (arr (rows * i) 1)
    let W := arr (o * i) 2
    let b := arr o 3
    ⟨s!"{rows}x{i}->{o} relu={relu}", E.dense.linear rows i o relu X W b,
      S.dense.linear rows i o relu X W b⟩
  | "gemm" =>
    let (bt, m, k, n) := (d 0 1 2, d 1 1 4, d 2 0 12, d 3 1 4)
    let A := maybeShort malformed r[15]! (arr (bt * m * k) 1)
    let B := arr (bt * k * n) 2
    ⟨s!"{bt}x{m}x{k}x{n}", E.dense.gemm bt m k n A B, S.dense.gemm bt m k n A B⟩
  | "conv2d" | "depthwise" =>
    let s : ConvShape := ⟨d 0 1 3, d 1 1 6, d 2 1 6, d 3 1 3, d 4 1 3, d 5 1 3, d 6 1 2, d 7 0 2⟩
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
    let (c, h, w, k) := (d 0 1 3, d 1 1 7, d 2 1 7, d 3 1 3)
    let inp := maybeShort malformed r[15]! (arr (c * h * w) 1)
    let shape := s!"c={c} {h}x{w} k={k}"
    if kernel == "avgpool" then ⟨shape, E.elem.avgpool c h w k inp, S.elem.avgpool c h w k inp⟩
    else ⟨shape, E.elem.maxpool c h w k inp, S.elem.maxpool c h w k inp⟩
  | "addRelu" =>
    let n := d 0 0 32
    let a := arr n 1
    let b := maybeShort malformed r[15]! (arr n 2)
    ⟨s!"n={n} relu={relu}", E.elem.addRelu relu a b, S.elem.addRelu relu a b⟩
  | "scaleShift" =>
    let (c, hw) := (d 0 1 4, d 1 1 8)
    let x := maybeShort malformed r[15]! (arr (c * hw) 1)
    let s := arr c 2
    let t := arr c 3
    ⟨s!"c={c} hw={hw} relu={relu}", E.elem.scaleShift relu c hw x s t,
      S.elem.scaleShift relu c hw x s t⟩
  | "rmsnorm" | "layernorm" =>
    let (rows, n) := (d 0 1 3, d 1 0 24)
    let e := if r[13]! % 4 == 0 then (arr 1 4)[0]! else eps
    let x := maybeShort malformed r[15]! (arr (rows * n) 1)
    let g := arr n 2
    let shape := s!"{rows}x{n}"
    if kernel == "rmsnorm" then ⟨shape, E.norm.rmsnorm rows n e x g, S.norm.rmsnorm rows n e x g⟩
    else
      let b := arr n 3
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

/-! ## Planted bugs for the self-test -/

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

/-- Each planted bug must be found, in its own kernel only; the spec against itself must be clean. -/
def selftest : IO UInt32 := do
  let cases := 6
  let (_, clean) := audit specKernels specKernels false cases 7
  let mut ok := clean.isEmpty
  IO.println s!"spec against itself: {clean.size} mismatches"
  for (name, K, kernel) in [("flushSum", flushSum, "sum"), ("zeroSignMaxpool", zeroSignMaxpool, "maxpool"),
      ("foldDot", foldDot, "dot")] do
    let (_, found) := audit K specKernels false cases 7
    let elsewhere := found.filter (·.kernel != kernel)
    let caught := found.size > 0 && elsewhere.isEmpty
    ok := ok && caught
    IO.println s!"{name}: {found.size} mismatches in `{kernel}`, {elsewhere.size} elsewhere: {if caught then "caught" else "MISSED"}"
    for line in summary found do IO.println line
  IO.println (if ok then "selftest passed" else "selftest FAILED")
  return if ok then 0 else 1

end ExactAudit

open ExactAudit in
/-- `exactAudit [--malformed] [cases] [seed]` on the entry, or `exactAudit --selftest`. -/
def main (args : List String) : IO UInt32 := do
  if args == ["--selftest"] then return ← selftest
  let malformed := args.head? == some "--malformed"
  let args := if malformed then args.drop 1 else args
  let cases := (args.head? >>= String.toNat?).getD 20
  let seed := ((args.drop 1).head? >>= String.toNat?).getD 1 |>.toUInt32
  let t0 ← IO.monoMsNow
  let (total, found) := audit Submission.kernels ExactRuntime.specKernels malformed cases seed
  let t1 ← IO.monoMsNow
  IO.println s!"{total} cases ({kernelNames.length} kernels × {kinds.length} kinds × {cases}), seed {seed}, {t1 - t0} ms"
  if found.isEmpty then
    IO.println "every output equal to the spec"
    return 0
  IO.println s!"{found.size} mismatches:"
  for line in summary found do IO.println line
  for m in found.toList.take 10 do
    IO.println s!"  {m.kernel} {m.kind} seed={m.seed} {m.shape}: {m.detail}"
  return 1
