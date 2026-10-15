import ContractExact

/-!
# Exact runtime, `slowdepthwise` fixture

The champion with `depthwise` left as FloatLib's spec: proved and correct, but about 3.5x the
champion's time on the MobileNet-style model, so the regression guard must reject it at the score
stage.
-/

namespace Submission

open ExactRuntime FloatLib.Floats.Formats.BinaryInterchange FloatLib.Floats

/-! ## The exact accumulator -/

/-- Positive and negative limbs, "a NaN or infinity was seen", and "every term so far was a zero
of negative sign" (which decides the sign of a zero result). -/
structure Acc where
  pos : Array Nat
  neg : Array Nat
  special : Bool
  negZero : Bool

def Acc.empty : Acc := ⟨Array.replicate 18 0, Array.replicate 18 0, false, true⟩

/-- The exact integer total held in the limbs. -/
def Acc.total (acc : Acc) : Int := (limbVal acc.pos.toList : Int) - limbVal acc.neg.toList

/-- Add the exact term `±m · 2^idx`; a zero term only updates the zero-sign flag. -/
@[inline] def Acc.add (acc : Acc) (m idx : Nat) (neg : Bool) : Acc :=
  match acc with
  | ⟨pos, ng, sp, nz⟩ =>
    if m = 0 then ⟨pos, ng, sp, nz && neg⟩
    else if neg then ⟨pos, addLimbs64 ng m.toUInt64 idx, sp, false⟩
    else ⟨addLimbs64 pos m.toUInt64 idx, ng, sp, false⟩

@[inline] def Acc.markSpecial (acc : Acc) : Acc :=
  match acc with
  | ⟨pos, ng, _, nz⟩ => ⟨pos, ng, true, nz⟩

/-- Everything the proof needs to know about one `Acc.add`. -/
theorem Acc.add_spec (acc : Acc) (m idx : Nat) (neg : Bool)
    (hp : acc.pos.size = 18) (hn : acc.neg.size = 18) (hm : m < 2 ^ 64) (hi : idx / 32 + 1 < 18) :
    (acc.add m idx neg).pos.size = 18 ∧ (acc.add m idx neg).neg.size = 18 ∧
    (acc.add m idx neg).special = acc.special ∧
    (acc.add m idx neg).negZero = (acc.negZero && (decide (m = 0) && neg)) ∧
    (acc.add m idx neg).total = acc.total + (if neg then -(m : Int) else m) * 2 ^ idx := by
  obtain ⟨pos, ng, sp, nz⟩ := acc
  simp only at hp hn
  by_cases hm0 : m = 0
  · simp [Acc.add, Acc.total, hm0, hp, hn]
  · cases neg
    · refine ⟨by simp [Acc.add, hm0, addLimbs64_size, hp], by simp [Acc.add, hm0, hn],
        by simp [Acc.add, hm0], by simp [Acc.add, hm0], ?_⟩
      simp only [Acc.add, hm0, ite_false, Bool.false_eq_true, addLimbs64_eq _ _ _ hm, Acc.total]
      rw [addLimbs_val _ _ _ (by omega)]
      push_cast; ring
    · refine ⟨by simp [Acc.add, hm0, hp], by simp [Acc.add, hm0, addLimbs64_size, hn],
        by simp [Acc.add, hm0], by simp [Acc.add, hm0], ?_⟩
      simp only [Acc.add, hm0, ite_false, ite_true, addLimbs64_eq _ _ _ hm, Acc.total]
      rw [addLimbs_val _ _ _ (by omega)]
      push_cast; ring

theorem Acc.total_empty : Acc.empty.total = 0 := by decide

/-! ## The exact dot product -/

@[inline] def dotStep (acc : Acc) (wa wb : UInt32) : Acc :=
  if expOf wa = 255 ∨ expOf wb = 255 then acc.markSpecial
  else acc.add (mantOf wa * mantOf wb) (idxOf wa + idxOf wb) (Bool.xor (signOf wa) (signOf wb))

