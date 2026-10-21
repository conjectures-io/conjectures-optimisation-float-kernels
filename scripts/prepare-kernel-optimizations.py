"""Create proved optimization candidates in the ignored research data directory."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data/kernel-optimization/20261021/candidates"
OUT.mkdir(parents=True, exist_ok=True)
base = (ROOT / "kernels/exact/incumbent/Kernel.lean").read_text()
(OUT / "incumbent.lean").write_text(base)
conv = """
/-- Materialize each input patch once, sharing it between output channels. -/
def convCached (s : ConvShape) (relu : Bool) (inp wts b : Array Word) : Array Word :=
  let k := s.cin * s.kh * s.kw
  let patches := Array.ofFn (n := s.ho) fun y =>
    Array.ofFn (n := s.wo) fun x =>
      Array.ofFn (n := k) fun t => windowAt s inp y.val x.val t.val
  Array.ofFn (n := s.cout * (s.ho * s.wo)) fun q =>
    let o := q.val / (s.ho * s.wo)
    let y := q.val / s.wo % s.ho
    let x := q.val % s.wo
    act relu <| dotFn (k + 1)
      (fun t => if t < k then ((patches[y]!)[x]!)[t]! else oneW)
      (fun t => if t < k then wts[o * k + t]! else b[o]!)

theorem convCached_eq : convCached = conv2dWith dotFn := by
  funext s relu inp wts b
  unfold convCached conv2dWith
  apply congrArg Array.ofFn
  funext q
  have hq := q.isLt
  have hwo : 0 < s.wo := by
    by_contra h
    have hz : s.wo = 0 := by omega
    simp [hz] at hq
  have hho : 0 < s.ho := by
    by_contra h
    have hz : s.ho = 0 := by omega
    simp [hz] at hq
  simp only [dotFn_eq_dotOf, dotOf]
  congr 3
  funext t
  split_ifs with ht
  · simp [getElem!_pos, Nat.mod_lt _ hwo,
      Nat.mod_lt _ hho, ht]
  · rfl
"""
linear = """
/-- Append bias operands once per row/filter, removing per-term conditionals. -/
def linearCached (rows inW outW : Nat) (relu : Bool) (X W b : Array Word) : Array Word :=
  let xs := Array.ofFn (n := rows) fun i =>
    Array.ofFn (n := inW + 1) fun t => if t.val < inW then X[i.val * inW + t.val]! else oneW
  let ws := Array.ofFn (n := outW) fun j =>
    Array.ofFn (n := inW + 1) fun t => if t.val < inW then W[j.val * inW + t.val]! else b[j.val]!
  Array.ofFn (n := rows * outW) fun q =>
    act relu <| dotFn (inW + 1) (fun t => (xs[q.val / outW]!)[t]!)
      (fun t => (ws[q.val % outW]!)[t]!)

theorem linearCached_eq : linearCached = linearWith dotFn := by
  funext rows inW outW relu X W b
  unfold linearCached linearWith
  apply congrArg Array.ofFn
  funext q
  have hq := q.isLt
  have ho : 0 < outW := by
    by_contra h
    have hz : outW = 0 := by omega
    simp [hz] at hq
  have hrow : q.val / outW < rows := (Nat.div_lt_iff_lt_mul ho).mpr q.isLt
  simp only [dotFn_eq_dotOf, dotOf]
  congr 3 <;> funext t <;>
    simp [getElem!_pos, hrow, Nat.mod_lt _ ho, t.isLt]
"""
zero = """
/-- Avoid exponent-index arithmetic for a zero product (including signed zero). -/
@[inline] def zeroStep (acc : Acc) (wa wb : UInt32) : Acc :=
  if expOf wa = 255 ∨ expOf wb = 255 then acc.markSpecial
  else if mantOf wa = 0 ∨ mantOf wb = 0 then
    ⟨acc.pos, acc.neg, acc.special, acc.negZero && Bool.xor (signOf wa) (signOf wb)⟩
  else acc.add (mantOf wa * mantOf wb) (idxOf wa + idxOf wb)
    (Bool.xor (signOf wa) (signOf wb))

theorem zeroStep_eq : zeroStep = dotStep := by
  funext acc wa wb
  unfold zeroStep dotStep
  split_ifs with hs hz
  · rfl
  · have hm : mantOf wa * mantOf wb = 0 := Nat.mul_eq_zero.mpr hz
    cases acc
    simp [Acc.add, hm]
  · rfl

@[specialize] def zeroLoop (fa fb : Nat → UInt32) (pos neg : Array Nat) (sp nz : Bool)
    (index : Nat) : Nat → Acc
  | 0 => ⟨pos, neg, sp, nz⟩
  | count + 1 =>
    match zeroStep ⟨pos, neg, sp, nz⟩ (fa index) (fb index) with
    | ⟨p, n, s, z⟩ => zeroLoop fa fb p n s z (index + 1) count

theorem zeroLoop_eq (fa fb : Nat → UInt32) (pos neg : Array Nat) (sp nz : Bool)
    (index count : Nat) : zeroLoop fa fb pos neg sp nz index count =
      dotLoop fa fb pos neg sp nz index count := by
  induction count generalizing pos neg sp nz index with
  | zero => rfl
  | succ count ih =>
    simp only [zeroLoop, dotLoop, zeroStep_eq]
    exact ih _ _ _ _ _

