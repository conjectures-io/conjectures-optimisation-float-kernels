import ExactToolkit.TensorProofs

/-!
# What an LSTM or GRU output means

The recurrent specs are flat loops over dot products. This file states what they compute in
mathematical terms, as `TensorProofs` does for the dense and convolution kernels:

* `lstm_entry`, `gru_entry`: output `(t, u)` is unit `u` of the hidden state after `t + 1` steps
  (`stateAfter`), from the zero state;
* `lstm_hidden`, `lstm_cell`, `gru_hidden`: one step, unit by unit;
* `gate_total`: a gate's dot product is the exact sum `Σ x·w_ih + Σ h·w_hh + b_ih + b_hh`;
  `gate_nonzero_math`: with finite operands and a nonzero sum, the gate is that rational rounded
  once;
* `cell_total`: the LSTM cell update's exact sum is `f·c + i·g`; `gru_update_total`: the GRU
  update's is `n − z·n + z·h`; `gru_candidate_total`: the GRU candidate's is
  `Σ x·w_in + b_in + r·hn`. Each is rounded once (`dotOf_nonzero_math`).

Together with `toReal_sigmoidModel` and `toReal_tanhModel` (`ActivationProof`) these describe a
step completely: every value is an exact rational expression of finite words, rounded once.
-/

namespace ExactRuntime.TensorMath

open FloatLib.Floats.Formats.BinaryInterchange FloatLib.Floats

/-! ## The sign flip -/

private theorem testBit_top (i : Nat) : (2147483648 : Nat).testBit i = decide (i = 31) := by
  rw [show (2147483648 : Nat) = 2 ^ 31 by norm_num, Nat.testBit_two_pow]
  by_cases h : i = 31 <;> simp [h, eq_comm]

/-- `n &&& 2^31` keeps only bit 31. -/
private theorem and_top (n : Nat) :
    n &&& 2147483648 = if n.testBit 31 then 2147483648 else 0 := by
  apply Nat.eq_of_testBit_eq
  intro i
  rw [Nat.testBit_and, testBit_top]
  by_cases hi : i = 31
  · subst hi
    split <;> rename_i h <;> simp [h, testBit_top]
  · split <;> simp [hi, testBit_top]

private theorem consts : (0x80000000 : UInt32).toNat = 2147483648 ∧ (0xFF : UInt32).toNat = 255 ∧
    (23 : UInt32).toNat = 23 ∧ (0x7FFFFF : UInt32).toNat = 8388607 := ⟨rfl, rfl, rfl, rfl⟩

theorem expOf_negW (w : Word) : expOf (negW w) = expOf w := by
  apply UInt32.toNat_inj.mp
  simp only [expOf, negW, UInt32.toNat_and, UInt32.toNat_shiftRight, UInt32.toNat_xor,
    consts.1, consts.2.1, consts.2.2.1, Nat.reduceMod]
  apply Nat.eq_of_testBit_eq
  intro i
  simp only [Nat.testBit_and, Nat.testBit_shiftRight, Nat.testBit_xor]
  by_cases hi : i < 8
  · have : ¬(23 + i = 31) := by omega
    rw [testBit_top (23 + i)]
    simp [this]
  · have h255 : (255 : Nat).testBit i = false := by
      rw [show (255 : Nat) = 2 ^ 8 - 1 by norm_num, Nat.testBit_two_pow_sub_one]
      simp; omega
    simp [h255]

theorem fracOf_negW (w : Word) : fracOf (negW w) = fracOf w := by
  apply UInt32.toNat_inj.mp
  simp only [fracOf, negW, UInt32.toNat_and, UInt32.toNat_xor, consts.1, consts.2.2.2]
  apply Nat.eq_of_testBit_eq
  intro i
  simp only [Nat.testBit_and, Nat.testBit_xor]
  by_cases hi : i < 23
  · have : ¬(i = 31) := by omega
    rw [testBit_top i]
    simp [this]
  · have hm : (8388607 : Nat).testBit i = false := by
      rw [show (8388607 : Nat) = 2 ^ 23 - 1 by norm_num, Nat.testBit_two_pow_sub_one]
      simp; omega
    simp [hm]

