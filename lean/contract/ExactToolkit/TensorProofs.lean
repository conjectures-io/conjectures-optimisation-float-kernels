import ContractExactChecked

/-! Mathematical tensor coordinates and statistics, independently stated from flat loops.
The rounding primitive remains the pinned exact dot product; rational reduction meaning is
proved below. Bounds prevent the equations from relying on out-of-bounds default values. -/

namespace ExactRuntime.TensorMath

open FloatLib.Floats.Formats.BinaryInterchange FloatLib.Floats

def matrixAt (a : Array Word) (cols row col : Nat) : Word := a[row * cols + col]!
def batchAt (a : Array Word) (rows cols batch row col : Nat) : Word :=
  a[(batch * rows + row) * cols + col]!

theorem flatten_pair (row col width : Nat) (hw : 0 < width) (hc : col < width) :
    (row * width + col) / width = row ∧ (row * width + col) % width = col := by
  constructor
  · rw [Nat.mul_comm row width, Nat.mul_add_div hw, Nat.div_eq_of_lt hc, Nat.add_zero]
  · simp [Nat.add_mod, Nat.mod_eq_of_lt hc]

theorem flat_lt (row col rows cols : Nat) (hr : row < rows) (hc : col < cols) :
    row * cols + col < rows * cols := by
  have h := Nat.mul_le_mul_right cols (Nat.succ_le_of_lt hr)
  nlinarith

theorem linear_entry (rows inner outputs i j : Nat) (relu : Bool) (x w b : Array Word)
    (hi : i < rows) (hj : j < outputs) :
    (linearSpec rows inner outputs relu x w b)[i * outputs + j]! =
      act relu (dotOf (inner + 1)
        (fun t => if t < inner then matrixAt x inner i t else oneW)
        (fun t => if t < inner then matrixAt w inner j t else b[j]!)) := by
  have hflat := flat_lt i j rows outputs hi hj
  have hpair := flatten_pair i j outputs (by omega) hj
  rw [getElem!_pos _ _ (by simpa using hflat)]
  simp [linearSpec, linearWith, matrixAt, hpair.1, hpair.2]

theorem gemm_entry (batch m k n β i j : Nat) (a b : Array Word)
    (hβ : β < batch) (hi : i < m) (hj : j < n) :
    (gemmSpec batch m k n a b)[(β * m + i) * n + j]! =
      dotOf k (fun t => batchAt a m k β i t) (fun t => batchAt b k n β t j) := by
  have hp := flatten_pair (β * m + i) j n (by omega) hj
  have hβi := flatten_pair β i m (by omega) hi
  have hbdiv : ((β * m + i) * n + j) / (m * n) = β := by
    rw [Nat.mul_comm m n, ← Nat.div_div_eq_div_mul, hp.1, hβi.1]
  have hflat := flat_lt (β * m + i) j (batch * m) n (flat_lt β i batch m hβ hi) hj
  rw [getElem!_pos _ _ (by simpa [Nat.mul_assoc] using hflat)]
  simp [gemmSpec, gemmWith, batchAt, hbdiv, hp.1, hp.2, hβi.2]

theorem linear_input_bound (rows inner outputs i t : Nat) (x w b : Array Word)
    (hv : LinearValid rows inner outputs x w b) (hi : i < rows) (ht : t < inner) :
    i * inner + t < x.size := by
  rw [hv.2.2.2.1]
  exact flat_lt i t rows inner hi ht

theorem linear_weight_bound (rows inner outputs j t : Nat) (x w b : Array Word)
    (hv : LinearValid rows inner outputs x w b) (hj : j < outputs) (ht : t < inner) :
    j * inner + t < w.size := by
  rw [hv.2.2.2.2.1]
  exact flat_lt j t outputs inner hj ht

theorem conv_input_bound (s : ConvShape) (dw : Bool) (input w b : Array Word)
    (hv : ConvValid s dw input w b) (c y x : Nat)
    (hc : c < s.cin) (hy : y < s.h) (hx : x < s.w) :
    (c * s.h + y) * s.w + x < input.size := by
  rw [hv.2.1]
  exact flat_lt (c * s.h + y) x (s.cin * s.h) s.w (flat_lt c y s.cin s.h hc hy) hx

/-- Signed spatial coordinates express padding without saturating subtraction. -/
def paddedAt (s : ConvShape) (input : Array Word) (c : Nat) (y x : Int) : Word :=
  if 0 ≤ y ∧ y < s.h ∧ 0 ≤ x ∧ x < s.w then
    input[(c * s.h + y.toNat) * s.w + x.toNat]!
  else 0