/-- The loop has FloatLib's `dotStateLoop` shape; the accumulator's fields are plain arguments so
the compiled loop allocates nothing per term. -/
@[specialize] def dotLoop (fa fb : Nat → UInt32) (pos neg : Array Nat) (sp nz : Bool)
    (index : Nat) : Nat → Acc
  | 0 => ⟨pos, neg, sp, nz⟩
  | count + 1 =>
    match dotStep ⟨pos, neg, sp, nz⟩ (fa index) (fb index) with
    | ⟨p, n, s, z⟩ => dotLoop fa fb p n s z (index + 1) count

/-- The exact dot product of `[fa 0, …, fa (n-1)]` and `[fb 0, …, fb (n-1)]`. -/
@[inline] def dotFn (n : Nat) (fa fb : Nat → UInt32) : UInt32 :=
  let acc := dotLoop fa fb (Array.replicate 18 0) (Array.replicate 18 0) false true 0 n
  if acc.special then
    dotSpec (Array.ofFn (n := n) fun i => fa i) (Array.ofFn (n := n) fun i => fb i)
  else finishLimbs acc.pos acc.neg (-298) (n ≠ 0 && acc.negZero)

theorem sv_mul (wa wb : UInt32) :
    (if Bool.xor (signOf wa) (signOf wb) then -((mantOf wa * mantOf wb : Nat) : Int)
      else ((mantOf wa * mantOf wb : Nat) : Int)) = sv wa * sv wb := by
  unfold sv; cases signOf wa <;> cases signOf wb <;> simp

/-- A NaN or infinity, once seen, stays recorded. -/
theorem dotLoop_special (fa fb : Nat → UInt32) (pos neg : Array Nat) (nz : Bool)
    (index count : Nat) : (dotLoop fa fb pos neg true nz index count).special = true := by
  induction count generalizing pos neg nz index with
  | zero => rfl
  | succ count ih =>
    rw [dotLoop]
    have hs : (dotStep ⟨pos, neg, true, nz⟩ (fa index) (fb index)).special = true := by
      unfold dotStep Acc.markSpecial Acc.add; split_ifs <;> rfl
    generalize dotStep ⟨pos, neg, true, nz⟩ (fa index) (fb index) = r at hs ⊢
    obtain ⟨p, q, s, z⟩ := r
    simp only at hs; subst hs
    exact ih ..