/-- The sign is bit 31. -/
private theorem signOf_eq (w : Word) : signOf w = w.toNat.testBit 31 := by
  unfold signOf
  have h := and_top w.toNat
  rw [← show (0x80000000 : UInt32).toNat = 2147483648 from rfl, ← UInt32.toNat_and] at h
  by_cases hb : w.toNat.testBit 31
  · rw [hb, if_pos rfl] at h
    have hne : w &&& 0x80000000 ≠ 0 := fun h0 => by simp [h0] at h
    simp [hb, hne]
  · rw [Bool.not_eq_true] at hb
    rw [hb] at h
    have h0 : w &&& 0x80000000 = 0 := UInt32.toNat_inj.mp (by simpa using h)
    simp [hb, h0]

theorem signOf_negW (w : Word) : signOf (negW w) = !signOf w := by
  rw [signOf_eq, signOf_eq, negW, UInt32.toNat_xor, Nat.testBit_xor]
  simp [show (0x80000000 : UInt32).toNat = 2147483648 from rfl, testBit_top]

/-- Flipping the sign bit negates the exact value. -/
theorem rational_negW (w : Word) : rationalWord (negW w) = -rationalWord w := by
  unfold rationalWord decodeD
  rw [expOf_negW, fracOf_negW, signOf_negW]
  split_ifs <;> cases signOf w <;>
    simp [FloatLib.Numerics.Dyadic.toRat, FloatLib.Numerics.Dyadic.signedSignificand] <;> ring

theorem expOf_oneW_ne : expOf oneW ≠ 255 := by
  intro h
  have h' := congrArg UInt32.toNat h
  norm_num [expOf, oneW, UInt32.toNat_and, UInt32.toNat_shiftRight] at h'

/-! ## The recurrence -/

/-- The state after `n` steps of `step` from `init`. -/
def stateAfter (step : Nat → Array Word × Array Word → Array Word × Array Word)
    (init : Array Word × Array Word) : Nat → Array Word × Array Word
  | 0 => init
  | n + 1 => step n (stateAfter step init n)