theorem window_coordinates (s : ConvShape) (input : Array Word) (y x c dy dx : Nat)
    (hdy : dy < s.kh) (hdx : dx < s.kw) :
    windowAt s input y x ((c * s.kh + dy) * s.kw + dx) =
      paddedAt s input c ((y * s.stride + dy : Nat) - (s.pad : Int))
        ((x * s.stride + dx : Nat) - (s.pad : Int)) := by
  have hp := flatten_pair (c * s.kh + dy) dx s.kw (by omega) hdx
  have hc := flatten_pair c dy s.kh (by omega) hdy
  have hchannel : ((c * s.kh + dy) * s.kw + dx) / (s.kh * s.kw) = c := by
    rw [Nat.mul_comm s.kh s.kw, ← Nat.div_div_eq_div_mul, hp.1, hc.1]
  simp only [windowAt, hp.1, hp.2, hc.2, hchannel, paddedAt]
  by_cases hy : s.pad ≤ y * s.stride + dy
  · by_cases hx : s.pad ≤ x * s.stride + dx
    · have hy' := Int.ofNat_sub hy
      have hx' := Int.ofNat_sub hx
      simp only [← hy', ← hx', Int.natCast_nonneg, true_and, Int.toNat_natCast]
      simp only [Nat.not_lt.mpr hy, Nat.not_lt.mpr hx, false_or]
      split_ifs <;> simp_all <;> omega
    · have hx' : (x * s.stride + dx : Int) - s.pad < 0 := by omega
      simp [Nat.lt_of_not_ge hx]
      omega
  · have hy' : (y * s.stride + dy : Int) - s.pad < 0 := by omega
    simp [Nat.lt_of_not_ge hy]
    omega

theorem pool_coordinates (h w k ch y x dy dx : Nat) (input : Array Word)
    (hdy : dy < k) (hdx : dx < k) :
    poolAt h w k input ch y x (dy * k + dx) =
      input[(ch * h + y * k + dy) * w + x * k + dx]! := by
  have hp := flatten_pair dy dx k (by omega) hdx
  simp [poolAt, hp.1, hp.2]

theorem pool_input_bound (c h w k ch y x dy dx : Nat) (input : Array Word)
    (hv : PoolValid c h w k input) (hc : ch < c) (hy : y < h / k) (hx : x < w / k)
    (hdy : dy < k) (hdx : dx < k) :
    (ch * h + y * k + dy) * w + x * k + dx < input.size := by
  have hyy : y * k + dy < h := lt_of_lt_of_le (flat_lt y dy (h / k) k hy hdy)
    (Nat.div_mul_le_self h k)
  have hxx : x * k + dx < w := lt_of_lt_of_le (flat_lt x dx (w / k) k hx hdx)
    (Nat.div_mul_le_self w k)
  rw [hv.2.2.2.2.2.2]
  simpa [Nat.add_assoc] using flat_lt (ch * h + (y * k + dy)) (x * k + dx)
    (c * h) w (flat_lt ch (y * k + dy) c h hc hyy) hxx

theorem triple_coordinates (batch row col rows cols : Nat) (hr : row < rows) (hc : col < cols) :
    ((batch * rows + row) * cols + col) / (rows * cols) = batch ∧
    ((batch * rows + row) * cols + col) / cols % rows = row ∧
    ((batch * rows + row) * cols + col) % cols = col := by
  have hp := flatten_pair (batch * rows + row) col cols (by omega) hc
  have hb := flatten_pair batch row rows (by omega) hr
  refine ⟨?_, by rw [hp.1, hb.2], hp.2⟩
  rw [Nat.mul_comm rows cols, ← Nat.div_div_eq_div_mul, hp.1, hb.1]

theorem conv2d_entry (s : ConvShape) (relu : Bool) (input w b : Array Word) (o y x : Nat)
    (ho : o < s.cout) (hy : y < s.ho) (hx : x < s.wo) :
    (conv2dSpec s relu input w b)[(o * s.ho + y) * s.wo + x]! =
      act relu (dotOf (s.cin * s.kh * s.kw + 1)
        (fun t => if t < s.cin * s.kh * s.kw then windowAt s input y x t else oneW)
        (fun t => if t < s.cin * s.kh * s.kw then w[o * (s.cin * s.kh * s.kw) + t]!
          else b[o]!)) := by
  have hp := triple_coordinates o y x s.ho s.wo hy hx
  have hflat := flat_lt (o * s.ho + y) x (s.cout * s.ho) s.wo
    (flat_lt o y s.cout s.ho ho hy) hx
  rw [getElem!_pos _ _ (by simpa [Nat.mul_assoc] using hflat)]
  simp [conv2dSpec, conv2dWith, hp.1, hp.2.1, hp.2.2, Nat.mul_assoc]

theorem depthwise_entry (s : ConvShape) (relu : Bool) (input w b : Array Word) (c y x : Nat)
    (hc : c < s.cin) (hy : y < s.ho) (hx : x < s.wo) :
    (depthwiseSpec s relu input w b)[(c * s.ho + y) * s.wo + x]! =
      act relu (dotOf (s.kh * s.kw + 1)
        (fun t => if t < s.kh * s.kw then windowAt s input y x (c * (s.kh * s.kw) + t)
          else oneW)
        (fun t => if t < s.kh * s.kw then w[c * (s.kh * s.kw) + t]! else b[c]!)) := by
  have hp := triple_coordinates c y x s.ho s.wo hy hx
  have hflat := flat_lt (c * s.ho + y) x (s.cin * s.ho) s.wo
    (flat_lt c y s.cin s.ho hc hy) hx
  rw [getElem!_pos _ _ (by simpa [Nat.mul_assoc] using hflat)]
  simp [depthwiseSpec, depthwiseWith, hp.1, hp.2.1, hp.2.2]

theorem addRelu_entry (relu : Bool) (a b : Array Word) (i : Nat) (hi : i < a.size) :
    (addReluSpec relu a b)[i]! =
      act relu (toWord (Model.add (ofW a[i]!) (ofW b[i]!))) := by
  rw [getElem!_pos _ _ (by simpa [addReluSpec] using hi)]
  simp [addReluSpec, getElem!_pos a i hi]