/-- What a loop without specials tells us, stated over arrays `A`, `B` that hold the operands (as
the toolkit's FloatLib lemmas are). -/
theorem dotLoopFacts (fa fb : Nat → UInt32) (A B : Array UInt32) (n : Nat)
    (hA : ∀ k, k < n → A[k]! = fa k) (hB : ∀ k, k < n → B[k]! = fb k)
    (acc : Acc) (index remaining : Nat) (h : index + remaining ≤ n)
    (hp : acc.pos.size = 18) (hn : acc.neg.size = 18)
    (hfin : (dotLoop fa fb acc.pos acc.neg acc.special acc.negZero index remaining).special
      = false) :
    acc.special = false ∧
    (∀ k, index ≤ k → k < index + remaining → expOf A[k]! ≠ 255 ∧ expOf B[k]! ≠ 255) ∧
    (dotLoop fa fb acc.pos acc.neg acc.special acc.negZero index remaining).total =
      acc.total + dsum A B index remaining ∧
    (dotLoop fa fb acc.pos acc.neg acc.special acc.negZero index remaining).negZero =
      (acc.negZero && dallNZ A B index remaining) := by
  induction remaining generalizing acc index with
  | zero => obtain ⟨p, n, s, z⟩ := acc; simp_all [dotLoop, dsum, dallNZ]
  | succ count ih =>
    obtain ⟨p, q, s, z⟩ := acc
    simp only at hp hn
    have ha := hA index (by omega)
    have hb := hB index (by omega)
    by_cases he : expOf (fa index) = 255 ∨ expOf (fb index) = 255
    · exfalso
      rw [dotLoop] at hfin
      have hs : (dotStep ⟨p, q, s, z⟩ (fa index) (fb index)).special = true := by
        simp [dotStep, he, Acc.markSpecial]
      generalize dotStep ⟨p, q, s, z⟩ (fa index) (fb index) = r at hs hfin
      obtain ⟨p', q', s', z'⟩ := r
      simp only at hs; subst hs
      rw [dotLoop_special] at hfin
      exact Bool.noConfusion hfin
    · have he' := not_or.mp he
      obtain ⟨h1, h2, h3, h4, h5⟩ :=
        Acc.add_spec ⟨p, q, s, z⟩ (mantOf (fa index) * mantOf (fb index))
          (idxOf (fa index) + idxOf (fb index)) (Bool.xor (signOf (fa index)) (signOf (fb index)))
          hp hn (mul_mant_lt _ _)
          (by have := idxOf_le _ he'.1; have := idxOf_le _ he'.2; omega)
      have hstep : dotStep ⟨p, q, s, z⟩ (fa index) (fb index) =
          Acc.add ⟨p, q, s, z⟩ (mantOf (fa index) * mantOf (fb index))
            (idxOf (fa index) + idxOf (fb index))
            (Bool.xor (signOf (fa index)) (signOf (fb index))) := by
        simp [dotStep, he]
      rw [dotLoop, hstep] at hfin ⊢
      obtain ⟨hs, hall, hD, hz⟩ := ih _ (index + 1) (by omega) h1 h2 hfin
      refine ⟨by rw [← h3, hs], ?_, ?_, ?_⟩
      · intro k k1 k2
        rcases Nat.lt_or_ge k (index + 1) with hk | hk
        · have : k = index := by omega
          subst this; rw [ha, hb]; exact he'
        · exact hall k hk (by omega)
      · rw [hD, h5, dsum, ha, hb, sv_mul]; ring
      · rw [hz, h4, dallNZ, ha, hb]; simp [Bool.and_assoc]

theorem getElem!_ofFn (n : Nat) (f : Nat → UInt32) (k : Nat) (hk : k < n) :
    (Array.ofFn (n := n) fun i => f i.val)[k]! = f k := by
  rw [getElem!_pos _ k (by simpa using hk), Array.getElem_ofFn]

theorem dotFn_eq (n : Nat) (fa fb : Nat → UInt32) :
    dotFn n fa fb =
      dotSpec (Array.ofFn (n := n) fun i => fa i) (Array.ofFn (n := n) fun i => fb i) := by
  unfold dotFn
  set A := Array.ofFn (n := n) fun i => fa i with hA
  set B := Array.ofFn (n := n) fun i => fb i with hB
  set acc := dotLoop fa fb (Array.replicate 18 0) (Array.replicate 18 0) false true 0 n with hacc
  by_cases hsp : acc.special = true
  · simp [hsp]
  · have hsp' : acc.special = false := by simpa using hsp
    obtain ⟨-, hall, hD, hz⟩ := dotLoopFacts fa fb A B n (getElem!_ofFn n fa)
      (getElem!_ofFn n fb) Acc.empty 0 n (by omega) (by simp [Acc.empty]) (by simp [Acc.empty])
      hsp'
    have hD : acc.total = dsum A B 0 n := by
      have := hD; simp only [Acc.total_empty, Int.zero_add] at this; exact this
    have hz : acc.negZero = dallNZ A B 0 n := by
      have := hz; simp only [Acc.empty, Bool.true_and] at this; exact this
    simp only [hsp', Bool.false_eq_true, ite_false]
    rw [finishLimbs_eq, show ((limbVal acc.pos.toList : Int) - limbVal acc.neg.toList) = acc.total
      from rfl]
    -- FloatLib's side: every word is finite, and its exact total is ours.
    have hAsize : A.size = n := by simp [hA]
    have hBsize : B.size = n := by simp [hB]
    set L := A.map (Model.ofUInt32 (fmt := Fmt)) with hL
    set R := B.map (Model.ofUInt32 (fmt := Fmt)) with hR
    have hLR : L.size = R.size := by simp [hL, hR, hAsize, hBsize]
    have hLsize : L.size = n := by simp [hL, hAsize]
    have hLfin : ∀ k (hk : k < L.size), Model.isFinite L[k] = true := by
      intro k hk
      have hk' : k < A.size := by simpa [hL] using hk
      have := (hall k (by omega) (by omega)).1
      rw [getElem!_pos A k hk'] at this
      simp [hL, isFinite_ofUInt32, this]
    have hRfin : ∀ k (hk : k < R.size), Model.isFinite R[k] = true := by
      intro k hk
      have hk' : k < B.size := by simpa [hR] using hk
      have := (hall k (by omega) (by omega)).2
      rw [getElem!_pos B k hk'] at this
      simp [hR, isFinite_ofUInt32, this]
    have hslice : Model.Reduction.Semantics.finiteDotSlice L R 0 L.size (by simp)
        (by simp [hLR]) = (acc.total : ℚ) * 2 ^ (-298 : ℤ) := by
      have := finiteDotSlice_eq A B 0 L.size (by simp [hL]) (by simp [hL, hAsize, hBsize])
        (by intro k k1 k2; exact hall k k1 (by omega))
      exact this.trans (by rw [hLsize, hD])
    have hexact := Model.Reduction.Internal.dotState_exact_toRat
      (destination := Fmt) L R hLR hLfin hRfin
    unfold dotSpec Model.dot
    by_cases hS0 : acc.total = 0
    · have hz0 : (Model.Reduction.Internal.dotState L R hLR :
          Model.Reduction.Internal.State Fmt).exact.significand = 0 := by
        rw [← FloatLib.Numerics.Dyadic.toRat_eq_zero_iff, hexact, hslice, hS0]; simp
      rw [Model.Reduction.dotWithStatus_eq_zero_of_finite Fmt L R .nearestEven hLR hLfin hRfin
        hz0, hS0, finishWord_zero]
      unfold Model.Reduction.Internal.dotState
      obtain ⟨h1, h2⟩ := dotStateLoop_flags A B {} 0 L.size (by simp [hL])
        (by simp [hL, hAsize, hBsize]) (by intro k k1 k2; exact hall k k1 (by omega))
      have h1' : (Model.Reduction.Internal.dotStateLoop L R
          ({} : Model.Reduction.Internal.State Fmt) 0 L.size (by simp)
          (by simp [hLR])).sawFiniteTerm = decide (L.size ≠ 0) := by
        rw [show decide (L.size ≠ 0) = (false || decide (L.size ≠ 0)) by simp]; exact h1
      have h2' : (Model.Reduction.Internal.dotStateLoop L R
          ({} : Model.Reduction.Internal.State Fmt) 0 L.size (by simp)
          (by simp [hLR])).allTermsNegativeZero = dallNZ A B 0 L.size := by
        rw [show dallNZ A B 0 L.size = (true && dallNZ A B 0 L.size) by simp]; exact h2
      simp only [Except.map, h1', h2']
      rw [hz]
      simp [hLsize]
    · have hnz : Model.Reduction.Semantics.finiteDot L.toList R.toList ≠ .ok 0 := by
        rw [Model.Reduction.Semantics.finiteDot_toList_eq_slice L R hLR]
        intro h
        injection h with h
        rw [hslice] at h
        simp at h; exact hS0 h
      rw [Model.Reduction.dotWithStatus_eq_round_of_finite_nonzero Fmt L R .nearestEven hLR hLfin
        hRfin hnz]
      simp only [Except.map]
      exact finishWord_of_ne _ _ _ _ hS0 (by rw [hexact, hslice])

/-! ## Arrays as functions -/

theorem ofFn_getElem! (a : Array UInt32) (n : Nat) (h : n = a.size) :
    (Array.ofFn (n := n) fun i => a[i.val]!) = a := by
  subst h
  apply Array.ext (by simp)
  intro i h1 h2
  rw [Array.getElem_ofFn, getElem!_pos a i h2]

theorem ofFn_const (n : Nat) (c : UInt32) :
    (Array.ofFn (n := n) fun _ => c) = Array.replicate n c := by
  apply Array.ext (by simp)
  intro i h1 h2
  simp

/-! ## The kernels -/

def dotX (a b : Array UInt32) : UInt32 :=
  if a.size = b.size then dotFn a.size (fun i => a[i]!) (fun i => b[i]!) else dotSpec a b

def sumX (x : Array UInt32) : UInt32 := dotFn x.size (fun i => x[i]!) (fun _ => oneW)

theorem dotX_eq_spec (a b : Array UInt32) : dotX a b = dotSpec a b := by
  unfold dotX
  split
  · rename_i h
    rw [dotFn_eq, ofFn_getElem! a _ rfl, ofFn_getElem! b _ h]
  · rfl

theorem sumX_eq_spec (x : Array UInt32) : sumX x = sumSpec x := by
  rw [sumX, dotFn_eq, ofFn_getElem! x _ rfl, ofFn_const, sumSpec]

/-- `dotFn` is the spec's dot product, so every `…With` definition run with `dotFn` is its spec. -/
theorem dotFn_eq_dotOf : dotFn = dotOf :=
  funext fun n => funext fun fa => funext fun fb => dotFn_eq n fa fb

/-! ## Max pooling on words -/

theorem toWord_ofW (w : Word) : toWord (ofW w) = w := by
  apply UInt32.toNat_inj.mp
  simp only [toWord, ofW, Model.toNatBits, ofUInt32_bits, UInt32.toNat_ofNat']
  exact Nat.mod_eq_of_lt w.toBitVec.isLt

theorem ofW_toWord (z : Model Fmt) : ofW (toWord z) = z := by
  have h : z.toNatBits < 2 ^ 32 := Model.toNatBits_lt_two_pow z
  simp only [ofW, toWord, Model.ofUInt32, UInt32.toNat_ofNat', Nat.mod_eq_of_lt h]
  exact Model.ofNatBits_toNatBits z

/-- A finite word's exact value times `2^149`: `±mant · 2^idx`. -/
@[inline] def keyOf (w : Word) : Int :=
  if signOf w then -((mantOf w <<< idxOf w : Nat) : Int) else ((mantOf w <<< idxOf w : Nat) : Int)

theorem keyOf_eq (w : Word) : keyOf w = sv w * 2 ^ idxOf w := by
  unfold keyOf sv; split <;> simp [Nat.shiftLeft_eq]

theorem toReal_decodeD (w : Word) :
    (decodeD w).toReal = (keyOf w : ℝ) * 2 ^ (-149 : Int) := by
  rw [← FloatLib.Numerics.Dyadic.cast_toRat, decodeD_toRat, keyOf_eq]
  push_cast
  rw [zpow_sub₀ two_ne_zero, zpow_natCast]
  ring

/-- FloatLib's `maximum` on words: finite words compared by `keyOf`; the spec's own steps when
the values are equal (signed zeros) or a NaN or infinity is involved. -/
@[inline] def maxW (a b : Word) : Word :=
  if expOf a = 255 ∨ expOf b = 255 then toWord (Model.maximum (ofW a) (ofW b))
  else match compare (keyOf a) (keyOf b) with
    | .lt => b
    | .gt => a
    | .eq => toWord (if Model.isZero (ofW a) && Model.isZero (ofW b) then
        Model.zero Fmt (Model.signBit (ofW a) && Model.signBit (ofW b)) else ofW a)

theorem cmp_decodeD (a b : Word) :
    Model.cmpDyadic (decodeD a) (decodeD b) = compare (keyOf a) (keyOf b) := by
  have hs : (0 : ℝ) < 2 ^ (-149 : Int) := zpow_pos two_pos _
  cases h : compare (keyOf a) (keyOf b)
  · rw [Model.cmpDyadic_lt_iff, toReal_decodeD, toReal_decodeD]
    exact mul_lt_mul_of_pos_right (by exact_mod_cast compare_lt_iff_lt.mp h) hs
  · rw [Model.cmpDyadic_eq_iff, toReal_decodeD, toReal_decodeD, compare_eq_iff_eq.mp h]
  · rw [Model.cmpDyadic_gt_iff, toReal_decodeD, toReal_decodeD]
    exact mul_lt_mul_of_pos_right (by exact_mod_cast compare_gt_iff_gt.mp h) hs

theorem maxW_spec (a b : Word) : ofW (maxW a b) = Model.maximum (ofW a) (ofW b) := by
  unfold maxW
  split
  · exact ofW_toWord _
  · rename_i hs
    have ha := toDyadic_ofUInt32 a (fun h => hs (Or.inl h))
    have hb := toDyadic_ofUInt32 b (fun h => hs (Or.inr h))
    have hna := Model.isNaN_eq_false_of_toDyadic?_some ha
    have hnb := Model.isNaN_eq_false_of_toDyadic?_some hb
    have hc : Model.compare (ofW a) (ofW b) = some _ :=
      Model.compare_eq_some_cmpDyadic_of_toDyadic? _ _ ha hb
    rw [cmp_decodeD] at hc
    have hnone : Model.chooseNaN2 (ofW a) (ofW b) = none :=
      (Model.chooseNaN2_eq_none_iff _ _).2 ⟨hna, hnb⟩
    unfold Model.maximum
    rw [Model.withNaNSelection_of_none _ _ hnone]
    dsimp only
    rw [Model.compareNonNaN_eq_of_compare_eq_some _ _ _ _ hc]
    cases compare (keyOf a) (keyOf b) <;> simp only [ofW_toWord]

/-- `maxpoolSpec` with the running maximum kept as a word. -/
def maxpoolW (c h w k : Nat) (inp : Array Word) : Array Word :=
  Array.ofFn (n := c * (h / k * (w / k))) fun q =>
    let ch := q.val / (h / k * (w / k))
    let y := q.val / (w / k) % (h / k)
    let x := q.val % (w / k)
    (List.range (k * k)).foldl (fun acc t => maxW acc (poolAt h w k inp ch y x t))
      (poolAt h w k inp ch y x 0)

theorem foldl_maxW (p : Nat → Word) (l : List Nat) (acc : Word) :
    l.foldl (fun m t => Model.maximum m (ofW (p t))) (ofW acc) =
      ofW (l.foldl (fun a t => maxW a (p t)) acc) := by
  induction l generalizing acc with
  | nil => rfl
  | cons t l ih => simp only [List.foldl_cons, ← maxW_spec, ih]

theorem maxpoolW_eq : maxpoolW = maxpoolSpec := by
  funext c h w k inp
  simp only [maxpoolW, maxpoolSpec, foldl_maxW, toWord_ofW]

/-! ## The entry -/

def kernels : Kernels where
  core := ⟨sumX, dotX⟩
  dense := ⟨linearWith dotFn, gemmWith dotFn⟩
  conv := ⟨conv2dWith dotFn, depthwiseSpec⟩
  elem := ⟨avgpoolWith dotFn, maxpoolW, addReluSpec, scaleShiftSpec⟩
  norm := ⟨rmsnormWith dotFn, layernormWith dotFn⟩

theorem correct : Obligation kernels := by
  simp only [Obligation, kernels, specKernels, specCore, specDense, specConv, specElem, specNorm,
    linearSpec, gemmSpec, conv2dSpec, depthwiseSpec, avgpoolSpec, rmsnormSpec, layernormSpec,
    maxpoolW_eq, dotFn_eq_dotOf, funext sumX_eq_spec, funext fun a => funext (dotX_eq_spec a)]

end Submission