/-- `recurWith`'s loop keeps the state after `n` steps and every hidden state on the way. -/
theorem recur_fold (step : Nat → Array Word × Array Word → Array Word × Array Word)
    (init : Array Word × Array Word) (n : Nat) :
    (List.range n).foldl (fun (acc : (Array Word × Array Word) × Array (Array Word)) t =>
        let st := step t acc.1
        (st, acc.2.push st.1)) (init, #[]) =
      (stateAfter step init n,
        Array.ofFn (n := n) fun i => (stateAfter step init (i.val + 1)).1) := by
  induction n with
  | zero => simp [stateAfter]
  | succ n ih =>
    rw [List.range_succ, List.foldl_append, ih]
    simp [stateAfter, Array.ofFn_succ]

/-- Output `(t, u)` of a recurrence is unit `u` of the hidden state after `t + 1` steps. -/
theorem recurWith_entry (T H : Nat) (step : Nat → Array Word × Array Word → Array Word × Array Word)
    (init : Array Word × Array Word) (t u : Nat) (ht : t < T) (hu : u < H) :
    (recurWith T H step init)[t * H + u]! = (stateAfter step init (t + 1)).1[u]! := by
  have hflat := flat_lt t u T H ht hu
  have hpair := flatten_pair t u H (by omega) hu
  unfold recurWith
  rw [recur_fold, ofFn_get _ _ _ hflat]
  simp only [hpair.1, hpair.2]
  have hin : (Array.ofFn (n := T) fun i => (stateAfter step init (i.val + 1)).1)[t]! =
      (stateAfter step init (t + 1)).1 := by
    rw [getElem!_pos _ _ (by simpa using ht), Array.getElem_ofFn]
  rw [hin]

theorem lstm_entry (T inW H : Nat) (x wih whh bih bhh : Array Word) (t u : Nat)
    (ht : t < T) (hu : u < H) :
    (lstmSpec T inW H x wih whh bih bhh)[t * H + u]! =
      (stateAfter (lstmStepWith dotOf sigmoidW tanhW inW H x wih whh bih bhh)
        (Array.replicate H 0, Array.replicate H 0) (t + 1)).1[u]! :=
  recurWith_entry T H _ _ t u ht hu

theorem gru_entry (T inW H : Nat) (x wih whh bih bhh : Array Word) (t u : Nat)
    (ht : t < T) (hu : u < H) :
    (gruSpec T inW H x wih whh bih bhh)[t * H + u]! =
      (stateAfter (gruStepWith dotOf sigmoidW tanhW inW H x wih whh bih bhh)
        (Array.replicate H 0, #[]) (t + 1)).1[u]! :=
  recurWith_entry T H _ _ t u ht hu

/-! ## One step -/

section Step
variable (dot : DotFn) (sig tanh : Word → Word) (inW H : Nat) (x wih whh bih bhh : Array Word)
  (t : Nat) (st : Array Word × Array Word)

/-- Gate row `k` of step `t` on hidden state `h`. -/
abbrev gate (h : Array Word) (k : Nat) : Word := gateWith dot inW H x wih whh bih bhh h t k

/-- The LSTM cell: `R(f·c + i·g)` with `f = σ(a_f)`, `i = σ(a_i)`, `g = tanh(a_g)`. -/
theorem lstm_cell (u : Nat) (hu : u < H) :
    (lstmStepWith dot sig tanh inW H x wih whh bih bhh t st).2[u]! =
      dot 2 (fun j => if j = 0 then sig (gate dot inW H x wih whh bih bhh t st.1 (H + u))
          else sig (gate dot inW H x wih whh bih bhh t st.1 u))
        (fun j => if j = 0 then st.2[u]!
          else tanh (gate dot inW H x wih whh bih bhh t st.1 (2 * H + u))) := by
  simp only [lstmStepWith]
  rw [ofFn_get _ _ _ hu]
  simp only [ofFn_get _ _ _ (show H + u < 4 * H by omega), ofFn_get _ _ _ (show u < 4 * H by omega),
    ofFn_get _ _ _ (show 2 * H + u < 4 * H by omega)]

/-- The LSTM hidden state: `R(o · tanh(c'))`. -/
theorem lstm_hidden (u : Nat) (hu : u < H) :
    (lstmStepWith dot sig tanh inW H x wih whh bih bhh t st).1[u]! =
      toWord (Model.mul (ofW (sig (gate dot inW H x wih whh bih bhh t st.1 (3 * H + u))))
        (ofW (tanh (lstmStepWith dot sig tanh inW H x wih whh bih bhh t st).2[u]!))) := by
  simp only [lstmStepWith]
  rw [ofFn_get _ _ _ hu, ofFn_get _ _ _ hu,
    ofFn_get _ _ _ (show 3 * H + u < 4 * H by omega)]

/-- The GRU hidden state: `R(n − z·n + z·h)` with `z = σ(a_z)` and the candidate
`n = tanh(R(Σ x·w_in + b_in + r·hn))`, `r = σ(a_r)`, `hn = R(Σ h·w_hn + b_hn)`. -/
theorem gru_hidden (u : Nat) (hu : u < H) :
    let h := st.1
    let r := sig (gate dot inW H x wih whh bih bhh t h u)
    let hn := dot (H + 1) (fun j => if j < H then h[j]! else oneW)
      (fun j => if j < H then whh[(2 * H + u) * H + j]! else bhh[2 * H + u]!)
    (gruStepWith dot sig tanh inW H x wih whh bih bhh t st).1[u]! =
      dot 3 (fun j => if j = 2 then h[u]! else tanh (dot (inW + 2)
          (fun j => if j < inW then x[t * inW + j]! else if j = inW then oneW else hn)
          (fun j => if j < inW then wih[(2 * H + u) * inW + j]!
            else if j = inW then bih[2 * H + u]! else r)))
        (fun j => if j = 0 then oneW
          else if j = 1 then negW (sig (gate dot inW H x wih whh bih bhh t h (H + u)))
          else sig (gate dot inW H x wih whh bih bhh t h (H + u))) := by
  simp only [gruStepWith]
  rw [ofFn_get _ _ _ hu]
  simp only [ofFn_get _ _ _ hu]

end Step

/-! ## Exact sums -/

/-- A gate's exact value: the input row against `w_ih`'s row, the hidden state against `w_hh`'s
row, and both biases. -/
def gateValue (inW H : Nat) (x wih whh bih bhh h : Array Word) (t k : Nat) : ℚ :=
  (∑ j ∈ Finset.range inW, rationalWord x[t * inW + j]! * rationalWord wih[k * inW + j]!) +
  (∑ j ∈ Finset.range H, rationalWord h[j]! * rationalWord whh[k * H + j]!) +
  rationalWord bih[k]! + rationalWord bhh[k]!

/-- The gate's dot product sums exactly `gateValue`. -/
theorem gate_total (inW H : Nat) (x wih whh bih bhh h : Array Word) (t k : Nat) :
    (∑ j ∈ Finset.range (inW + H + 2),
      rationalWord (if j < inW then x[t * inW + j]! else if j < inW + H then h[j - inW]! else oneW) *
      rationalWord (if j < inW then wih[k * inW + j]!
        else if j < inW + H then whh[k * H + (j - inW)]!
        else if j = inW + H then bih[k]! else bhh[k]!)) =
      gateValue inW H x wih whh bih bhh h t k := by
  rw [show inW + H + 2 = inW + H + 1 + 1 by omega, Finset.sum_range_succ, Finset.sum_range_succ,
    Finset.sum_range_add]
  have h1 : (∑ j ∈ Finset.range inW,
      rationalWord (if j < inW then x[t * inW + j]! else if j < inW + H then h[j - inW]! else oneW) *
      rationalWord (if j < inW then wih[k * inW + j]!
        else if j < inW + H then whh[k * H + (j - inW)]!
        else if j = inW + H then bih[k]! else bhh[k]!)) =
      ∑ j ∈ Finset.range inW, rationalWord x[t * inW + j]! * rationalWord wih[k * inW + j]! :=
    Finset.sum_congr rfl fun j hj => by simp [Finset.mem_range.mp hj]
  have h2 : (∑ j ∈ Finset.range H,
      rationalWord (if inW + j < inW then x[t * inW + (inW + j)]!
        else if inW + j < inW + H then h[inW + j - inW]! else oneW) *
      rationalWord (if inW + j < inW then wih[k * inW + (inW + j)]!
        else if inW + j < inW + H then whh[k * H + (inW + j - inW)]!
        else if inW + j = inW + H then bih[k]! else bhh[k]!)) =
      ∑ j ∈ Finset.range H, rationalWord h[j]! * rationalWord whh[k * H + j]! :=
    Finset.sum_congr rfl fun j hj => by
      have := Finset.mem_range.mp hj
      simp [show inW + j < inW + H by omega]
  rw [h1, h2]
  simp only [show ¬(inW + H < inW) by omega, show ¬(inW + H < inW + H) by omega,
    show ¬(inW + H + 1 < inW) by omega, show ¬(inW + H + 1 < inW + H) by omega,
    show inW + H + 1 ≠ inW + H by omega, ite_false, ite_true, rational_one, one_mul, gateValue]

/-- With finite operands and a nonzero exact value, a gate is that value rounded once. -/
theorem gate_nonzero_math (inW H : Nat) (x wih whh bih bhh h : Array Word) (t k : Nat)
    (hx : ∀ j, j < inW → expOf x[t * inW + j]! ≠ 255)
    (hh : ∀ j, j < H → expOf h[j]! ≠ 255)
    (hwi : ∀ j, j < inW → expOf wih[k * inW + j]! ≠ 255)
    (hwh : ∀ j, j < H → expOf whh[k * H + j]! ≠ 255)
    (hbi : expOf bih[k]! ≠ 255) (hbh : expOf bhh[k]! ≠ 255)
    (hn : gateValue inW H x wih whh bih bhh h t k ≠ 0) :
    ∃ d : FloatLib.Numerics.Dyadic, d.toRat = gateValue inW H x wih whh bih bhh h t k ∧
      gateWith dotOf inW H x wih whh bih bhh h t k = toWord (Model.roundDyadic Fmt d) := by
  let fa := fun j => if j < inW then x[t * inW + j]! else if j < inW + H then h[j - inW]! else oneW
  let fb := fun j => if j < inW then wih[k * inW + j]!
    else if j < inW + H then whh[k * H + (j - inW)]!
    else if j = inW + H then bih[k]! else bhh[k]!
  have hf : ∀ j, j < inW + H + 2 → expOf (fa j) ≠ 255 ∧ expOf (fb j) ≠ 255 := by
    intro j hj
    by_cases h1 : j < inW
    · simp only [fa, fb, h1, ite_true]
      exact ⟨hx j h1, hwi j h1⟩
    · by_cases h2 : j < inW + H
      · simp only [fa, fb, h1, h2, ite_true, ite_false]
        exact ⟨hh _ (by omega), hwh _ (by omega)⟩
      · by_cases h3 : j = inW + H
        · subst h3
          simp only [fa, fb, show ¬(inW + H < inW) by omega,
            show ¬(inW + H < inW + H) by omega, ite_true, ite_false]
          exact ⟨expOf_oneW_ne, hbi⟩
        · simp only [fa, fb, h1, h2, h3, ite_false]
          exact ⟨expOf_oneW_ne, hbh⟩
  obtain ⟨d, hd, hw⟩ := dotOf_nonzero_math (inW + H + 2) fa fb hf
    (by simpa only [fa, fb, gate_total] using hn)
  exact ⟨d, hd.trans (gate_total inW H x wih whh bih bhh h t k), hw⟩

/-- The LSTM cell update's exact value is `f·c + i·g`. -/
theorem cell_total (f i c g : Word) :
    (∑ j ∈ Finset.range 2, rationalWord (if j = 0 then f else i) *
      rationalWord (if j = 0 then c else g)) =
      rationalWord f * rationalWord c + rationalWord i * rationalWord g := by
  simp [Finset.sum_range_succ]

/-- The GRU update's exact value is `n − z·n + z·h`. -/
theorem gru_update_total (n z h : Word) :
    (∑ j ∈ Finset.range 3, rationalWord (if j = 2 then h else n) *
      rationalWord (if j = 0 then oneW else if j = 1 then negW z else z)) =
      rationalWord n - rationalWord z * rationalWord n + rationalWord z * rationalWord h := by
  simp [Finset.sum_range_succ, rational_one, rational_negW]
  ring

/-- The GRU candidate's exact value is `Σ x·w_in + b_in + r·hn`. -/
theorem gru_candidate_total (inW : Nat) (x wih bih : Array Word) (hn r : Word) (t row : Nat) :
    (∑ j ∈ Finset.range (inW + 2),
      rationalWord (if j < inW then x[t * inW + j]! else if j = inW then oneW else hn) *
      rationalWord (if j < inW then wih[row * inW + j]! else if j = inW then bih[row]! else r)) =
      (∑ j ∈ Finset.range inW, rationalWord x[t * inW + j]! * rationalWord wih[row * inW + j]!) +
        rationalWord bih[row]! + rationalWord r * rationalWord hn := by
  rw [show inW + 2 = inW + 1 + 1 by omega, Finset.sum_range_succ, Finset.sum_range_succ]
  have h1 : (∑ j ∈ Finset.range inW,
      rationalWord (if j < inW then x[t * inW + j]! else if j = inW then oneW else hn) *
      rationalWord (if j < inW then wih[row * inW + j]! else if j = inW then bih[row]! else r)) =
      ∑ j ∈ Finset.range inW, rationalWord x[t * inW + j]! * rationalWord wih[row * inW + j]! :=
    Finset.sum_congr rfl fun j hj => by simp [Finset.mem_range.mp hj]
  rw [h1]
  simp [rational_one]
  ring

end ExactRuntime.TensorMath