theorem scaleShift_entry (relu : Bool) (c hw ch t : Nat) (x s b : Array Word)
    (hc : ch < c) (ht : t < hw) :
    (scaleShiftSpec relu c hw x s b)[ch * hw + t]! =
      act relu (toWord (Model.fma (ofW x[ch * hw + t]!) (ofW s[ch]!) (ofW b[ch]!))) := by
  have hp := flatten_pair ch t hw (by omega) ht
  rw [getElem!_pos _ _ (by simpa [scaleShiftSpec] using flat_lt ch t c hw hc ht)]
  simp [scaleShiftSpec, hp.1]

theorem avgpool_entry (c h w k ch y x : Nat) (input : Array Word)
    (hc : ch < c) (hy : y < h / k) (hx : x < w / k) :
    (avgpoolSpec c h w k input)[(ch * (h / k) + y) * (w / k) + x]! =
      toWord (Model.div
        (ofW (dotOf (k * k) (poolAt h w k input ch y x) (fun _ => oneW)))
        (ofW (natW (k * k)))) := by
  have hp := triple_coordinates ch y x (h / k) (w / k) hy hx
  have hflat := flat_lt (ch * (h / k) + y) x (c * (h / k)) (w / k)
    (flat_lt ch y c (h / k) hc hy) hx
  rw [getElem!_pos _ _ (by simpa [avgpoolSpec, avgpoolWith, Nat.mul_assoc] using hflat)]
  simp [avgpoolSpec, avgpoolWith, hp.1, hp.2.1, hp.2.2]

theorem maxpool_entry (c h w k ch y x : Nat) (input : Array Word)
    (hc : ch < c) (hy : y < h / k) (hx : x < w / k) :
    (maxpoolSpec c h w k input)[(ch * (h / k) + y) * (w / k) + x]! =
      toWord ((List.range (k * k)).foldl (fun acc t =>
        Model.maximum acc (ofW (poolAt h w k input ch y x t)))
        (ofW (poolAt h w k input ch y x 0))) := by
  have hp := triple_coordinates ch y x (h / k) (w / k) hy hx
  have hflat := flat_lt (ch * (h / k) + y) x (c * (h / k)) (w / k)
    (flat_lt ch y c (h / k) hc hy) hx
  rw [getElem!_pos _ _ (by simpa [maxpoolSpec, Nat.mul_assoc] using hflat)]
  simp [maxpoolSpec, hp.1, hp.2.1, hp.2.2]

theorem ofFn_get (n : Nat) (f : Fin n → Word) (i : Nat) (hi : i < n) :
    (Array.ofFn f)[i]! = f ⟨i, hi⟩ := by
  rw [getElem!_pos _ _ (by simpa using hi), Array.getElem_ofFn]

theorem rmsnorm_entry (rows n i j : Nat) (eps : Word) (x g : Array Word)
    (hi : i < rows) (hj : j < n) (hd : stableNormDomain rows n eps x g = true) :
    (rmsnormSpec rows n eps x g)[i * n + j]! =
      roundRational (rationalWord (matrixAt x n i j) * rationalWord g[j]! /
        rationalWord (sqrtRational (rowVariance n x i 0 + rationalWord eps))) := by
  rw [rmsnorm_stable_on_domain rows n eps x g hd]
  have hp := flatten_pair i j n (by omega) hj
  unfold rmsnormStable
  rw [ofFn_get _ _ _ (flat_lt i j rows n hi hj)]
  simp only [hp.1, hp.2, ofFn_get _ _ _ hi, matrixAt]

theorem layernorm_entry (rows n i j : Nat) (eps : Word) (x g b : Array Word)
    (hi : i < rows) (hj : j < n) (hd : stableNormDomain rows n eps x g = true)
    (hb : b.size = n) (hf : b.all finiteWord = true) :
    (layernormSpec rows n eps x g b)[i * n + j]! =
      roundRational ((rationalWord (matrixAt x n i j) - rowMean n x i) * rationalWord g[j]! /
        rationalWord (sqrtRational (rowVariance n x i (rowMean n x i) + rationalWord eps)) +
          rationalWord b[j]!) := by
  rw [layernorm_stable_on_domain rows n eps x g b hd hb hf]
  have hp := flatten_pair i j n (by omega) hj
  unfold layernormStable
  rw [ofFn_get _ _ _ (flat_lt i j rows n hi hj)]
  simp only [hp.1, hp.2, matrixAt]
  simp only [getElem!_pos (Array.ofFn _) i (by simpa using hi), Array.getElem_ofFn]

theorem fold_range_sum (f : Nat → ℚ) (n : Nat) (total : ℚ) :
    (List.range n).foldl (fun acc j => acc + f j) total =
      total + ∑ j ∈ Finset.range n, f j := by
  induction n with
  | zero => simp
  | succ n ih =>
    simp only [List.range_succ, List.foldl_append, List.foldl_cons, List.foldl_nil,
      ih, Finset.sum_range_succ]
    ring

theorem rowMean_math (n : Nat) (x : Array Word) (row : Nat) :
    rowMean n x row = (∑ j ∈ Finset.range n, rationalWord (matrixAt x n row j)) / n := by
  simp [rowMean, matrixAt, fold_range_sum]