@[inline] def zeroDot (n : Nat) (fa fb : Nat → UInt32) : UInt32 :=
  let acc := zeroLoop fa fb (Array.replicate 18 0) (Array.replicate 18 0) false true 0 n
  if acc.special then
    dotSpec (Array.ofFn (n := n) fun i => fa i) (Array.ofFn (n := n) fun i => fb i)
  else finishFast acc.pos acc.neg (-298) (n ≠ 0 && acc.negZero)

theorem zeroDot_eq : zeroDot = dotFn := by
  funext n fa fb
  simp only [zeroDot, dotFn, zeroLoop_eq]
"""
for name, code, old, new, lemma in [
    (
        "conv-cache",
        conv,
        "conv2dWith dotFn, depthwiseWith dotFn",
        "convCached, depthwiseWith dotFn",
        "convCached_eq",
    ),
    (
        "dense-cache",
        linear,
        "linearWith dotFn, gemmWith dotFn",
        "linearCached, gemmWith dotFn",
        "linearCached_eq",
    ),
    ("zero-fast", zero, "With dotFn", "With zeroDot", "zeroDot_eq"),
]:
    source = base.replace("/-! ## The entry -/", code + "\n/-! ## The entry -/")
    source = source.replace(old, new).replace("    maxpoolW_eq,", f"    {lemma}, maxpoolW_eq,")
    (OUT / (name + ".lean")).write_text(source)

# A second iteration: capture the selected row/patch outside the dot-product closure.
# The mathematical functions are definitionally unchanged; the compiler can now pass
# the selected array directly rather than repeatedly selecting it inside the loop.
conv_hoist = conv.replace(
    "    act relu <| dotFn (k + 1)",
    "    let patch := (patches[y]!)[x]!\n    act relu <| dotFn (k + 1)",
).replace("((patches[y]!)[x]!)[t]!", "patch[t]!")
linear_hoist = (
    linear.replace(
        "    act relu <| dotFn (inW + 1)",
        "    let xr := xs[q.val / outW]!\n    let wr := ws[q.val % outW]!\n"
        "    act relu <| dotFn (inW + 1)",
    )
    .replace("(xs[q.val / outW]!)[t]!", "xr[t]!")
    .replace("(ws[q.val % outW]!)[t]!", "wr[t]!")
)
for name, code, replacements, lemmas in [
    ("conv-hoist", conv_hoist, [("conv2dWith dotFn", "convCached")], "convCached_eq"),
    ("dense-hoist", linear_hoist, [("linearWith dotFn", "linearCached")], "linearCached_eq"),
    (
        "combined-hoist",
        conv_hoist + linear_hoist,
        [("conv2dWith dotFn", "convCached"), ("linearWith dotFn", "linearCached")],
        "convCached_eq, linearCached_eq",
    ),
]:
    # Replace only the entry record, keeping all equivalence theorem statements intact.
    before, entry = base.split("/-! ## The entry -/")
    for old, new in replacements:
        entry = entry.replace(old, new)
    entry = entry.replace("    maxpoolW_eq,", f"    {lemmas}, maxpoolW_eq,")
    (OUT / f"{name}.lean").write_text(before + code + "\n/-! ## The entry -/" + entry)

pointwise = """
/-- A checked 1×1, stride-one, unpadded convolution avoids generic window indexing. -/
def pointwiseConv (s : ConvShape) (relu : Bool) (inp wts b : Array Word) : Array Word :=
  if s.kh = 1 ∧ s.kw = 1 ∧ s.stride = 1 ∧ s.pad = 0 then
    Array.ofFn (n := s.cout * (s.ho * s.wo)) fun q =>
      let o := q.val / (s.ho * s.wo)
      let y := q.val / s.wo % s.ho
      let x := q.val % s.wo
      act relu <| dotFn (s.cin + 1)
        (fun t => if t < s.cin then
          if y ≥ s.h ∨ x ≥ s.w then 0 else inp[(t * s.h + y) * s.w + x]!
          else oneW)
        (fun t => if t < s.cin then wts[o * s.cin + t]! else b[o]!)
  else conv2dWith dotFn s relu inp wts b

theorem pointwiseConv_eq : pointwiseConv = conv2dWith dotFn := by
  funext s relu inp wts b
  unfold pointwiseConv
  split_ifs with h
  · rcases h with ⟨hkh, hkw, hs, hp⟩
    simp [conv2dWith, windowAt, hkh, hkw, hs, hp, Nat.mod_one]
  · rfl
"""
for name, code, function, lemmas in [
    ("pointwise", pointwise, "pointwiseConv", "pointwiseConv_eq"),
    (
        "conv-hybrid",
        conv_hoist
        + pointwise.replace(
            "else conv2dWith dotFn s relu inp wts b", "else convCached s relu inp wts b"
        ).replace(
            "  · rfl\n",
            "  · exact congrFun (congrFun (congrFun (congrFun "
            "(congrFun convCached_eq s) relu) inp) wts) b\n",
        ),
        "pointwiseConv",
        "pointwiseConv_eq",
    ),
]:
    before, entry = base.split("/-! ## The entry -/")
    entry = entry.replace("conv2dWith dotFn", function)
    entry = entry.replace("    maxpoolW_eq,", f"    {lemmas}, maxpoolW_eq,")
    (OUT / f"{name}.lean").write_text(before + code + "\n/-! ## The entry -/" + entry)