theorem rowVariance_math (n : Nat) (x : Array Word) (row : Nat) (center : ℚ) :
    rowVariance n x row center =
      (∑ j ∈ Finset.range n, (rationalWord (matrixAt x n row j) - center) ^ 2) / n := by
  simp [rowVariance, matrixAt, fold_range_sum]

theorem centered_sum_zero (n : Nat) (x : Array Word) (row : Nat) (hn : n ≠ 0) :
    (∑ j ∈ Finset.range n, (rationalWord (matrixAt x n row j) - rowMean n x row)) = 0 := by
  simp only [Finset.sum_sub_distrib, Finset.sum_const, Finset.card_range, nsmul_eq_mul,
    rowMean_math]
  have hn' : (n : ℚ) ≠ 0 := by exact_mod_cast hn
  field_simp
  ring

theorem variance_nonneg (n : Nat) (x : Array Word) (row : Nat) (center : ℚ) :
    0 ≤ rowVariance n x row center := by
  rw [rowVariance_math]
  exact div_nonneg (Finset.sum_nonneg (fun _ _ => sq_nonneg _)) (by positivity)

theorem variance_second_moment (n : Nat) (x : Array Word) (row : Nat) (hn : n ≠ 0) :
    rowVariance n x row (rowMean n x row) =
      (∑ j ∈ Finset.range n, rationalWord (matrixAt x n row j) ^ 2) / n -
        rowMean n x row ^ 2 := by
  let v := fun j => rationalWord (matrixAt x n row j)
  let m := rowMean n x row
  have hsum : (∑ j ∈ Finset.range n, (v j - m) ^ 2) =
      (∑ j ∈ Finset.range n, v j ^ 2) - 2 * m * (∑ j ∈ Finset.range n, v j) + n * m ^ 2 := by
    calc
      _ = ∑ j ∈ Finset.range n, (v j ^ 2 - (2 * m) * v j + m ^ 2) := by
        apply Finset.sum_congr rfl
        intro j _
        ring
      _ = _ := by
        rw [Finset.sum_add_distrib, Finset.sum_sub_distrib, ← Finset.mul_sum]
        simp
  rw [rowVariance_math]
  change (∑ j ∈ Finset.range n, (v j - m) ^ 2) / n = _
  rw [hsum]
  have hm : m = (∑ j ∈ Finset.range n, v j) / n := rowMean_math n x row
  change _ = (∑ j ∈ Finset.range n, v j ^ 2) / n - m ^ 2
  rw [hm]
  have hn' : (n : ℚ) ≠ 0 := by exact_mod_cast hn
  field_simp
  ring

theorem scaled_product (a b : Word) :
    ((sv a : ℚ) * sv b * (2 : ℚ) ^ (idxOf a + idxOf b)) * 2 ^ (-298 : Int) =
      rationalWord a * rationalWord b := by
  simp only [rationalWord, decodeD_toRat]
  rw [show (-298 : Int) = -149 + -149 by norm_num, zpow_add₀ two_ne_zero]
  rw [zpow_sub₀ two_ne_zero, zpow_sub₀ two_ne_zero, zpow_natCast, zpow_natCast, pow_add]
  norm_num [zpow_neg, zpow_natCast]
  ring

/-- The reducer's scaled integer is exactly the mathematical sum of products. -/
theorem dsum_math (a b : Array Word) (start n : Nat) :
    (dsum a b start n : ℚ) * 2 ^ (-298 : Int) =
      ∑ j ∈ Finset.range n, rationalWord a[start + j]! * rationalWord b[start + j]! := by
  induction n generalizing start with
  | zero => simp [dsum]
  | succ n ih =>
    rw [dsum]
    push_cast
    rw [add_mul, ih]
    conv_rhs => rw [Finset.sum_range_succ']
    simp only [Nat.add_zero]
    rw [scaled_product]
    have ht : (∑ j ∈ Finset.range n,
        rationalWord a[start + 1 + j]! * rationalWord b[start + 1 + j]!) =
        ∑ j ∈ Finset.range n,
          rationalWord a[start + (j + 1)]! * rationalWord b[start + (j + 1)]! := by
      apply Finset.sum_congr rfl
      intro j _
      simp [Nat.add_assoc, Nat.add_comm, Nat.add_left_comm]
    rw [ht]
    ring

/-- Independent mathematical total of a finite dot product. -/
def exactDot (a b : Array Word) : ℚ :=
  ∑ j ∈ Finset.range a.size, rationalWord a[j]! * rationalWord b[j]!

theorem finiteDot_math (a b : Array Word) (hs : a.size = b.size)
    (hf : ∀ j, j < a.size → expOf a[j]! ≠ 255 ∧ expOf b[j]! ≠ 255) :
    Model.Reduction.Semantics.finiteDotSlice
      (a.map (Model.ofUInt32 (fmt := Fmt))) (b.map (Model.ofUInt32 (fmt := Fmt)))
      0 a.size (by simp) (by simp [hs]) = exactDot a b := by
  rw [finiteDotSlice_eq a b 0 a.size (by simp) (by simp [hs])
    (by intro j _ hj; exact hf j (by omega)), dsum_math]
  simp [exactDot]

/-- For a nonzero finite total, the output is exactly one FloatLib rounding of that total.
The existential dyadic is only a representation of the independently stated rational sum. -/
theorem dotSpec_nonzero_math (a b : Array Word) (hs : a.size = b.size)
    (hf : ∀ j, j < a.size → expOf a[j]! ≠ 255 ∧ expOf b[j]! ≠ 255)
    (hn : exactDot a b ≠ 0) :
    ∃ d : FloatLib.Numerics.Dyadic,
      d.toRat = exactDot a b ∧ dotSpec a b = toWord (Model.roundDyadic Fmt d) := by
  let L := a.map (Model.ofUInt32 (fmt := Fmt))
  let R := b.map (Model.ofUInt32 (fmt := Fmt))
  have hLR : L.size = R.size := by simp [L, R, hs]
  have hLf : ∀ j (hj : j < L.size), Model.isFinite L[j] = true := by
    intro j hj
    have hj' : j < a.size := by simpa [L] using hj
    have he := (hf j hj').1
    rw [getElem!_pos a j hj'] at he
    simp [L, isFinite_ofUInt32, he]
  have hRf : ∀ j (hj : j < R.size), Model.isFinite R[j] = true := by
    intro j hj
    have hj' : j < b.size := by simpa [R] using hj
    have he := (hf j (by omega)).2
    rw [getElem!_pos b j hj'] at he
    simp [R, isFinite_ofUInt32, he]
  have hex := Model.Reduction.Internal.dotState_exact_toRat (destination := Fmt) L R hLR hLf hRf
  have hmath : Model.Reduction.Semantics.finiteDotSlice L R 0 L.size (by simp)
      (by simp [hLR]) = exactDot a b := by
    simpa [L, R] using finiteDot_math a b hs hf
  have hn' : Model.Reduction.Semantics.finiteDot L.toList R.toList ≠ .ok 0 := by
    rw [Model.Reduction.Semantics.finiteDot_toList_eq_slice L R hLR, hmath]
    simpa using hn
  refine ⟨(Model.Reduction.Internal.dotState L R hLR :
    Model.Reduction.Internal.State Fmt).exact, hex.trans hmath, ?_⟩
  unfold dotSpec Model.dot
  rw [Model.Reduction.dotWithStatus_eq_round_of_finite_nonzero Fmt L R .nearestEven
    hLR hLf hRf hn']
  rfl

/-- Zero totals obey the IEEE sign rule: -0 only for a nonempty set of exclusively
negative-zero products; exact cancellation and the empty dot return +0. -/
theorem dotSpec_zero_math (a b : Array Word) (hs : a.size = b.size)
    (hf : ∀ j, j < a.size → expOf a[j]! ≠ 255 ∧ expOf b[j]! ≠ 255)
    (hz : exactDot a b = 0) :
    dotSpec a b = toWord (Model.zero Fmt (decide (a.size ≠ 0) && dallNZ a b 0 a.size)) := by
  let L := a.map (Model.ofUInt32 (fmt := Fmt))
  let R := b.map (Model.ofUInt32 (fmt := Fmt))
  have hLR : L.size = R.size := by simp [L, R, hs]
  have hLf : ∀ j (hj : j < L.size), Model.isFinite L[j] = true := by
    intro j hj
    have hj' : j < a.size := by simpa [L] using hj
    have he := (hf j hj').1
    rw [getElem!_pos a j hj'] at he
    simp [L, isFinite_ofUInt32, he]
  have hRf : ∀ j (hj : j < R.size), Model.isFinite R[j] = true := by
    intro j hj
    have hj' : j < b.size := by simpa [R] using hj
    have he := (hf j (by omega)).2
    rw [getElem!_pos b j hj'] at he
    simp [R, isFinite_ofUInt32, he]
  let state : Model.Reduction.Internal.State Fmt := Model.Reduction.Internal.dotState L R hLR
  have hex := Model.Reduction.Internal.dotState_exact_toRat (destination := Fmt) L R hLR hLf hRf
  have hmath : Model.Reduction.Semantics.finiteDotSlice L R 0 L.size (by simp)
      (by simp [hLR]) = exactDot a b := by
    simpa [L, R] using finiteDot_math a b hs hf
  have hzero : state.exact.significand = 0 := by
    rw [← FloatLib.Numerics.Dyadic.toRat_eq_zero_iff]
    exact (hex.trans hmath).trans hz
  have flags := dotStateLoop_flags a b {} 0 a.size (by simp) (by simp [hs])
    (by intro j _ hj; exact hf j (by omega))
  have hflags : state.sawFiniteTerm = decide (a.size ≠ 0) ∧
      state.allTermsNegativeZero = dallNZ a b 0 a.size := by
    simpa [state, Model.Reduction.Internal.dotState, L, R] using flags
  unfold dotSpec Model.dot
  rw [Model.Reduction.dotWithStatus_eq_zero_of_finite Fmt L R .nearestEven hLR hLf hRf hzero]
  simp only [Except.map]
  change toWord (Model.zero Fmt (state.sawFiniteTerm && (state.allTermsNegativeZero ||
    !state.allTermsPositiveZero &&
      (Model.IEEERoundingMode.nearestEven == Model.IEEERoundingMode.towardNegativeInfinity)))) = _
  simp [hflags.1, hflags.2]

theorem rational_one : rationalWord oneW = 1 := by
  have he : expOf oneW = 127 := by
    apply UInt32.toNat_inj.mp
    norm_num [expOf, oneW, UInt32.toNat_and, UInt32.toNat_shiftRight]
  have hf : fracOf oneW = 0 := by
    apply UInt32.toNat_inj.mp
    norm_num [fracOf, oneW, UInt32.toNat_and]
  have hs : signOf oneW = false := by
    have hz : oneW &&& 0x80000000 = 0 := by
      apply UInt32.toNat_inj.mp
      norm_num [oneW, UInt32.toNat_and]
    simp [signOf, hz]
  norm_num [rationalWord, decodeD, he, hf, hs, FloatLib.Numerics.Dyadic.toRat,
    FloatLib.Numerics.Dyadic.signedSignificand]

theorem rational_zero : rationalWord (0 : Word) = 0 := by
  norm_num [rationalWord, decodeD, expOf, fracOf, signOf, FloatLib.Numerics.Dyadic.toRat,
    FloatLib.Numerics.Dyadic.signedSignificand]

theorem relu_finite_math (w : Word) (hf : expOf w ≠ 255) :
    rationalWord (reluW w) = max 0 (rationalWord w) := by
  have hr : reluW w = if signOf w then 0 else w := by simp [reluW, signOf, hf]
  have hp : (0 : ℚ) < 2 ^ ((idxOf w : Int) - 149) := zpow_pos two_pos _
  have hm : (0 : ℚ) ≤ mantOf w := by positivity
  rw [hr]
  split_ifs with hs
  · have hn : rationalWord w ≤ 0 := by
      simp only [rationalWord, decodeD_toRat, sv, hs, ite_true, Int.cast_neg, Int.cast_natCast]
      exact mul_nonpos_of_nonpos_of_nonneg (neg_nonpos.mpr hm) hp.le
    rw [rational_zero, max_eq_left hn]
  · have hn : 0 ≤ rationalWord w := by
      simp only [rationalWord, decodeD_toRat, sv, hs, ite_false, Int.cast_natCast]
      exact mul_nonneg hm hp.le
    rw [max_eq_right hn]

theorem relu_preserves_nan (w : Word) (he : expOf w = 255) (hf : fracOf w ≠ 0) :
    reluW w = w := by simp [reluW, he, hf]

theorem exactDot_ofFn (n : Nat) (fa fb : Nat → Word) :
    exactDot (Array.ofFn (n := n) fun i => fa i.val)
      (Array.ofFn (n := n) fun i => fb i.val) =
        ∑ j ∈ Finset.range n, rationalWord (fa j) * rationalWord (fb j) := by
  unfold exactDot
  simp only [Array.size_ofFn]
  apply Finset.sum_congr rfl
  intro j hj
  rw [ofFn_get _ _ _ (Finset.mem_range.mp hj), ofFn_get _ _ _ (Finset.mem_range.mp hj)]

theorem dotOf_nonzero_math (n : Nat) (fa fb : Nat → Word)
    (hf : ∀ j, j < n → expOf (fa j) ≠ 255 ∧ expOf (fb j) ≠ 255)
    (hn : (∑ j ∈ Finset.range n, rationalWord (fa j) * rationalWord (fb j)) ≠ 0) :
    ∃ d : FloatLib.Numerics.Dyadic,
      d.toRat = (∑ j ∈ Finset.range n, rationalWord (fa j) * rationalWord (fb j)) ∧
        dotOf n fa fb = toWord (Model.roundDyadic Fmt d) := by
  let a := Array.ofFn (n := n) fun i => fa i.val
  let b := Array.ofFn (n := n) fun i => fb i.val
  have hmath : exactDot a b = _ := exactDot_ofFn n fa fb
  have hf' : ∀ j, j < a.size → expOf a[j]! ≠ 255 ∧ expOf b[j]! ≠ 255 := by
    intro j hj
    have hj' : j < n := by simpa [a] using hj
    simpa only [a, b, ofFn_get _ _ _ hj'] using hf j hj'
  obtain ⟨d, hd, hw⟩ := dotSpec_nonzero_math a b (by simp [a, b]) hf' (by rwa [hmath])
  exact ⟨d, hd.trans hmath, hw⟩

def linearValue (inner i j : Nat) (x w b : Array Word) : ℚ :=
  (∑ t ∈ Finset.range inner,
    rationalWord (matrixAt x inner i t) * rationalWord (matrixAt w inner j t)) + rationalWord b[j]!

theorem linear_total (inner i j : Nat) (x w b : Array Word) :
    (∑ t ∈ Finset.range (inner + 1),
      rationalWord (if t < inner then matrixAt x inner i t else oneW) *
      rationalWord (if t < inner then matrixAt w inner j t else b[j]!)) =
        linearValue inner i j x w b := by
  rw [Finset.sum_range_succ]
  simp only [lt_self_iff_false, ite_false, rational_one, one_mul, linearValue]
  congr 1
  apply Finset.sum_congr rfl
  intro t ht
  simp only [if_pos (Finset.mem_range.mp ht)]

/-- A complete affine-cell theorem: valid finite arrays, mathematical row dot plus bias,
and one final rounding (followed by optional ReLU). -/
theorem linear_nonzero_math (rows inner outputs i j : Nat) (relu : Bool) (x w b : Array Word)
    (hv : LinearValid rows inner outputs x w b) (hi : i < rows) (hj : j < outputs)
    (hx : ∀ t, t < x.size → expOf x[t]! ≠ 255)
    (hw : ∀ t, t < w.size → expOf w[t]! ≠ 255)
    (hb : ∀ t, t < b.size → expOf b[t]! ≠ 255)
    (hn : linearValue inner i j x w b ≠ 0) :
    ∃ d : FloatLib.Numerics.Dyadic, d.toRat = linearValue inner i j x w b ∧
      (linearSpec rows inner outputs relu x w b)[i * outputs + j]! =
        act relu (toWord (Model.roundDyadic Fmt d)) := by
  let fa := fun t => if t < inner then matrixAt x inner i t else oneW
  let fb := fun t => if t < inner then matrixAt w inner j t else b[j]!
  have hf : ∀ t, t < inner + 1 → expOf (fa t) ≠ 255 ∧ expOf (fb t) ≠ 255 := by
    intro t ht
    by_cases hti : t < inner
    · simpa only [fa, fb, hti, ite_true, matrixAt] using
        And.intro (hx _ (linear_input_bound rows inner outputs i t x w b hv hi hti))
          (hw _ (linear_weight_bound rows inner outputs j t x w b hv hj hti))
    · have hone : expOf oneW ≠ 255 := by
        intro h
        have h' := congrArg UInt32.toNat h
        norm_num [expOf, oneW, UInt32.toNat_and, UInt32.toNat_shiftRight] at h'
      have hjb : j < b.size := by simpa [hv.2.2.2.2.2] using hj
      simpa only [fa, fb, hti, ite_false] using And.intro hone (hb j hjb)
  obtain ⟨d, hd, hword⟩ := dotOf_nonzero_math (inner + 1) fa fb hf
    (by simpa only [fa, fb, linear_total] using hn)
  refine ⟨d, hd.trans (linear_total inner i j x w b), ?_⟩
  rw [linear_entry rows inner outputs i j relu x w b hi hj]
  exact congrArg (act relu) hword

theorem sum_blocks (n k : Nat) (f : Nat → ℚ) :
    (∑ t ∈ Finset.range (n * k), f t) =
      ∑ i ∈ Finset.range n, ∑ j ∈ Finset.range k, f (i * k + j) := by
  induction n with
  | zero => simp
  | succ n ih => rw [Nat.succ_mul, Finset.sum_range_add, ih, Finset.sum_range_succ]

def convValue (s : ConvShape) (input w b : Array Word) (o y x : Nat) : ℚ :=
  (∑ c ∈ Finset.range s.cin, ∑ dy ∈ Finset.range s.kh, ∑ dx ∈ Finset.range s.kw,
    rationalWord (paddedAt s input c ((y * s.stride + dy : Nat) - (s.pad : Int))
      ((x * s.stride + dx : Nat) - (s.pad : Int))) *
        rationalWord w[((o * s.cin + c) * s.kh + dy) * s.kw + dx]!) + rationalWord b[o]!

/-- The flat convolution reduction is exactly the nested channel/filter sum plus bias. -/
theorem conv_total (s : ConvShape) (input w b : Array Word) (o y x : Nat) :
    (∑ t ∈ Finset.range (s.cin * s.kh * s.kw + 1),
      rationalWord (if t < s.cin * s.kh * s.kw then windowAt s input y x t else oneW) *
      rationalWord (if t < s.cin * s.kh * s.kw then w[o * (s.cin * s.kh * s.kw) + t]!
        else b[o]!)) = convValue s input w b o y x := by
  rw [Finset.sum_range_succ]
  simp only [lt_self_iff_false, ite_false, rational_one, one_mul, convValue]
  congr 1
  have hinner : (∑ t ∈ Finset.range (s.cin * s.kh * s.kw),
      rationalWord (if t < s.cin * s.kh * s.kw then windowAt s input y x t else oneW) *
      rationalWord (if t < s.cin * s.kh * s.kw then w[o * (s.cin * s.kh * s.kw) + t]!
        else b[o]!)) = ∑ t ∈ Finset.range (s.cin * s.kh * s.kw),
          rationalWord (windowAt s input y x t) * rationalWord w[o * (s.cin * s.kh * s.kw) + t]! := by
    apply Finset.sum_congr rfl
    intro t ht
    simp only [if_pos (Finset.mem_range.mp ht)]
  rw [hinner, Nat.mul_assoc, sum_blocks]
  apply Finset.sum_congr rfl
  intro c hc
  rw [sum_blocks]
  apply Finset.sum_congr rfl
  intro dy hdy
  apply Finset.sum_congr rfl
  intro dx hdx
  have ht : c * (s.kh * s.kw) + (dy * s.kw + dx) = (c * s.kh + dy) * s.kw + dx := by ring
  rw [ht, window_coordinates s input y x c dy dx (Finset.mem_range.mp hdy)
    (Finset.mem_range.mp hdx)]
  have hw : o * (s.cin * (s.kh * s.kw)) + ((c * s.kh + dy) * s.kw + dx) =
      ((o * s.cin + c) * s.kh + dy) * s.kw + dx := by ring
  rw [hw]

def depthwiseValue (s : ConvShape) (input w b : Array Word) (c y x : Nat) : ℚ :=
  (∑ dy ∈ Finset.range s.kh, ∑ dx ∈ Finset.range s.kw,
    rationalWord (paddedAt s input c ((y * s.stride + dy : Nat) - (s.pad : Int))
      ((x * s.stride + dx : Nat) - (s.pad : Int))) *
        rationalWord w[(c * s.kh + dy) * s.kw + dx]!) + rationalWord b[c]!

theorem depthwise_total (s : ConvShape) (input w b : Array Word) (c y x : Nat) :
    (∑ t ∈ Finset.range (s.kh * s.kw + 1),
      rationalWord (if t < s.kh * s.kw then windowAt s input y x (c * (s.kh * s.kw) + t)
        else oneW) *
      rationalWord (if t < s.kh * s.kw then w[c * (s.kh * s.kw) + t]! else b[c]!)) =
        depthwiseValue s input w b c y x := by
  rw [Finset.sum_range_succ]
  simp only [lt_self_iff_false, ite_false, rational_one, one_mul, depthwiseValue]
  congr 1
  have hinner : (∑ t ∈ Finset.range (s.kh * s.kw),
      rationalWord (if t < s.kh * s.kw then windowAt s input y x (c * (s.kh * s.kw) + t)
        else oneW) *
      rationalWord (if t < s.kh * s.kw then w[c * (s.kh * s.kw) + t]! else b[c]!)) =
        ∑ t ∈ Finset.range (s.kh * s.kw),
          rationalWord (windowAt s input y x (c * (s.kh * s.kw) + t)) *
            rationalWord w[c * (s.kh * s.kw) + t]! := by
    apply Finset.sum_congr rfl
    intro t ht
    simp only [if_pos (Finset.mem_range.mp ht)]
  rw [hinner, sum_blocks]
  apply Finset.sum_congr rfl
  intro dy hdy
  apply Finset.sum_congr rfl
  intro dx hdx
  have ht : c * (s.kh * s.kw) + (dy * s.kw + dx) = (c * s.kh + dy) * s.kw + dx := by ring
  rw [ht, window_coordinates s input y x c dy dx (Finset.mem_range.mp hdy)
    (Finset.mem_range.mp hdx)]

theorem pool_total (h w k ch y x : Nat) (input : Array Word) :
    (∑ t ∈ Finset.range (k * k), rationalWord (poolAt h w k input ch y x t)) =
      ∑ dy ∈ Finset.range k, ∑ dx ∈ Finset.range k,
        rationalWord input[(ch * h + y * k + dy) * w + x * k + dx]! := by
  rw [sum_blocks]
  apply Finset.sum_congr rfl
  intro dy hdy
  apply Finset.sum_congr rfl
  intro dx hdx
  rw [pool_coordinates h w k ch y x dy dx input (Finset.mem_range.mp hdy)
    (Finset.mem_range.mp hdx)]

theorem sum_total (x : Array Word) :
    exactDot x (Array.replicate x.size oneW) = ∑ j ∈ Finset.range x.size, rationalWord x[j]! := by
  unfold exactDot
  apply Finset.sum_congr rfl
  intro j hj
  rw [getElem!_pos (Array.replicate x.size oneW) j (by simpa using Finset.mem_range.mp hj)]
  simp [rational_one]

theorem model_roundtrip (z : Model Fmt) : ofW (toWord z) = z := by
  have h : z.toNatBits < 2 ^ 32 := Model.toNatBits_lt_two_pow z
  simp only [ofW, toWord, Model.ofUInt32, UInt32.toNat_ofNat', Nat.mod_eq_of_lt h]
  exact Model.ofNatBits_toNatBits z

/-- IEEE maximum is a mathematical maximum on non-NaN values, including infinities. -/
theorem maximum_fold_math (indices : List Nat) (f : Nat → Model Fmt) (initial : Model Fmt)
    (hf : ∀ i ∈ indices, Model.isNaN (f i) = false) (hi : Model.isNaN initial = false) :
    Model.toEReal (indices.foldl (fun acc i => Model.maximum acc (f i)) initial) =
      indices.foldl (fun acc i => max acc (Model.toEReal (f i))) (Model.toEReal initial) := by
  induction indices generalizing initial with
  | nil => rfl
  | cons i rest ih =>
    simp only [List.foldl_cons]
    rw [ih (Model.maximum initial (f i)) (by intro j hj; exact hf j (by simp [hj]))
      (Model.isNaN_maximum_eq_false_of_isNaN_eq_false _ _ hi (hf i (by simp)))]
    rw [Model.toEReal_maximum_eq_max initial (f i) hi (hf i (by simp))]

theorem maxpool_math (c h w k ch y x : Nat) (input : Array Word)
    (hc : ch < c) (hy : y < h / k) (hx : x < w / k) (hk : 0 < k)
    (hf : ∀ t, t < k * k → Model.isNaN (ofW (poolAt h w k input ch y x t)) = false) :
    Model.toEReal (ofW ((maxpoolSpec c h w k input)[(ch * (h / k) + y) * (w / k) + x]!)) =
      (List.range (k * k)).foldl (fun acc t =>
        max acc (Model.toEReal (ofW (poolAt h w k input ch y x t))))
        (Model.toEReal (ofW (poolAt h w k input ch y x 0))) := by
  rw [maxpool_entry c h w k ch y x input hc hy hx, model_roundtrip]
  exact maximum_fold_math _ _ _ (by intro t ht; exact hf t (by simpa using ht))
    (hf 0 (Nat.mul_pos hk hk))

end ExactRuntime.TensorMath
