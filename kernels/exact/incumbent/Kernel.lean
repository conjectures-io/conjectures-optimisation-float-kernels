import ContractExact

/-!
# Exact runtime, season 2: starting champion for the new families

The season-1 families are the contract's pinned kernels (`Pinned.kernels`); this entry supplies
the `attn` and `rnn` families:
- `sigmoid`, `tanh` and `exp` (for `x ≤ 0`) have fast paths (`FastAct`). `e^(−|x|)` is enclosed in
  64-bit fixed point (tables of `e^(−n)` and `e^(−j/64)`, a degree-6 Taylor sum, every product
  rounded in the safe direction); quotients start from a `Float` guess that is then checked
  exactly; a result is returned only when both ends of its enclosure round to the same finite
  nonzero word (`FastRound.roundW`, `FastRound.bracket_spec`). Otherwise the spec decides.
- `scores`, `mix` and softmax's sum use `dotSel`: sums of more than two terms go to `dotWin`, which
  sums the products truncated on a window below the largest exponent in 64-bit words and accepts
  when both ends of the `±n`-unit enclosure round alike (`dotWin_spec`); the rest use `dotFn`.
- LSTM and GRU use the fast activations with the pinned `dotFn`: their operands are computed per
  read, and the window path, which reads them twice, measured slower there.

`correct` proves the entry equals the specs; `sigmoidF_eq`, `tanhF_eq`, `expF_eq`, `dotWin_spec`
carry the new parts.
-/

namespace FastAct

/-! ## 128-bit products of 64-bit words -/

/-- The middle column of `a · b` from four 32-bit products (carries into the high half). -/
@[inline] def midW (a b : UInt64) : UInt64 :=
  (((a &&& (0xffffffff : UInt64)) * (b &&& (0xffffffff : UInt64))) >>> (32 : UInt64)) +
    (((a &&& (0xffffffff : UInt64)) * (b >>> (32 : UInt64))) &&& (0xffffffff : UInt64)) +
    (((a >>> (32 : UInt64)) * (b &&& (0xffffffff : UInt64))) &&& (0xffffffff : UInt64))

/-- The high 64 bits of `a · b`. -/
@[inline] def mulHiW (a b : UInt64) : UInt64 :=
  (a >>> (32 : UInt64)) * (b >>> (32 : UInt64)) + (((a &&& (0xffffffff : UInt64)) * (b >>> (32 : UInt64))) >>> (32 : UInt64)) +
    (((a >>> (32 : UInt64)) * (b &&& (0xffffffff : UInt64))) >>> (32 : UInt64)) + (midW a b >>> (32 : UInt64))

/-- The low 64 bits of `a · b`. -/
@[inline] def mulLoW (a b : UInt64) : UInt64 :=
  (midW a b <<< (32 : UInt64)) + ((a &&& (0xffffffff : UInt64)) * (b &&& (0xffffffff : UInt64)) &&& (0xffffffff : UInt64))

/-- `a · b` as `(high, low)` 64-bit halves (for proofs; the code reads the halves directly). -/
def mul128 (a b : UInt64) : UInt64 × UInt64 := (mulHiW a b, mulLoW a b)

theorem toNat_and_mask (x : UInt64) : (x &&& 0xffffffff).toNat = x.toNat % 2 ^ 32 := by
  rw [UInt64.toNat_and]
  exact Nat.and_two_pow_sub_one_eq_mod x.toNat 32

theorem toNat_shr32 (x : UInt64) : (x >>> 32).toNat = x.toNat / 2 ^ 32 := by
  rw [UInt64.toNat_shiftRight]
  simp [Nat.shiftRight_eq_div_pow]

theorem mul128_spec (a b : UInt64) :
    (mul128 a b).1.toNat * 2 ^ 64 + (mul128 a b).2.toNat = a.toNat * b.toNat := by
  have ha := a.toNat_lt
  have hb := b.toNat_lt
  have hk : a.toNat * b.toNat < 2 ^ 128 :=
    calc a.toNat * b.toNat < 2 ^ 64 * 2 ^ 64 := Nat.mul_lt_mul'' ha hb
      _ = 2 ^ 128 := by norm_num
  simp only [mul128, mulHiW, mulLoW, midW, UInt64.toNat_add, UInt64.toNat_mul,
    UInt64.toNat_shiftLeft, toNat_and_mask, toNat_shr32]
  have h32 : (32 : UInt64).toNat % 64 = 32 := by decide
  rw [h32, Nat.shiftLeft_eq]
  have hA := (Nat.div_add_mod' a.toNat (2 ^ 32)).symm
  have hB := (Nat.div_add_mod' b.toNat (2 ^ 32)).symm
  have hA0 : a.toNat % 2 ^ 32 < 2 ^ 32 := Nat.mod_lt _ (by norm_num)
  have hB0 : b.toNat % 2 ^ 32 < 2 ^ 32 := Nat.mod_lt _ (by norm_num)
  have hA1 : a.toNat / 2 ^ 32 < 2 ^ 32 := by omega
  have hB1 : b.toNat / 2 ^ 32 < 2 ^ 32 := by omega
  generalize a.toNat / 2 ^ 32 = A1 at *
  generalize a.toNat % 2 ^ 32 = A0 at *
  generalize b.toNat / 2 ^ 32 = B1 at *
  generalize b.toNat % 2 ^ 32 = B0 at *
  have prod_lt : ∀ x y : Nat, x < 2 ^ 32 → y < 2 ^ 32 → x * y < 2 ^ 64 := fun x y hx hy =>
    calc x * y < 2 ^ 32 * 2 ^ 32 := Nat.mul_lt_mul'' hx hy
      _ = 2 ^ 64 := by norm_num
  have m00 := prod_lt A0 B0 hA0 hB0
  have m01 := prod_lt A0 B1 hA0 hB1
  have m10 := prod_lt A1 B0 hA1 hB0
  have m11 := prod_lt A1 B1 hA1 hB1
  have hab : a.toNat * b.toNat = A1 * B1 * 2 ^ 64 + (A0 * B1 + A1 * B0) * 2 ^ 32 + A0 * B0 := by
    rw [hA, hB]; ring
  simp only [Nat.mod_eq_of_lt m00, Nat.mod_eq_of_lt m01, Nat.mod_eq_of_lt m10,
    Nat.mod_eq_of_lt m11]
  rw [hab]
  generalize A0 * B0 = p00 at *
  generalize A0 * B1 = p01 at *
  generalize A1 * B0 = p10 at *
  generalize A1 * B1 = p11 at *
  omega

/-! ## Fixed point on the grid `2^(−62)`

A natural number `a` stands for `a / 2^62` (`V a`). Every quantity below is at most about `1`, so
values stay below `2^63` and remain unboxed. -/

/-- `1` on the grid. -/
def one : Nat := 2 ^ 62

/-- The real value of a grid number. -/
noncomputable def V (a : Nat) : ℝ := (a : ℝ) / 2 ^ 62

theorem V_nonneg (a : Nat) : 0 ≤ V a := by unfold V; positivity

theorem V_one : V one = 1 := by norm_num [V, one]

theorem V_mono {a b : Nat} (h : a ≤ b) : V a ≤ V b := by
  unfold V; gcongr

theorem V_add (a b : Nat) : V (a + b) = V a + V b := by unfold V; push_cast; ring

theorem V_sub_ge (a b : Nat) : V a - V b ≤ V (a - b) := by
  unfold V
  rcases le_total b a with h | h
  · rw [Nat.cast_sub h]; ring_nf; rfl
  · rw [Nat.sub_eq_zero_of_le h]
    have : (a : ℝ) ≤ b := by exact_mod_cast h
    simp only [Nat.cast_zero, zero_div]
    rw [div_sub_div_same]
    exact div_nonpos_of_nonpos_of_nonneg (by linarith) (by positivity)

theorem V_sub_le_of_le {a b : Nat} (h : b ≤ a) : V (a - b) = V a - V b := by
  unfold V; rw [Nat.cast_sub h]; ring

/-- `⌊a · b / 2^62⌋` as a word (wrapping past `2^64`). -/
@[inline] def mulDnW (a b : UInt64) : UInt64 := (mulHiW a b <<< 2) + (mulLoW a b >>> 62)

/-- `⌈a · b / 2^62⌉` as a word. -/
@[inline] def mulUpW (a b : UInt64) : UInt64 :=
  if mulLoW a b &&& 0x3fffffffffffffff = 0 then mulDnW a b else mulDnW a b + 1

/-- `⌊a · b / 2^62⌋`; a lower bound for any `a, b`. -/
@[inline] def mulDn (a b : Nat) : Nat := (mulDnW a.toUInt64 b.toUInt64).toNat

/-- `⌈a · b / 2^62⌉` for `a, b ≤ one`. -/
@[inline] def mulUp (a b : Nat) : Nat := (mulUpW a.toUInt64 b.toUInt64).toNat

theorem toNat_toUInt64_le (a : Nat) : a.toUInt64.toNat ≤ a := by
  rw [UInt64.toNat_ofNat']; exact Nat.mod_le _ _

theorem toNat_toUInt64_of_lt {a : Nat} (h : a < 2 ^ 64) : a.toUInt64.toNat = a := by
  rw [UInt64.toNat_ofNat']; exact Nat.mod_eq_of_lt h

theorem toNat_mulDnW (a b : UInt64) : (mulDnW a b).toNat =
    ((mulHiW a b).toNat * 4 % 2 ^ 64 + (mulLoW a b).toNat / 2 ^ 62) % 2 ^ 64 := by
  unfold mulDnW
  rw [UInt64.toNat_add, UInt64.toNat_shiftLeft, UInt64.toNat_shiftRight]
  have h2 : (2 : UInt64).toNat % 64 = 2 := by decide
  have h62 : (62 : UInt64).toNat % 64 = 62 := by decide
  rw [h2, h62, Nat.shiftLeft_eq, Nat.shiftRight_eq_div_pow]

theorem low62_zero_iff (x : UInt64) : (x &&& 0x3fffffffffffffff = 0) ↔ x.toNat % 2 ^ 62 = 0 := by
  rw [← UInt64.toNat_inj, UInt64.toNat_and]
  have : (0x3fffffffffffffff : UInt64).toNat = 2 ^ 62 - 1 := by decide
  rw [this, Nat.and_two_pow_sub_one_eq_mod]; simp

/-- The halves of a product of two numbers below `2^64`. -/
theorem halves {a b : Nat} (ha : a < 2 ^ 64) (hb : b < 2 ^ 64) :
    (mulHiW a.toUInt64 b.toUInt64).toNat * 2 ^ 64 + (mulLoW a.toUInt64 b.toUInt64).toNat = a * b := by
  have hs := mul128_spec a.toUInt64 b.toUInt64
  rwa [toNat_toUInt64_of_lt ha, toNat_toUInt64_of_lt hb] at hs

theorem mulDn_mul_le (a b : Nat) : mulDn a b * 2 ^ 62 ≤ a * b := by
  have hs := mul128_spec a.toUInt64 b.toUInt64
  have hle : a.toUInt64.toNat * b.toUInt64.toNat ≤ a * b :=
    Nat.mul_le_mul (toNat_toUInt64_le a) (toNat_toUInt64_le b)
  simp only [mul128] at hs
  unfold mulDn
  rw [toNat_mulDnW]
  generalize (mulHiW a.toUInt64 b.toUInt64).toNat = h at *
  generalize (mulLoW a.toUInt64 b.toUInt64).toNat = l at *
  omega

/-- For operands at most `one`, `mulUp` is the exact ceiling: within `2^62` above `a · b`. -/
theorem mulUp_bounds {a b : Nat} (ha : a ≤ one) (hb : b ≤ one) :
    a * b ≤ mulUp a b * 2 ^ 62 ∧ mulUp a b * 2 ^ 62 < a * b + 2 ^ 62 := by
  have hs := halves (a := a) (b := b) (by unfold one at ha; omega) (by unfold one at hb; omega)
  have hab : a * b ≤ 2 ^ 62 * 2 ^ 62 := Nat.mul_le_mul ha hb
  have hl := (mulLoW a.toUInt64 b.toUInt64).toNat_lt
  unfold mulUp mulUpW
  split
  · rename_i h
    rw [low62_zero_iff] at h
    rw [toNat_mulDnW]
    generalize a * b = p at *
    generalize (mulHiW a.toUInt64 b.toUInt64).toNat = hi at *
    generalize (mulLoW a.toUInt64 b.toUInt64).toNat = l at *
    omega
  · rename_i h
    rw [low62_zero_iff] at h
    rw [UInt64.toNat_add, toNat_mulDnW, show (1 : UInt64).toNat = 1 from rfl]
    generalize a * b = p at *
    generalize (mulHiW a.toUInt64 b.toUInt64).toNat = hi at *
    generalize (mulLoW a.toUInt64 b.toUInt64).toNat = l at *
    omega

theorem mulUp_mul_ge {a b : Nat} (ha : a ≤ one) (hb : b ≤ one) :
    a * b ≤ mulUp a b * 2 ^ 62 := (mulUp_bounds ha hb).1

theorem mulUp_le {a b : Nat} (ha : a ≤ one) (hb : b ≤ one) : mulUp a b ≤ one := by
  have h := (mulUp_bounds ha hb).2
  have hab : a * b ≤ 2 ^ 62 * 2 ^ 62 := Nat.mul_le_mul ha hb
  unfold one at *
  generalize a * b = p at *
  omega

theorem V_mulDn (a b : Nat) : V (mulDn a b) ≤ V a * V b := by
  have h := mulDn_mul_le a b
  unfold V
  rw [div_mul_div_comm, le_div_iff₀ (by positivity), div_mul_eq_mul_div, div_le_iff₀ (by positivity)]
  have : ((mulDn a b * 2 ^ 62 : Nat) : ℝ) ≤ ((a * b : Nat) : ℝ) := by exact_mod_cast h
  push_cast at this
  nlinarith [this]

theorem V_mulUp {a b : Nat} (ha : a ≤ one) (hb : b ≤ one) : V a * V b ≤ V (mulUp a b) := by
  have h := mulUp_mul_ge ha hb
  unfold V
  rw [div_mul_div_comm, div_le_iff₀ (by positivity), div_mul_eq_mul_div, le_div_iff₀ (by positivity)]
  have : ((a * b : Nat) : ℝ) ≤ ((mulUp a b * 2 ^ 62 : Nat) : ℝ) := by exact_mod_cast h
  push_cast at this
  nlinarith [this]

/-- Lower bounds multiply. -/
theorem V_mulDn_le {a b : Nat} {x y : ℝ} (ha : V a ≤ x) (hb : V b ≤ y) :
    V (mulDn a b) ≤ x * y :=
  (V_mulDn a b).trans (mul_le_mul ha hb (V_nonneg b) ((V_nonneg a).trans ha))

/-- Upper bounds multiply (operands at most `1`). -/
theorem le_V_mulUp {a b : Nat} {x y : ℝ} (ha : x ≤ V a) (hb : y ≤ V b) (hy : 0 ≤ y)
    (ha1 : a ≤ one) (hb1 : b ≤ one) : x * y ≤ V (mulUp a b) :=
  (mul_le_mul ha hb hy (V_nonneg a)).trans (V_mulUp ha1 hb1)

theorem V_div_le (k : Nat) : V (one / k) ≤ 1 / k := by
  rcases Nat.eq_zero_or_pos k with rfl | hk
  · simp [V]
  have h : one / k * k ≤ one := Nat.div_mul_le_self one k
  have hc : ((one / k : Nat) : ℝ) * k ≤ (one : ℝ) := by exact_mod_cast h
  unfold V
  rw [div_le_div_iff₀ (by positivity) (by exact_mod_cast hk)]
  have : (one : ℝ) = 2 ^ 62 := by norm_num [one]
  linarith

theorem le_V_divUp (k : Nat) (hk : 0 < k) : 1 / (k : ℝ) ≤ V ((one + k - 1) / k) := by
  have h : one ≤ (one + k - 1) / k * k := by
    have h2 := Nat.div_add_mod (one + k - 1) k
    have h3 := Nat.mod_lt (one + k - 1) hk
    have : (one + k - 1) / k * k = k * ((one + k - 1) / k) := Nat.mul_comm _ _
    omega
  have hc : (one : ℝ) ≤ (((one + k - 1) / k : Nat) : ℝ) * k := by exact_mod_cast h
  unfold V
  rw [div_le_div_iff₀ (by exact_mod_cast hk) (by positivity)]
  have : (one : ℝ) = 2 ^ 62 := by norm_num [one]
  linarith

/-! ## `e^(−r)` for `0 ≤ r ≤ 1/64`: Taylor sum of degree 6 with directed rounding -/

def f2l : Nat := one / 2
def f3l : Nat := one / 6
def f4l : Nat := one / 24
def f5l : Nat := one / 120
def f6l : Nat := one / 720
def f2h : Nat := (one + 2 - 1) / 2
def f3h : Nat := (one + 6 - 1) / 6
def f4h : Nat := (one + 24 - 1) / 24
def f5h : Nat := (one + 120 - 1) / 120
def f6h : Nat := (one + 720 - 1) / 720
/-- `8 / (7! · 7)` rounded up: the Taylor remainder factor (`Real.exp_bound`, `n = 7`). -/
def remF : Nat := (one + 35280 - 1) / 35280 * 8

/-- Enclosure of `e^(−r)` from `V rl ≤ r ≤ V rh`. -/
@[inline] def expSmall (rl rh : Nat) : Nat × Nat :=
  let p2l := mulDn rl rl
  let p3l := mulDn p2l rl
  let p4l := mulDn p3l rl
  let p5l := mulDn p4l rl
  let p6l := mulDn p5l rl
  let p2h := mulUp rh rh
  let p3h := mulUp p2h rh
  let p4h := mulUp p3h rh
  let p5h := mulUp p4h rh
  let p6h := mulUp p5h rh
  let p7h := mulUp p6h rh
  let lo := (one + mulDn p2l f2l + mulDn p4l f4l + mulDn p6l f6l) -
    (rh + mulUp p3h f3h + mulUp p5h f5h + mulUp p7h remF)
  let hi := (one + mulUp p2h f2h + mulUp p4h f4h + mulUp p6h f6h + mulUp p7h remF) -
    (rl + mulDn p3l f3l + mulDn p5l f5l)
  (lo, min one hi)

theorem exp_neg_taylor {r : ℝ} (h0 : 0 ≤ r) (h1 : r ≤ 1) :
    |Real.exp (-r) - (1 - r + r ^ 2 / 2 - r ^ 3 / 6 + r ^ 4 / 24 - r ^ 5 / 120 + r ^ 6 / 720)| ≤
      r ^ 7 * (8 / 35280) := by
  have h := Real.exp_bound (x := -r) (by rw [abs_neg, abs_of_nonneg h0]; exact h1) (n := 7)
    (by norm_num)
  simp only [Finset.sum_range_succ, Finset.sum_range_zero, Nat.factorial] at h
  rw [abs_neg, abs_of_nonneg h0] at h
  convert h using 2
  · norm_num; ring
  · norm_num

theorem f_le_one {x : Nat} (k : Nat) (hk : 0 < k) (h : x = (one + k - 1) / k) : x ≤ one := by
  subst h; unfold one; apply Nat.div_le_of_le_mul; omega

theorem expSmall_spec {rl rh : Nat} {r : ℝ} (hl : V rl ≤ r) (hh : r ≤ V rh) (hrh : rh ≤ one) :
    V (expSmall rl rh).1 ≤ Real.exp (-r) ∧ Real.exp (-r) ≤ V (expSmall rl rh).2 ∧
      (expSmall rl rh).2 ≤ one := by
  have h0 : 0 ≤ r := (V_nonneg rl).trans hl
  have h1 : r ≤ 1 := hh.trans (V_one ▸ V_mono hrh)
  have hT := exp_neg_taylor h0 h1
  have hp : ∀ n, 0 ≤ r ^ n := fun n => pow_nonneg h0 n
  -- lower powers
  have l2 : V (mulDn rl rl) ≤ r ^ 2 := by rw [sq]; exact V_mulDn_le hl hl
  have l3 : V (mulDn (mulDn rl rl) rl) ≤ r ^ 3 := by rw [pow_succ]; exact V_mulDn_le l2 hl
  have l4 : V (mulDn (mulDn (mulDn rl rl) rl) rl) ≤ r ^ 4 := by
    rw [pow_succ]; exact V_mulDn_le l3 hl
  have l5 : V (mulDn (mulDn (mulDn (mulDn rl rl) rl) rl) rl) ≤ r ^ 5 := by
    rw [pow_succ]; exact V_mulDn_le l4 hl
  have l6 : V (mulDn (mulDn (mulDn (mulDn (mulDn rl rl) rl) rl) rl) rl) ≤ r ^ 6 := by
    rw [pow_succ]; exact V_mulDn_le l5 hl
  -- upper powers, each at most `one`
  have u2 : r ^ 2 ≤ V (mulUp rh rh) := by rw [sq]; exact le_V_mulUp hh hh h0 hrh hrh
  have b2 : mulUp rh rh ≤ one := mulUp_le hrh hrh
  have u3 : r ^ 3 ≤ V (mulUp (mulUp rh rh) rh) := by
    rw [pow_succ]; exact le_V_mulUp u2 hh h0 b2 hrh
  have b3 := mulUp_le b2 hrh
  have u4 : r ^ 4 ≤ V (mulUp (mulUp (mulUp rh rh) rh) rh) := by
    rw [pow_succ]; exact le_V_mulUp u3 hh h0 b3 hrh
  have b4 := mulUp_le b3 hrh
  have u5 : r ^ 5 ≤ V (mulUp (mulUp (mulUp (mulUp rh rh) rh) rh) rh) := by
    rw [pow_succ]; exact le_V_mulUp u4 hh h0 b4 hrh
  have b5 := mulUp_le b4 hrh
  have u6 : r ^ 6 ≤ V (mulUp (mulUp (mulUp (mulUp (mulUp rh rh) rh) rh) rh) rh) := by
    rw [pow_succ]; exact le_V_mulUp u5 hh h0 b5 hrh
  have b6 := mulUp_le b5 hrh
  have u7 : r ^ 7 ≤ V (mulUp (mulUp (mulUp (mulUp (mulUp (mulUp rh rh) rh) rh) rh) rh) rh) := by
    rw [pow_succ]; exact le_V_mulUp u6 hh h0 b6 hrh
  have b7 := mulUp_le b6 hrh
  -- factorial factors
  have fl : ∀ (k : Nat) (x : Nat), x = one / k → V x ≤ 1 / k := fun k x h => h ▸ V_div_le k
  have fh : ∀ (k : Nat) (x : Nat), 0 < k → x = (one + k - 1) / k → 1 / (k : ℝ) ≤ V x :=
    fun k x hk h => h ▸ le_V_divUp k hk
  have hf2l := fl 2 f2l rfl
  have hf3l := fl 6 f3l rfl
  have hf4l := fl 24 f4l rfl
  have hf5l := fl 120 f5l rfl
  have hf6l := fl 720 f6l rfl
  have hf2h := fh 2 f2h (by norm_num) rfl
  have hf3h := fh 6 f3h (by norm_num) rfl
  have hf4h := fh 24 f4h (by norm_num) rfl
  have hf5h := fh 120 f5h (by norm_num) rfl
  have hf6h := fh 720 f6h (by norm_num) rfl
  have hremF : 8 / (35280 : ℝ) ≤ V remF := by
    have h := fh 35280 ((one + 35280 - 1) / 35280) (by norm_num) rfl
    unfold remF V at *
    push_cast at *
    rw [mul_div_right_comm]
    nlinarith
  have bf2 := f_le_one 2 (by norm_num) (rfl : f2h = _)
  have bf3 := f_le_one 6 (by norm_num) (rfl : f3h = _)
  have bf4 := f_le_one 24 (by norm_num) (rfl : f4h = _)
  have bf5 := f_le_one 120 (by norm_num) (rfl : f5h = _)
  have bf6 := f_le_one 720 (by norm_num) (rfl : f6h = _)
  have brem : remF ≤ one := by unfold remF one; norm_num
  have c2 := V_mulDn_le l2 hf2l
  have c3 := V_mulDn_le l3 hf3l
  have c4 := V_mulDn_le l4 hf4l
  have c5 := V_mulDn_le l5 hf5l
  have c6 := V_mulDn_le l6 hf6l
  have d2 := le_V_mulUp u2 hf2h (by norm_num) b2 bf2
  have d3 := le_V_mulUp u3 hf3h (by norm_num) b3 bf3
  have d4 := le_V_mulUp u4 hf4h (by norm_num) b4 bf4
  have d5 := le_V_mulUp u5 hf5h (by norm_num) b5 bf5
  have d6 := le_V_mulUp u6 hf6h (by norm_num) b6 bf6
  have d7 := le_V_mulUp u7 hremF (by norm_num) b7 brem
  have hexp1 : Real.exp (-r) ≤ 1 := Real.exp_le_one_iff.mpr (by linarith)
  have hab := abs_le.mp hT
  refine ⟨?_, ?_, min_le_left _ _⟩
  · simp only [expSmall]
    refine le_trans (b := max 0 (V (one + mulDn (mulDn rl rl) f2l +
      mulDn (mulDn (mulDn (mulDn rl rl) rl) rl) f4l +
      mulDn (mulDn (mulDn (mulDn (mulDn (mulDn rl rl) rl) rl) rl) rl) f6l) -
      V (rh + mulUp (mulUp (mulUp rh rh) rh) f3h +
        mulUp (mulUp (mulUp (mulUp (mulUp rh rh) rh) rh) rh) f5h +
        mulUp (mulUp (mulUp (mulUp (mulUp (mulUp (mulUp rh rh) rh) rh) rh) rh) rh) remF))) ?_ ?_
    · unfold V
      rw [le_max_iff]
      rcases le_total (rh + mulUp (mulUp (mulUp rh rh) rh) f3h +
        mulUp (mulUp (mulUp (mulUp (mulUp rh rh) rh) rh) rh) f5h +
        mulUp (mulUp (mulUp (mulUp (mulUp (mulUp (mulUp rh rh) rh) rh) rh) rh) rh) remF)
        (one + mulDn (mulDn rl rl) f2l +
          mulDn (mulDn (mulDn (mulDn rl rl) rl) rl) f4l +
          mulDn (mulDn (mulDn (mulDn (mulDn (mulDn rl rl) rl) rl) rl) rl) f6l) with h | h
      · right; rw [Nat.cast_sub h]; ring_nf; rfl
      · left; rw [Nat.sub_eq_zero_of_le h]; simp
    · apply max_le (Real.exp_pos _).le
      simp only [V_add, V_one] at *
      nlinarith
  · simp only [expSmall]
    rcases le_total one (one + mulUp (mulUp rh rh) f2h + mulUp (mulUp (mulUp (mulUp rh rh) rh) rh) f4h +
        mulUp (mulUp (mulUp (mulUp (mulUp (mulUp rh rh) rh) rh) rh) rh) f6h +
        mulUp (mulUp (mulUp (mulUp (mulUp (mulUp (mulUp rh rh) rh) rh) rh) rh) rh) remF -
        (rl + mulDn (mulDn (mulDn rl rl) rl) f3l +
          mulDn (mulDn (mulDn (mulDn (mulDn rl rl) rl) rl) rl) f5l)) with h | h
    · rw [min_eq_left h, V_one]; exact hexp1
    · rw [min_eq_right h]
      refine le_trans ?_ (V_sub_ge _ _)
      simp only [V_add, V_one]
      linarith [hab.2]

/-! ## Tables: powers of an enclosure -/

/-- An enclosure `(lo, hi)` of `e^(−b)` with `hi ≤ one`. -/
def Encl (iv : Nat × Nat) (b : ℝ) : Prop :=
  V iv.1 ≤ Real.exp (-b) ∧ Real.exp (-b) ≤ V iv.2 ∧ iv.2 ≤ one

/-- `j`-th power of an enclosure of `e^(−b)`: an enclosure of `e^(−j b)`. -/
def powOf (base : Nat × Nat) : Nat → Nat × Nat
  | 0 => (one, one)
  | j + 1 => let p := powOf base j; (mulDn p.1 base.1, min one (mulUp p.2 base.2))

/-- The product of enclosures of `e^(−a)` and `e^(−b)` encloses `e^(−(a + b))`. -/
theorem Encl.mul {x y : Nat × Nat} {a b : ℝ} (hx : Encl x a) (hy : Encl y b) :
    Encl (mulDn x.1 y.1, min one (mulUp x.2 y.2)) (a + b) := by
  have he : Real.exp (-(a + b)) = Real.exp (-a) * Real.exp (-b) := by
    rw [← Real.exp_add]; ring_nf
  refine ⟨?_, ?_, min_le_left _ _⟩
  · rw [he]; exact V_mulDn_le hx.1 hy.1
  · rw [he]
    rcases le_total one (mulUp x.2 y.2) with h | h
    · rw [min_eq_left h, V_one, ← he]
      exact Real.exp_le_one_iff.mpr (by
        have := Real.exp_le_one_iff.mp (le_trans hx.2.1 (V_one ▸ V_mono hx.2.2))
        have := Real.exp_le_one_iff.mp (le_trans hy.2.1 (V_one ▸ V_mono hy.2.2))
        linarith)
    · rw [min_eq_right h]
      exact le_V_mulUp hx.2.1 hy.2.1 (Real.exp_pos _).le hx.2.2 hy.2.2

theorem powOf_spec {base : Nat × Nat} {b : ℝ} (hb : Encl base b) :
    ∀ j : Nat, Encl (powOf base j) (j * b)
  | 0 => by
    refine ⟨?_, ?_, le_rfl⟩ <;> simp [powOf, V_one]
  | j + 1 => by
    have := (powOf_spec hb j).mul hb
    simp only [powOf]
    convert this using 2; push_cast; ring

/-- `2^56`, that is `1/64` on the grid. -/
def sixtyFourth : Nat := 2 ^ 56

theorem V_sixtyFourth : V sixtyFourth = 1 / 64 := by norm_num [V, sixtyFourth]

/-- An enclosure of `e^(−1/64)`. -/
def base64 : Nat × Nat := expSmall sixtyFourth sixtyFourth

theorem base64_spec : Encl base64 (1 / 64) := by
  exact expSmall_spec (rl := sixtyFourth) (rh := sixtyFourth) (r := 1 / 64)
    V_sixtyFourth.le V_sixtyFourth.ge (by norm_num [sixtyFourth, one])

/-- `e^(−j/64)` for `j < 64`. -/
def fracTab : Array (Nat × Nat) := Array.ofFn (n := 64) fun j => powOf base64 j.val

/-- An enclosure of `e^(−1)`. -/
def baseOne : Nat × Nat := powOf base64 64

/-- `e^(−n)` for `n ≤ 16`. -/
def intTab : Array (Nat × Nat) := Array.ofFn (n := 17) fun n => powOf baseOne n.val

theorem getElem!_ofFn' {α : Type} [Inhabited α] {n : Nat} (f : Fin n → α) {i : Nat}
    (h : i < n) : (Array.ofFn f)[i]! = f ⟨i, h⟩ := by
  rw [getElem!_pos (Array.ofFn f) i (by simpa using h)]
  simp

theorem fracTab_spec {j : Nat} (h : j < 64) : Encl fracTab[j]! (j * (1 / 64)) := by
  rw [fracTab, getElem!_ofFn' _ h]
  exact powOf_spec base64_spec j

theorem intTab_spec {n : Nat} (h : n < 17) : Encl intTab[n]! n := by
  rw [intTab, getElem!_ofFn' _ h]
  have h1 : Encl baseOne 1 := by
    have := powOf_spec base64_spec 64
    simp only [baseOne]; convert this using 1; norm_num
  have := powOf_spec h1 n
  simpa using this

/-! ## `e^(−c)` for `0 ≤ c < 17`: `c = n + j/64 + r` -/

/-- `c · 2^56` rounded down and up, for `c = m · 2^e`. -/
@[inline] def scaled (m : Nat) (e : Int) : Nat × Nat :=
  if 0 ≤ e + 56 then let v := m <<< (e + 56).toNat; (v, v)
  else let v := m >>> (-(e + 56)).toNat; (v, v + 1)

theorem scaled_spec (m : Nat) (e : Int) :
    ((scaled m e).1 : ℝ) ≤ m * (2 : ℝ) ^ e * 2 ^ 56 ∧
      (m : ℝ) * (2 : ℝ) ^ e * 2 ^ 56 ≤ (scaled m e).2 ∧ (scaled m e).1 ≤ (scaled m e).2 := by
  unfold scaled
  have hz : (m : ℝ) * (2 : ℝ) ^ e * 2 ^ 56 = m * (2 : ℝ) ^ (e + 56) := by
    rw [zpow_add₀ two_ne_zero]; norm_num; ring
  split
  · rename_i h
    have hk : ((2 : ℝ) ^ (e + 56)) = 2 ^ (e + 56).toNat := by
      rw [← zpow_natCast, Int.toNat_of_nonneg h]
    simp only [Nat.shiftLeft_eq, hz, hk]; push_cast; simp
  · rename_i h
    set k := (-(e + 56)).toNat
    have hk : ((2 : ℝ) ^ (e + 56)) = 1 / 2 ^ k := by
      rw [show e + 56 = -(k : Int) by omega, zpow_neg, zpow_natCast, one_div]
    simp only [Nat.shiftRight_eq_div_pow, hz, hk]
    have hp : (0 : ℝ) < 2 ^ k := by positivity
    have h1 := Nat.div_mul_le_self m (2 ^ k)
    have h2 := Nat.lt_succ_iff.mpr (le_refl (m / 2 ^ k))
    have h3 : m < (m / 2 ^ k + 1) * 2 ^ k := by
      have := Nat.lt_mul_div_succ m (Nat.two_pow_pos k); linarith
    refine ⟨?_, ?_, Nat.le_succ _⟩
    · rw [mul_one_div, le_div_iff₀ hp]; exact_mod_cast h1
    · rw [mul_one_div, div_le_iff₀ hp]; push_cast; exact_mod_cast h3.le

def lim17 : Nat := 17 * 2 ^ 56

/-- An enclosure of `e^(−c)` for `c = m · 2^e`, when `c < 17`. -/
@[inline] def expNeg (m : Nat) (e : Int) : Option (Nat × Nat) :=
  let c := scaled m e
  if lim17 ≤ c.2 then none else
  let q := c.1 >>> 50
  let rl := (c.1 - (q <<< 50)) <<< 6
  let rh := (c.2 - (q <<< 50)) <<< 6
  if one < rh then none else
  let P := expSmall rl rh
  let F := fracTab[q % 64]!
  let I := intTab[q >>> 6]!
  let IF := (mulDn I.1 F.1, min one (mulUp I.2 F.2))
  some (mulDn IF.1 P.1, min one (mulUp IF.2 P.2))

theorem expNeg_spec {m : Nat} {e : Int} {iv : Nat × Nat} (h : expNeg m e = some iv) :
    Encl iv (m * (2 : ℝ) ^ e) := by
  unfold expNeg at h
  simp only [Nat.shiftRight_eq_div_pow, Nat.shiftLeft_eq] at h
  obtain ⟨hlo, hhi, hle⟩ := scaled_spec m e
  generalize (scaled m e).1 = cl at *
  generalize (scaled m e).2 = ch at *
  split at h
  · exact absurd h (by simp)
  rename_i hlim
  split at h
  · exact absurd h (by simp)
  rename_i hrh
  simp only [Option.some.injEq] at h
  subst h
  set q := cl / 2 ^ 50
  have hq : q * 2 ^ 50 ≤ cl := Nat.div_mul_le_self cl _
  have hqd : q = q / 2 ^ 6 * 64 + q % 64 := by have := Nat.div_add_mod q 64; omega
  have hn : q / 2 ^ 6 < 17 := by unfold lim17 at hlim; omega
  have hj : q % 64 < 64 := Nat.mod_lt _ (by norm_num)
  set C := (m : ℝ) * (2 : ℝ) ^ e
  set r := C - (q / 2 ^ 6 : Nat) - (q % 64 : Nat) * (1 / 64 : ℝ)
  have hr : C = (q / 2 ^ 6 : Nat) + (q % 64 : Nat) * (1 / 64 : ℝ) + r := by ring
  have hqr : ((q : Nat) : ℝ) * 2 ^ 50 = ((q / 2 ^ 6 : Nat) : ℝ) * 2 ^ 56 + ((q % 64 : Nat) : ℝ) * 2 ^ 50 := by
    have : ((q : Nat) : ℝ) = ((q / 2 ^ 6 * 64 + q % 64 : Nat) : ℝ) := by rw [← hqd]
    rw [this]; push_cast; ring
  have hrl : V ((cl - q * 2 ^ 50) * 2 ^ 6) ≤ r := by
    unfold V; rw [Nat.cast_mul, Nat.cast_sub hq]; push_cast
    rw [div_le_iff₀ (by positivity)]
    simp only [r]; nlinarith [hqr]
  have hrh' : r ≤ V ((ch - q * 2 ^ 50) * 2 ^ 6) := by
    unfold V; rw [Nat.cast_mul, Nat.cast_sub (hq.trans hle)]; push_cast
    rw [le_div_iff₀ (by positivity)]
    simp only [r]; nlinarith [hqr]
  have hP := expSmall_spec hrl hrh' (by omega)
  have hI := intTab_spec hn
  have hF := fracTab_spec hj
  rw [hr]
  exact (hI.mul hF).mul hP

/-! ## Quotients: a guess, checked exactly -/

def two62f : Float := 4611686018427387904.0

/-- `q · d ≤ num · 2^62` by 128-bit comparison (for `q, d < 2^64`). -/
@[inline] def mulLe (q d num : Nat) : Bool :=
  let h := (mulHiW q.toUInt64 d.toUInt64).toNat
  let nh := num >>> 2
  h < nh || (h == nh && mulLoW q.toUInt64 d.toUInt64 ≤ (num % 4).toUInt64 <<< 62)

/-- `num · 2^62 ≤ q · d`. -/
@[inline] def mulGe (q d num : Nat) : Bool :=
  let h := (mulHiW q.toUInt64 d.toUInt64).toNat
  let nh := num >>> 2
  nh < h || (h == nh && (num % 4).toUInt64 <<< 62 ≤ mulLoW q.toUInt64 d.toUInt64)

theorem num_split (num : Nat) :
    ((num % 4).toUInt64 <<< 62).toNat = num % 4 * 2 ^ 62 := by
  have h4 : num % 4 < 4 := Nat.mod_lt _ (by norm_num)
  rw [UInt64.toNat_shiftLeft, toNat_toUInt64_of_lt (by omega)]
  have : (62 : UInt64).toNat % 64 = 62 := by decide
  rw [this, Nat.shiftLeft_eq, Nat.mod_eq_of_lt (by omega)]

theorem mulLe_spec {q d num : Nat} (hq : q < 2 ^ 64) (hd : d < 2 ^ 64) (h : mulLe q d num = true) :
    q * d ≤ num * 2 ^ 62 := by
  have hs := halves hq hd
  unfold mulLe at h
  simp only [Nat.shiftRight_eq_div_pow, Bool.or_eq_true, decide_eq_true_eq, Bool.and_eq_true,
    beq_iff_eq, UInt64.le_iff_toNat_le, num_split] at h
  have hl := (mulLoW q.toUInt64 d.toUInt64).toNat_lt
  generalize (mulHiW q.toUInt64 d.toUInt64).toNat = a at *
  generalize (mulLoW q.toUInt64 d.toUInt64).toNat = b at *
  omega

theorem mulGe_spec {q d num : Nat} (hq : q < 2 ^ 64) (hd : d < 2 ^ 64) (h : mulGe q d num = true) :
    num * 2 ^ 62 ≤ q * d := by
  have hs := halves hq hd
  unfold mulGe at h
  simp only [Nat.shiftRight_eq_div_pow, Bool.or_eq_true, decide_eq_true_eq, Bool.and_eq_true,
    beq_iff_eq, UInt64.le_iff_toNat_le, num_split] at h
  have hl := (mulLoW q.toUInt64 d.toUInt64).toNat_lt
  generalize (mulHiW q.toUInt64 d.toUInt64).toNat = a at *
  generalize (mulLoW q.toUInt64 d.toUInt64).toNat = b at *
  omega

/-- A guess for `num · 2^62 / den` (untrusted; only the checks below matter). -/
@[inline] def guess (num den : Nat) : Nat :=
  ((num.toUInt64.toFloat * two62f) / den.toUInt64.toFloat).toUInt64.toNat

/-- `2^64`, computed once. -/
def lim64 : Nat := 2 ^ 64

/-- `q` with `V q · V den ≤ V num`. -/
@[inline] def divLo (num den : Nat) : Option Nat :=
  let g := guess num den
  let q := g - ((g >>> 50) + 2)
  if q < lim64 && den < lim64 && mulLe q den num then some q else none

/-- `q` with `V num ≤ V q · V den`. -/
@[inline] def divHi (num den : Nat) : Option Nat :=
  let g := guess num den
  let q := g + ((g >>> 50) + 2)
  if q < lim64 && den < lim64 && mulGe q den num then some q else none

theorem V_mul_iff (a b c : Nat) : V a * V b ≤ V c ↔ a * b ≤ c * 2 ^ 62 := by
  unfold V
  rw [div_mul_div_comm, div_le_div_iff₀ (by positivity) (by positivity)]
  constructor
  · intro h
    have : ((a * b : Nat) : ℝ) * 2 ^ 62 ≤ ((c * 2 ^ 62 : Nat) : ℝ) * 2 ^ 62 := by
      push_cast; nlinarith
    exact_mod_cast le_of_mul_le_mul_right this (by positivity)
  · intro h
    have : ((a * b : Nat) : ℝ) ≤ ((c * 2 ^ 62 : Nat) : ℝ) := by exact_mod_cast h
    push_cast at this; nlinarith

theorem divLo_spec {num den q : Nat} (h : divLo num den = some q) : V q * V den ≤ V num := by
  unfold divLo at h
  dsimp only at h
  generalize guess num den = g at h
  split at h
  · rename_i hc
    simp only [Bool.and_eq_true, decide_eq_true_eq] at hc
    simp only [Option.some.injEq] at h; subst h
    exact (V_mul_iff _ _ _).mpr (mulLe_spec hc.1.1 hc.1.2 hc.2)
  · simp at h

theorem divHi_spec {num den q : Nat} (h : divHi num den = some q) : V num ≤ V q * V den := by
  unfold divHi at h
  dsimp only at h
  generalize guess num den = g at h
  split at h
  · rename_i hc
    simp only [Bool.and_eq_true, decide_eq_true_eq] at hc
    simp only [Option.some.injEq] at h; subst h
    have h2 := mulGe_spec hc.1.1 hc.1.2 hc.2
    unfold V
    rw [div_mul_div_comm, div_le_div_iff₀ (by positivity) (by positivity)]
    have : ((num * 2 ^ 62 : Nat) : ℝ) ≤ (((g + (g >>> 50 + 2)) * den : Nat) : ℝ) := by exact_mod_cast h2
    push_cast at this ⊢; nlinarith
  · simp at h

/-! ## Rounding both ends, and the bridge to the specs -/

open ExactRuntime FloatLib.Floats.Formats.BinaryInterchange FloatLib.Floats FloatLib.Numerics
open ExactRuntime.FastRound

/-- The word both ends of `[lo, hi] · 2^(−62)` round to, when they agree on a finite nonzero
word, through FloatLib's rounding (the reference for `settle`). -/
def settleInt (lo hi : Int) : Option UInt32 :=
  let a := finishWord lo (-62) false
  if lo ≠ 0 && hi ≠ 0 && a == finishWord hi (-62) false && expOf a != 255 &&
      (decodeD a).significand != 0 then some a else none

@[inline] def settle (neg : Bool) (a b : Nat) : Option UInt32 :=
  let w := roundW neg a (-62)
  if a ≠ 0 && b ≠ 0 && w == roundW neg b (-62) && expOf w != 255 &&
      (decodeD w).significand != 0 then some w else none

theorem settle_eq (neg : Bool) (a b : Nat) :
    settle neg a b =
      settleInt (if neg then -(b : Int) else a) (if neg then -(a : Int) else b) := by
  unfold settle settleInt
  by_cases ha : a = 0
  · subst ha; cases neg <;> simp
  by_cases hb : b = 0
  · subst hb; cases neg <;> simp
  rw [roundW_signed neg a _ ha, roundW_signed neg b _ hb]
  cases neg
  · simp
  · simp only [ite_true]
    by_cases he : finishWord (-(a : Int)) (-62) false = finishWord (-(b : Int)) (-62) false
    · simp [ha, hb, he]
    · have he' : ¬ finishWord (-(b : Int)) (-62) false = finishWord (-(a : Int)) (-62) false :=
        fun h => he h.symm
      simp [ha, hb, he, he']

/-- A finite word rounding `S · 2^e` has that rounding as its value. -/
theorem settleInt_spec {lo hi : Int} {a : UInt32} (h : settleInt lo hi = some a) {v : ℝ}
    (hl : (lo : ℝ) * 2 ^ (-62 : Int) ≤ v) (hh : v ≤ (hi : ℝ) * 2 ^ (-62 : Int)) {z : Model Fmt}
    (hz : Model.toReal z = Model.roundAt Fmt v) : toWord z = a := by
  unfold settleInt at h
  dsimp only at h
  split at h
  · rename_i hc
    simp only [Option.some.injEq] at h
    subst h
    simp only [Bool.and_eq_true, bne_iff_ne, ne_eq, decide_eq_true_eq, beq_iff_eq] at hc
    obtain ⟨⟨⟨⟨hlo, hhi⟩, heq⟩, hfin⟩, hsig⟩ := hc
    exact bracket_spec hlo hhi rfl heq.symm hfin hsig hl hh hz
  · simp at h

theorem int_scale (n : Nat) : ((n : Int) : ℝ) * 2 ^ (-62 : Int) = V n := by
  unfold V; push_cast; rw [zpow_neg, zpow_ofNat]; ring

/-- The value of a finite word: `±m · 2^e` from `decodeD`. -/
theorem toReal_ofW_decode {w : UInt32} (hfin : expOf w ≠ 255) :
    Model.toReal (ofW w) =
      (if (decodeD w).negative then -1 else 1) * ((decodeD w).significand * (2 : ℝ) ^ (decodeD w).exponent) := by
  rw [Model.toReal_eq, ofW, toDyadic_ofUInt32 _ hfin]
  simp only [FloatLib.Numerics.Dyadic.toReal, FloatLib.Numerics.Dyadic.cast_signedSignificand]
  ring

/-! ### Sigmoid -/

theorem realSigmoid_neg_eq (C : ℝ) :
    realSigmoid (-C) = Real.exp (-C) / (1 + Real.exp (-C)) := by
  unfold realSigmoid
  rw [neg_neg]
  have hp := Real.exp_pos (-C)
  have h : Real.exp C = 1 / Real.exp (-C) := by rw [Real.exp_neg, one_div, inv_inv]
  rw [h]; field_simp; ring

/-- Fast path for the correctly rounded sigmoid; `none` when it doesn't settle. -/
def sigmoidFast (w : UInt32) : Option UInt32 :=
  if expOf w = 255 then none else
  let d := decodeD w
  match expNeg d.significand d.exponent with
  | none => none
  | some t =>
    if d.negative then
      -- σ(−c) = t / (1 + t), increasing in t
      match divLo t.1 (one + t.1), divHi t.2 (one + t.2) with
      | some ql, some qh => settle false ql qh
      | _, _ => none
    else
      -- σ(c) = 1 / (1 + t), decreasing in t
      match divLo one (one + t.2), divHi one (one + t.1) with
      | some ql, some qh => settle false ql qh
      | _, _ => none

theorem sigmoidFast_spec {w a : UInt32} (h : sigmoidFast w = some a) : sigmoidCR w = a := by
  unfold sigmoidFast at h
  split at h
  · simp at h
  rename_i hfin
  dsimp only at h
  have hx := toReal_ofW_decode hfin
  generalize decodeD w = d at h hx
  have hspec := (toReal_sigmoidModel (fmt := Fmt) (by decide)
    (input := Model.ofUInt32 w) (by simp [isFinite_ofUInt32, hfin])).2
  change Model.toReal (sigmoidModel (ofW w)) = Model.roundAt Fmt (realSigmoid (Model.toReal (ofW w))) at hspec
  rw [hx] at hspec
  change toWord (sigmoidModel (ofW w)) = a
  split at h
  · simp at h
  rename_i t ht
  have hE := expNeg_spec ht
  set C := (d.significand : ℝ) * (2 : ℝ) ^ d.exponent
  set T := Real.exp (-C)
  have hT : 0 < T := Real.exp_pos _
  obtain ⟨h1, h2, h3⟩ := hE
  have v1 := V_nonneg t.1
  have v2 := V_nonneg t.2
  split at h
  · rename_i hneg
    split at h
    · rename_i ql qh hql hqh
      have bl := divLo_spec hql
      have bh := divHi_spec hqh
      rw [V_add, V_one] at bl bh
      rw [settle_eq] at h
      simp only [Bool.false_eq_true, ite_false] at h
      refine settleInt_spec h (v := realSigmoid (-C)) ?_ ?_ (by rw [hspec, hneg]; simp)
      · rw [int_scale]
        have : realSigmoid (-C) = T / (1 + T) := realSigmoid_neg_eq C
        rw [this, le_div_iff₀ (by linarith)]
        nlinarith
      · rw [int_scale]
        have : realSigmoid (-C) = T / (1 + T) := realSigmoid_neg_eq C
        rw [this, div_le_iff₀ (by linarith)]
        nlinarith
    · simp at h
  · rename_i hneg
    split at h
    · rename_i ql qh hql hqh
      have bl := divLo_spec hql
      have bh := divHi_spec hqh
      rw [V_add, V_one] at bl bh
      have hs : realSigmoid C = 1 / (1 + T) := rfl
      rw [settle_eq] at h
      simp only [Bool.false_eq_true, ite_false] at h
      refine settleInt_spec h (v := realSigmoid C) ?_ ?_ (by rw [hspec]; simp [hneg])
      · rw [int_scale, hs, le_div_iff₀ (by linarith)]
        nlinarith
      · rw [int_scale, hs, div_le_iff₀ (by linarith)]
        nlinarith
    · simp at h

/-- `sigmoid`, fast where it settles and the spec elsewhere. -/
def sigmoidF (w : UInt32) : UInt32 := (sigmoidFast w).getD (sigmoidCR w)

theorem sigmoidF_eq : sigmoidF = sigmoidCR := by
  funext w
  unfold sigmoidF
  cases h : sigmoidFast w with
  | none => rfl
  | some a => exact (sigmoidFast_spec h).symm

/-! ### Tanh -/

/-- Fast path for the correctly rounded `tanh`: `tanh |x| = (1 − t)/(1 + t)`, `t = e^(−2|x|)`. -/
def tanhFast (w : UInt32) : Option UInt32 :=
  if expOf w = 255 then none else
  let d := decodeD w
  match expNeg d.significand (d.exponent + 1) with
  | none => none
  | some t =>
    if one < t.1 then none else
    match divLo (one - t.2) (one + t.2), divHi (one - t.1) (one + t.1) with
    | some ql, some qh =>
      settle d.negative ql qh
    | _, _ => none

theorem neg_int_scale (n : Nat) : ((-(n : Int) : Int) : ℝ) * 2 ^ (-62 : Int) = -V n := by
  rw [Int.cast_neg, neg_mul, int_scale]

theorem tanhFast_spec {w a : UInt32} (h : tanhFast w = some a) : tanhCR w = a := by
  unfold tanhFast at h
  split at h
  · simp at h
  rename_i hfin
  dsimp only at h
  have hx := toReal_ofW_decode hfin
  generalize decodeD w = d at h hx
  have hspec := (toReal_tanhModel (fmt := Fmt) (by decide)
    (input := Model.ofUInt32 w) (by simp [isFinite_ofUInt32, hfin])).2
  change Model.toReal (tanhModel (ofW w)) = Model.roundAt Fmt (Real.tanh (Model.toReal (ofW w))) at hspec
  rw [hx] at hspec
  change toWord (tanhModel (ofW w)) = a
  split at h
  · simp at h
  rename_i t ht
  have hE := expNeg_spec ht
  set C := (d.significand : ℝ) * (2 : ℝ) ^ d.exponent
  have h2C : (d.significand : ℝ) * (2 : ℝ) ^ (d.exponent + 1) = 2 * C := by
    rw [zpow_add₀ two_ne_zero]; simp only [C]; ring
  rw [h2C] at hE
  set T := Real.exp (-(2 * C))
  have hT : 0 < T := Real.exp_pos _
  have htanh : Real.tanh C = (1 - T) / (1 + T) := by
    rw [tanh_eq_exp_neg_two]; simp only [T]; ring_nf
  obtain ⟨h1, h2, h3⟩ := hE
  have v1 := V_nonneg t.1
  have v2 := V_nonneg t.2
  split at h
  · simp at h
  rename_i ht1
  replace ht1 := not_lt.mp ht1
  split at h
  · rename_i ql qh hql hqh
    have bl := divLo_spec hql
    have bh := divHi_spec hqh
    rw [V_add, V_one, V_sub_le_of_le h3, V_one] at bl
    rw [V_add, V_one, V_sub_le_of_le ht1, V_one] at bh
    have lo : V ql ≤ Real.tanh C := by
      rw [htanh, le_div_iff₀ (by linarith)]; nlinarith
    have hi : Real.tanh C ≤ V qh := by
      rw [htanh, div_le_iff₀ (by linarith)]; nlinarith
    rw [settle_eq] at h
    cases hneg : d.negative <;> simp only [hneg, ite_true, Bool.false_eq_true, ite_false] at h
    · refine settleInt_spec h (v := Real.tanh C) ?_ ?_ (by rw [hspec]; simp [hneg])
      · rw [int_scale]; exact lo
      · rw [int_scale]; exact hi
    · refine settleInt_spec h (v := Real.tanh (-C)) ?_ ?_ (by rw [hspec, hneg]; simp)
      · rw [neg_int_scale, Real.tanh_neg]; linarith
      · rw [neg_int_scale, Real.tanh_neg]; linarith
  · simp at h

/-- `tanh`, fast where it settles and the spec elsewhere. -/
def tanhF (w : UInt32) : UInt32 := (tanhFast w).getD (tanhCR w)

theorem tanhF_eq : tanhF = tanhCR := by
  funext w
  unfold tanhF
  cases h : tanhFast w with
  | none => rfl
  | some a => exact (tanhFast_spec h).symm

/-! ### Exp, for softmax's arguments (`x ≤ 0`) -/

/-- Fast path for the correctly rounded `exp` at `x ≤ 0`: `e^x = e^(−|x|)`, enclosed by `expNeg`. -/
def expFast (w : UInt32) : Option UInt32 :=
  if expOf w = 255 then none else
  let d := decodeD w
  if !d.negative && d.significand != 0 then none else
  match expNeg d.significand d.exponent with
  | none => none
  | some t => settle false t.1 t.2

theorem expFast_spec {w a : UInt32} (h : expFast w = some a) : expW w = a := by
  unfold expFast at h
  split at h
  · simp at h
  rename_i hfin
  dsimp only at h
  have hx := toReal_ofW_decode hfin
  generalize hd : decodeD w = d at h hx
  split at h
  · simp at h
  rename_i hsign
  simp only [Bool.and_eq_true, Bool.not_eq_true', bne_iff_ne, ne_eq, not_and, not_not] at hsign
  split at h
  · simp at h
  rename_i t ht
  obtain ⟨h1, h2, -⟩ := expNeg_spec ht
  set C := (d.significand : ℝ) * (2 : ℝ) ^ d.exponent
  -- the input is `−C`
  have hxC : Model.toReal (ofW w) = -C := by
    rw [hx]
    by_cases hneg : d.negative = true
    · simp [hneg]
    · have hz : d.significand = 0 := hsign (by simpa using hneg)
      simp [C, hz]
  have hfinw : Model.isFinite (ofW w) = true := by simp [ofW, isFinite_ofUInt32, hfin]
  have hrange : Real.exp (Model.toReal (ofW w)) <
      (Model.Transcendentals.Certified.overflowThreshold Fmt : ℝ) := by
    have hC : 0 ≤ C := by simp only [C]; positivity
    have : Real.exp (-C) ≤ 1 := Real.exp_le_one_iff.mpr (by linarith)
    rw [hxC]
    exact lt_of_le_of_lt this
      (Model.Transcendentals.CorrectlyRounded.one_lt_overflowThreshold (by decide))
  have hspec := Model.Transcendentals.CorrectlyRounded.toReal_exp_eq_roundAt (fmt := Fmt)
    (by decide) hfinw hrange (budget := {})
  rw [hxC] at hspec
  rw [settle_eq] at h
  simp only [Bool.false_eq_true, ite_false] at h
  exact settleInt_spec h (v := Real.exp (-C)) (by rw [int_scale]; exact h1)
    (by rw [int_scale]; exact h2) hspec

/-- `exp`, fast at `x ≤ 0` where it settles and the spec elsewhere. -/
def expF (w : UInt32) : UInt32 := (expFast w).getD (expW w)

theorem expF_eq : expF = expW := by
  funext w
  unfold expF
  cases h : expFast w with
  | none => rfl
  | some a => exact (expFast_spec h).symm

end FastAct


namespace Submission

open ExactRuntime FloatLib.Floats.Formats.BinaryInterchange FloatLib.Floats

/-! ## The entry -/

/-! ## A window dot product: a fast path for short sums

The products are summed on a window `2^Ec` below the largest exponent, each truncated, in 64-bit
words. The exact total then lies within `n` window units of the sum; when both ends round to the
same word, so does the total (`dotWin_spec`). -/

/-- Largest `idxOf a + idxOf b` over the terms, or `none` at a NaN or an infinity. -/
@[specialize] def maxExp (fa fb : Nat → UInt32) (E index : Nat) : Nat → Option Nat
  | 0 => some E
  | count + 1 =>
    let a := fa index
    let b := fb index
    if expOf a = 255 ∨ expOf b = 255 then none
    else maxExp fa fb (max E (idxOf a + idxOf b)) (index + 1) count

theorem maxExp_spec (fa fb : Nat → UInt32) :
    ∀ (count E index E' : Nat), maxExp fa fb E index count = some E' →
      E ≤ E' ∧ ∀ k, index ≤ k → k < index + count →
        expOf (fa k) ≠ 255 ∧ expOf (fb k) ≠ 255 ∧ idxOf (fa k) + idxOf (fb k) ≤ E'
  | 0, E, index, E', h => by
    simp only [maxExp, Option.some.injEq] at h
    exact ⟨h.le, fun k h1 h2 => by omega⟩
  | count + 1, E, index, E', h => by
    simp only [maxExp] at h
    split at h
    · simp at h
    · rename_i hs
      obtain ⟨hE, hall⟩ := maxExp_spec fa fb count _ _ _ h
      push_neg at hs
      refine ⟨le_trans (le_max_left _ _) hE, fun k h1 h2 => ?_⟩
      rcases Nat.lt_or_ge k (index + 1) with hk | hk
      · have : k = index := by omega
        subst this
        exact ⟨hs.1, hs.2, le_trans (le_max_right _ _) hE⟩
      · exact hall k hk (by omega)

/-- One product on the window `2^Ec`, truncated: `⌊t · 2^(e − Ec)⌋`. -/
@[inline] def winTerm (t e Ec : Nat) : Nat := if Ec ≤ e then t <<< (e - Ec) else t >>> (Ec - e)

theorem winTerm_bounds (t e Ec : Nat) :
    winTerm t e Ec * 2 ^ Ec ≤ t * 2 ^ e ∧ t * 2 ^ e < (winTerm t e Ec + 1) * 2 ^ Ec := by
  unfold winTerm
  split
  · rename_i h
    rw [Nat.shiftLeft_eq]
    have : 2 ^ e = 2 ^ (e - Ec) * 2 ^ Ec := by rw [← pow_add]; congr 1; omega
    rw [this]
    constructor
    · rw [Nat.mul_assoc]
    · have : 0 < 2 ^ Ec := Nat.two_pow_pos _
      nlinarith
  · rename_i h
    rw [Nat.shiftRight_eq_div_pow]
    have hp : 2 ^ Ec = 2 ^ (Ec - e) * 2 ^ e := by rw [← pow_add]; congr 1; omega
    have hpos : 0 < 2 ^ e := Nat.two_pow_pos _
    have hq := Nat.div_mul_le_self t (2 ^ (Ec - e))
    have hr := Nat.lt_mul_div_succ t (Nat.two_pow_pos (Ec - e))
    rw [hp]
    constructor
    · calc t / 2 ^ (Ec - e) * (2 ^ (Ec - e) * 2 ^ e)
          = t / 2 ^ (Ec - e) * 2 ^ (Ec - e) * 2 ^ e := by ring
        _ ≤ t * 2 ^ e := Nat.mul_le_mul_right _ hq
    · calc t * 2 ^ e < 2 ^ (Ec - e) * (t / 2 ^ (Ec - e) + 1) * 2 ^ e :=
            Nat.mul_lt_mul_of_pos_right hr hpos
        _ = (t / 2 ^ (Ec - e) + 1) * (2 ^ (Ec - e) * 2 ^ e) := by ring

/-- The window sums by sign, in natural numbers (the reference for `sumWin64`). -/
def sumNat (fa fb : Nat → UInt32) (Ec index : Nat) : Nat → Nat × Nat
  | 0 => (0, 0)
  | count + 1 =>
    let a := fa index
    let b := fb index
    let q := winTerm (mantOf a * mantOf b) (idxOf a + idxOf b) Ec
    let r := sumNat fa fb Ec (index + 1) count
    if signOf a != signOf b then (r.1, r.2 + q) else (r.1 + q, r.2)

/-- The exact total lies within `count` window units of `P − N`. -/
theorem sumNat_bounds (fa fb : Nat → UInt32) (A B : Array UInt32) (n Ec : Nat)
    (hA : ∀ k, k < n → A[k]! = fa k) (hB : ∀ k, k < n → B[k]! = fb k) :
    ∀ (count index : Nat), index + count ≤ n →
      (((sumNat fa fb Ec index count).1 : Int) - (sumNat fa fb Ec index count).2 - count) * 2 ^ Ec
        ≤ dsum A B index count ∧
      dsum A B index count ≤
        (((sumNat fa fb Ec index count).1 : Int) - (sumNat fa fb Ec index count).2 + count) * 2 ^ Ec
  | 0, index, _ => by simp [sumNat, dsum]
  | count + 1, index, h => by
    obtain ⟨ih1, ih2⟩ := sumNat_bounds fa fb A B n Ec hA hB count (index + 1) (by omega)
    simp only [sumNat, dsum]
    rw [hA index (by omega), hB index (by omega)]
    set a := fa index
    set b := fb index
    set r := sumNat fa fb Ec (index + 1) count
    obtain ⟨w1, w2⟩ := winTerm_bounds (mantOf a * mantOf b) (idxOf a + idxOf b) Ec
    set q := winTerm (mantOf a * mantOf b) (idxOf a + idxOf b) Ec
    have hsv := Pinned.sv_mul a b
    have c1 : ((q * 2 ^ Ec : Nat) : Int) ≤ ((mantOf a * mantOf b * 2 ^ (idxOf a + idxOf b) : Nat) : Int) := by
      exact_mod_cast w1
    have c2 : ((mantOf a * mantOf b * 2 ^ (idxOf a + idxOf b) : Nat) : Int) < (((q + 1) * 2 ^ Ec : Nat) : Int) := by
      exact_mod_cast w2
    push_cast at c1 c2 ih1 ih2 ⊢
    split
    · rename_i hs
      have : sv a * sv b = -((mantOf a * mantOf b : Nat) : Int) := by
        rw [← hsv]; simp [show Bool.xor (signOf a) (signOf b) = true by simpa [bne_iff_ne] using hs]
      push_cast at this
      rw [this]
      push_cast
      constructor <;> (ring_nf; ring_nf at ih1 ih2 c1 c2; linarith)
    · rename_i hs
      have : sv a * sv b = ((mantOf a * mantOf b : Nat) : Int) := by
        rw [← hsv]; simp [show Bool.xor (signOf a) (signOf b) = false by simpa [bne_iff_ne] using hs]
      push_cast at this
      rw [this]
      push_cast
      constructor <;> (ring_nf; ring_nf at ih1 ih2 c1 c2; linarith)

/-- `winTerm` in a 64-bit word. -/
@[inline] def winTermW (a b : UInt32) (Ec : UInt64) : UInt64 :=
  let t := (mantOf a).toUInt64 * (mantOf b).toUInt64
  let e := (idxOf a + idxOf b).toUInt64
  if Ec ≤ e then t <<< (e - Ec) else if Ec - e < 64 then t >>> (Ec - e) else 0

/-- The window sums in 64-bit words. -/
@[specialize] def sumWin64 (fa fb : Nat → UInt32) (Ec : UInt64) (P N : UInt64) (index : Nat) :
    Nat → UInt64 × UInt64
  | 0 => (P, N)
  | count + 1 =>
    let a := fa index
    let b := fb index
    let q := winTermW a b Ec
    if signOf a != signOf b then sumWin64 fa fb Ec P (N + q) (index + 1) count
    else sumWin64 fa fb Ec (P + q) N (index + 1) count

/-- One window term in a word equals `winTerm`, below `2^(48 + c)`, when the exponent is at most
`c ≤ 15` above the window. -/
theorem winTermW_eq (a b : UInt32) (Ec c : Nat) (hc : c ≤ 15) (hEc : Ec ≤ 600)
    (he : idxOf a + idxOf b ≤ Ec + c) :
    (winTermW a b Ec.toUInt64).toNat = winTerm (mantOf a * mantOf b) (idxOf a + idxOf b) Ec ∧
    winTerm (mantOf a * mantOf b) (idxOf a + idxOf b) Ec < 2 ^ (48 + c) := by
  have ha := mantOf_lt_24 a
  have hb := mantOf_lt_24 b
  have hab : mantOf a * mantOf b < 2 ^ 48 := by
    calc mantOf a * mantOf b < 2 ^ 24 * 2 ^ 24 := Nat.mul_lt_mul'' ha hb
      _ = 2 ^ 48 := by norm_num
  have ht : ((mantOf a).toUInt64 * (mantOf b).toUInt64).toNat = mantOf a * mantOf b := by
    rw [UInt64.toNat_mul, FastAct.toNat_toUInt64_of_lt (by omega),
      FastAct.toNat_toUInt64_of_lt (by omega)]
    exact Nat.mod_eq_of_lt (by omega)
  have hix : idxOf a + idxOf b ≤ 615 := by omega
  have heN : (idxOf a + idxOf b).toUInt64.toNat = idxOf a + idxOf b :=
    FastAct.toNat_toUInt64_of_lt (by omega)
  have hEN : Ec.toUInt64.toNat = Ec := FastAct.toNat_toUInt64_of_lt (by omega)
  unfold winTermW winTerm
  dsimp only
  by_cases h1 : Ec ≤ idxOf a + idxOf b
  · have hle : Ec.toUInt64 ≤ (idxOf a + idxOf b).toUInt64 := by
      rw [UInt64.le_iff_toNat_le, hEN, heN]; exact h1
    simp only [hle, ite_true, h1]
    have hd : ((idxOf a + idxOf b).toUInt64 - Ec.toUInt64).toNat = idxOf a + idxOf b - Ec := by
      rw [UInt64.toNat_sub_of_le _ _ hle, heN, hEN]
    have hsh : idxOf a + idxOf b - Ec ≤ c := by omega
    have hlt : mantOf a * mantOf b * 2 ^ (idxOf a + idxOf b - Ec) < 2 ^ (48 + c) := by
      calc mantOf a * mantOf b * 2 ^ (idxOf a + idxOf b - Ec)
          < 2 ^ 48 * 2 ^ (idxOf a + idxOf b - Ec) :=
            Nat.mul_lt_mul_of_pos_right hab (Nat.two_pow_pos _)
        _ ≤ 2 ^ 48 * 2 ^ c := Nat.mul_le_mul_left _ (Nat.pow_le_pow_right (by norm_num) hsh)
        _ = 2 ^ (48 + c) := by rw [← pow_add]
    have h63 : 2 ^ (48 + c) ≤ 2 ^ 63 := Nat.pow_le_pow_right (by norm_num) (by omega)
    rw [UInt64.toNat_shiftLeft, ht, hd, Nat.shiftLeft_eq, Nat.shiftLeft_eq,
      Nat.mod_eq_of_lt (by omega : idxOf a + idxOf b - Ec < 64)]
    exact ⟨Nat.mod_eq_of_lt (by omega), hlt⟩
  · have hle : ¬ Ec.toUInt64 ≤ (idxOf a + idxOf b).toUInt64 := by
      rw [UInt64.le_iff_toNat_le, hEN, heN]; exact h1
    simp only [hle, ite_false, h1]
    have hd : (Ec.toUInt64 - (idxOf a + idxOf b).toUInt64).toNat = Ec - (idxOf a + idxOf b) := by
      rw [UInt64.toNat_sub_of_le _ _ (by rw [UInt64.le_iff_toNat_le, hEN, heN]; omega), heN, hEN]
    have hsmall : (mantOf a * mantOf b) >>> (Ec - (idxOf a + idxOf b)) < 2 ^ (48 + c) := by
      rw [Nat.shiftRight_eq_div_pow]
      calc mantOf a * mantOf b / 2 ^ (Ec - (idxOf a + idxOf b)) ≤ mantOf a * mantOf b :=
            Nat.div_le_self _ _
        _ < 2 ^ 48 := hab
        _ ≤ 2 ^ (48 + c) := Nat.pow_le_pow_right (by norm_num) (by omega)
    refine ⟨?_, hsmall⟩
    split
    · rename_i h64
      rw [UInt64.lt_iff_toNat_lt, hd] at h64
      rw [UInt64.toNat_shiftRight, ht, hd, Nat.mod_eq_of_lt (by simpa using h64)]
    · rename_i h64
      rw [UInt64.lt_iff_toNat_lt, hd] at h64
      simp only [UInt64.toNat_zero]
      rw [Nat.shiftRight_eq_div_pow, eq_comm, Nat.div_eq_zero_iff]
      right
      calc mantOf a * mantOf b < 2 ^ 48 := hab
        _ ≤ 2 ^ (Ec - (idxOf a + idxOf b)) :=
          Nat.pow_le_pow_right (by norm_num) (by simp at h64; omega)

theorem sumWin64_eq (fa fb : Nat → UInt32) (Ec c : Nat) (hc : c ≤ 15) (hEc : Ec ≤ 600) :
    ∀ (count index : Nat) (P N : UInt64),
      (∀ k, index ≤ k → k < index + count → idxOf (fa k) + idxOf (fb k) ≤ Ec + c) →
      P.toNat + N.toNat + count * 2 ^ (48 + c) < 2 ^ 64 →
      (sumWin64 fa fb Ec.toUInt64 P N index count).1.toNat =
          P.toNat + (sumNat fa fb Ec index count).1 ∧
        (sumWin64 fa fb Ec.toUInt64 P N index count).2.toNat =
          N.toNat + (sumNat fa fb Ec index count).2
  | 0, index, P, N, _, _ => by simp [sumWin64, sumNat]
  | count + 1, index, P, N, he, hb => by
    obtain ⟨hq, hqlt⟩ := winTermW_eq (fa index) (fb index) Ec c hc hEc (he index le_rfl (by omega))
    simp only [sumWin64, sumNat]
    have hpow : count * 2 ^ (48 + c) + 2 ^ (48 + c) = (count + 1) * 2 ^ (48 + c) := by ring
    split
    · have hN : (N + winTermW (fa index) (fb index) Ec.toUInt64).toNat =
          N.toNat + winTerm (mantOf (fa index) * mantOf (fb index))
            (idxOf (fa index) + idxOf (fb index)) Ec := by
        rw [UInt64.toNat_add, hq]; exact Nat.mod_eq_of_lt (by omega)
      obtain ⟨r1, r2⟩ := sumWin64_eq fa fb Ec c hc hEc count (index + 1) P _
        (fun k h1 h2 => he k (by omega) (by omega)) (by rw [hN]; omega)
      refine ⟨r1, ?_⟩
      rw [r2, hN]; ring
    · have hP : (P + winTermW (fa index) (fb index) Ec.toUInt64).toNat =
          P.toNat + winTerm (mantOf (fa index) * mantOf (fb index))
            (idxOf (fa index) + idxOf (fb index)) Ec := by
        rw [UInt64.toNat_add, hq]; exact Nat.mod_eq_of_lt (by omega)
      obtain ⟨r1, r2⟩ := sumWin64_eq fa fb Ec c hc hEc count (index + 1) _ N
        (fun k h1 h2 => he k (by omega) (by omega)) (by rw [hP]; omega)
      refine ⟨?_, r2⟩
      rw [r1, hP]; ring

/-! ### `Pinned.dotFn` on finite inputs -/

theorem dotLoop_special_false (fa fb : Nat → UInt32) :
    ∀ (count index : Nat) (pos neg : Array Nat) (nz : Bool),
      (∀ k, index ≤ k → k < index + count → expOf (fa k) ≠ 255 ∧ expOf (fb k) ≠ 255) →
      (Pinned.dotLoop fa fb pos neg false nz index count).special = false
  | 0, _, _, _, _, _ => by simp [Pinned.dotLoop]
  | count + 1, index, pos, neg, nz, hfin => by
    have h := hfin index le_rfl (by omega)
    simp only [Pinned.dotLoop]
    have hstep : (Pinned.dotStep ⟨pos, neg, false, nz⟩ (fa index) (fb index)).special = false := by
      unfold Pinned.dotStep
      rw [if_neg (by push_neg; exact h)]
      simp only [Pinned.Acc.add]
      split_ifs <;> rfl
    generalize Pinned.dotStep ⟨pos, neg, false, nz⟩ (fa index) (fb index) = acc at hstep
    obtain ⟨p, n, sp, z⟩ := acc
    simp only at hstep
    subst hstep
    exact dotLoop_special_false fa fb count (index + 1) p n z
      (fun k h1 h2 => hfin k (by omega) (by omega))

/-- On finite inputs, `Pinned.dotFn` rounds the exact total once. -/
theorem dotFn_finite (n : Nat) (fa fb : Nat → UInt32)
    (hfin : ∀ k, k < n → expOf (fa k) ≠ 255 ∧ expOf (fb k) ≠ 255) :
    Pinned.dotFn n fa fb =
      finishWord (dsum (Array.ofFn (n := n) fun i => fa i) (Array.ofFn (n := n) fun i => fb i) 0 n)
        (-298) (n ≠ 0 && dallNZ (Array.ofFn (n := n) fun i => fa i)
          (Array.ofFn (n := n) fun i => fb i) 0 n) := by
  unfold Pinned.dotFn
  have hsp := dotLoop_special_false fa fb n 0 (Array.replicate 18 0) (Array.replicate 18 0) true
    (fun k _ hk => hfin k (by omega))
  obtain ⟨-, -, hD, hz⟩ := Pinned.dotLoopFacts fa fb (Array.ofFn (n := n) fun i => fa i)
    (Array.ofFn (n := n) fun i => fb i) n (Pinned.getElem!_ofFn n fa) (Pinned.getElem!_ofFn n fb) Pinned.Acc.empty 0 n
    (by omega) (by simp [Pinned.Acc.empty]) (by simp [Pinned.Acc.empty]) hsp
  simp only [Pinned.Acc.empty] at hD hz hsp
  simp only [hsp, Bool.false_eq_true, ite_false]
  rw [Pinned.finishFast_eq]
  change finishWord (Pinned.Acc.total _) _ _ = _
  have h0 : (⟨Array.replicate 18 0, Array.replicate 18 0, false, true⟩ : Pinned.Acc).total = 0 :=
    Pinned.Acc.total_empty
  rw [hD, hz, h0]
  simp

/-! ### The window fast path -/

/-- The dot product on a window, when both ends of its enclosure round to the same finite nonzero
word below `2^127`; `none` otherwise. -/
@[inline] def dotWin (n : Nat) (fa fb : Nat → UInt32) : Option UInt32 :=
  match maxExp fa fb 0 0 n with
  | none => none
  | some E =>
    if 600 < E ∨ 12 < n.log2 then none else
    let c := 13 - n.log2
    let Ec := E - c
    let pn := sumWin64 fa fb Ec.toUInt64 0 0 0 n
    let P := pn.1.toNat
    let N := pn.2.toNat
    let neg := decide (P < N)
    let D := if neg then N - P else P - N
    if D ≤ n then none else
    let ex : Int := (Ec : Int) - 298
    let w := FastRound.roundW neg (D - n) ex
    if w = FastRound.roundW neg (D + n) ex ∧ ((D + n).log2 : Int) + ex ≤ 126 ∧
        expOf w ≠ 255 ∧ (decodeD w).significand ≠ 0 then some w else none

theorem two_pow_127_le_posMax :
    (2 : ℝ) ^ (127 : Int) ≤ Model.toReal (Model.posMaxFinite Fmt) := by
  rw [Model.toReal_posMaxFinite]
  have h1 : Fmt.fracWidth = 23 := rfl
  have h2 : Fmt.maxNormalExponent = 127 := rfl
  have h3 : Fmt.maxFiniteFracField = 2 ^ 23 - 1 := rfl
  have hp : Model.pow2 23 = 2 ^ 23 := by simp [Model.pow2, Nat.shiftLeft_eq]
  rw [h1, h2, h3, hp]
  simp [FloatLib.Floats.Formats.Flocq.bpow, FloatLib.Numerics.binaryRadix,
    FloatLib.Numerics.Radix.toReal]
  norm_num

/-- `X · 2^(Ec − 298) = (X · 2^Ec) · 2^(−298)`. -/
theorem scale_ex (X : Int) (Ec : Nat) :
    (X : ℝ) * 2 ^ ((Ec : Int) - 298) = ((X * 2 ^ Ec : Int) : ℝ) * 2 ^ (-298 : Int) := by
  rw [zpow_sub₀ two_ne_zero, zpow_neg, zpow_natCast]; push_cast; ring

theorem scale_le {X Y : Int} (h : X ≤ Y) : (X : ℝ) * 2 ^ (-298 : Int) ≤ (Y : ℝ) * 2 ^ (-298 : Int) :=
  mul_le_mul_of_nonneg_right (by exact_mod_cast h) (by positivity)

/-- What the window path returns is `Pinned.dotFn`'s word. -/
theorem dotWin_spec {n : Nat} {fa fb : Nat → UInt32} {w : UInt32} (h : dotWin n fa fb = some w) :
    Pinned.dotFn n fa fb = w := by
  unfold dotWin at h
  split at h
  · simp at h
  rename_i E hE
  split at h
  · simp at h
  rename_i hg
  push_neg at hg
  dsimp only at h
  obtain ⟨-, hall⟩ := maxExp_spec fa fb n 0 0 E hE
  have hfin : ∀ k, k < n → expOf (fa k) ≠ 255 ∧ expOf (fb k) ≠ 255 :=
    fun k hk => ⟨(hall k (by omega) (by omega)).1, (hall k (by omega) (by omega)).2.1⟩
  rw [dotFn_finite n fa fb hfin]
  set c := 13 - n.log2 with hc
  set Ec := E - c with hEc
  -- the 64-bit sums are the natural ones
  have hn2 : n < 2 ^ (n.log2 + 1) := Nat.lt_log2_self
  have hbound : (0 : UInt64).toNat + (0 : UInt64).toNat + n * 2 ^ (48 + c) < 2 ^ 64 := by
    have : n * 2 ^ (48 + c) < 2 ^ (n.log2 + 1) * 2 ^ (48 + c) :=
      Nat.mul_lt_mul_of_pos_right hn2 (Nat.two_pow_pos _)
    have h62 : 2 ^ (n.log2 + 1) * 2 ^ (48 + c) = 2 ^ 62 := by rw [← pow_add]; congr 1; omega
    simp; omega
  obtain ⟨hP, hN⟩ := sumWin64_eq fa fb Ec c (by omega) (by omega) n 0 0 0
    (fun k _ hk => by have := (hall k (by omega) hk).2.2; omega) hbound
  simp only [UInt64.toNat_zero, Nat.zero_add] at hP hN
  rw [hP, hN] at h
  set A := Array.ofFn (n := n) fun i => fa i
  set B := Array.ofFn (n := n) fun i => fb i
  obtain ⟨b1, b2⟩ := sumNat_bounds fa fb A B n Ec (Pinned.getElem!_ofFn n fa) (Pinned.getElem!_ofFn n fb) n 0
    (by omega)
  set T := dsum A B 0 n
  set Pn := (sumNat fa fb Ec 0 n).1
  set Nn := (sumNat fa fb Ec 0 n).2
  have hpos : (0 : Int) < 2 ^ Ec := by positivity
  set ex : Int := (Ec : Int) - 298 with hex
  set v : ℝ := (T : ℝ) * 2 ^ (-298 : Int)
  have hrepr : (ofIntDyadic T (-298)).toReal = v := by
    rw [← FloatLib.Numerics.Dyadic.cast_toRat, toRat_ofIntDyadic]; push_cast; rfl
  -- a magnitude `M` with `log2 M + ex ≤ 126` is below `2^127` on the scale `2^ex`
  have habs : ∀ M : Nat, M.log2 + ex ≤ 126 → (M : ℝ) * 2 ^ ex ≤ 2 ^ (127 : Int) := by
    intro M hl
    have hlt : M < 2 ^ (M.log2 + 1) := Nat.lt_log2_self
    have : (M : ℝ) ≤ (2 : ℝ) ^ ((M.log2 + 1 : Nat) : Int) := by
      rw [zpow_natCast]; exact_mod_cast hlt.le
    calc (M : ℝ) * 2 ^ ex ≤ 2 ^ ((M.log2 + 1 : Nat) : Int) * 2 ^ ex :=
          mul_le_mul_of_nonneg_right this (by positivity)
      _ = 2 ^ (((M.log2 + 1 : Nat) : Int) + ex) := by rw [zpow_add₀ two_ne_zero]
      _ ≤ 2 ^ (127 : Int) := zpow_le_zpow_right₀ (by norm_num) (by push_cast; omega)
  -- the bridge, once the ends are known
  have hfinish : ∀ (lo hi : Int) (M : Nat), lo ≠ 0 → hi ≠ 0 →
      finishWord lo ex false = w → finishWord hi ex false = w →
      lo * 2 ^ Ec ≤ T → T ≤ hi * 2 ^ Ec → -(M : Int) ≤ lo → hi ≤ M → M.log2 + ex ≤ 126 →
      expOf w ≠ 255 → (decodeD w).significand ≠ 0 → T ≠ 0 →
      finishWord T (-298) (n ≠ 0 && dallNZ A B 0 n) = w := by
    intro lo hi M hlo hhi f1 f2 l1 l2 m1 m2 hM g1 g2 hT
    have hv_lo : (lo : ℝ) * 2 ^ ex ≤ v := by rw [hex, scale_ex]; exact scale_le l1
    have hv_hi : v ≤ (hi : ℝ) * 2 ^ ex := by rw [hex, scale_ex]; exact scale_le l2
    have hp : (0 : ℝ) < 2 ^ ex := by positivity
    have m1' : -((M : ℝ) * 2 ^ ex) ≤ (lo : ℝ) * 2 ^ ex := by
      rw [← neg_mul]; exact mul_le_mul_of_nonneg_right (by exact_mod_cast m1) hp.le
    have m2' : (hi : ℝ) * 2 ^ ex ≤ (M : ℝ) * 2 ^ ex :=
      mul_le_mul_of_nonneg_right (by exact_mod_cast m2) hp.le
    have hvb : |v| ≤ 2 ^ (127 : Int) :=
      (abs_le.mpr ⟨m1'.trans hv_lo, hv_hi.trans m2'⟩).trans (habs M hM)
    have hzfin : Model.isFinite (Model.roundDyadic Fmt (ofIntDyadic T (-298))) = true :=
      Model.isFinite_roundDyadic_of_isIEEE_of_abs_toReal_le_posMaxFinite Fmt (by decide) _
        (by rw [hrepr]; exact hvb.trans two_pow_127_le_posMax)
    have hz : Model.toReal (Model.roundDyadic Fmt (ofIntDyadic T (-298))) = Model.roundAt Fmt v := by
      rw [Model.toReal_roundDyadic_eq_roundAt Fmt (by decide) _ hzfin, hrepr]
    have := FastRound.bracket_spec hlo hhi f1 f2 g1 g2 hv_lo hv_hi hz
    simp only [finishWord, hT, ite_false]
    exact this
  by_cases hneg : Pn < Nn
  · simp only [hneg, decide_true, ite_true] at h
    split at h
    · simp at h
    rename_i hDn
    split at h
    · rename_i hc
      simp only [Option.some.injEq] at h
      subst h
      obtain ⟨hw2, hlog, hfw, hsg⟩ := hc
      have hD : ((Nn - Pn : Nat) : Int) = Nn - Pn := by omega
      apply hfinish (-((Nn - Pn + n : Nat) : Int)) (-((Nn - Pn - n : Nat) : Int)) (Nn - Pn + n)
        (by omega) (by omega)
      · rw [hw2, FastRound.roundW_signed true _ _ (by omega)]; simp
      · rw [FastRound.roundW_signed true _ _ (by omega)]; simp
      · push_cast; rw [Nat.cast_sub (by omega)]; push_cast; nlinarith
      · push_cast; rw [Nat.cast_sub (by omega)]; push_cast; nlinarith
      · omega
      · omega
      · exact hlog
      · exact hfw
      · exact hsg
      · intro h0
        have : T ≤ ((Pn : Int) - Nn + n) * 2 ^ Ec := b2
        have hp' : (Pn : Int) - Nn + n < 0 := by omega
        nlinarith [mul_neg_of_neg_of_pos hp' hpos]
    · simp at h
  · simp only [hneg, decide_false, Bool.false_eq_true, ite_false] at h
    split at h
    · simp at h
    rename_i hDn
    split at h
    · rename_i hc
      simp only [Option.some.injEq] at h
      subst h
      obtain ⟨hw2, hlog, hfw, hsg⟩ := hc
      apply hfinish ((Pn - Nn - n : Nat) : Int) ((Pn - Nn + n : Nat) : Int) (Pn - Nn + n)
        (by omega) (by omega)
      · rw [FastRound.roundW_signed false _ _ (by omega)]; simp
      · rw [hw2, FastRound.roundW_signed false _ _ (by omega)]; simp
      · rw [Nat.cast_sub (by omega), Nat.cast_sub (by omega)]; nlinarith
      · push_cast; rw [Nat.cast_sub (by omega)]; nlinarith
      · omega
      · omega
      · exact hlog
      · exact hfw
      · exact hsg
      · intro h0
        have : ((Pn : Int) - Nn - n) * 2 ^ Ec ≤ T := b1
        have hp' : (0 : Int) < (Pn : Int) - Nn - n := by omega
        nlinarith [mul_pos hp' hpos]
    · simp at h

/-- The dot product: the window path where it settles, `Pinned.dotFn` elsewhere. -/
@[inline] def dotW (n : Nat) (fa fb : Nat → UInt32) : UInt32 :=
  (dotWin n fa fb).getD (Pinned.dotFn n fa fb)

theorem dotW_eq : dotW = Pinned.dotFn := by
  funext n fa fb
  unfold dotW
  cases h : dotWin n fa fb with
  | none => rfl
  | some w => exact (dotWin_spec h).symm

/-- The window path reads every operand twice. Two-term dots stay with `Pinned.dotFn`; so do the LSTM and
GRU kernels, whose operands are computed per read (measured slower with the window path). -/
@[inline] def dotSel (n : Nat) (fa fb : Nat → UInt32) : UInt32 :=
  if n ≤ 2 then Pinned.dotFn n fa fb else dotW n fa fb

theorem dotSel_eq : dotSel = Pinned.dotFn := by
  funext n fa fb
  unfold dotSel
  split <;> simp [dotW_eq]

/-- `softmaxWith` with the exponential as a parameter (the same definition). -/
@[specialize] def softmaxExp (dot : DotFn) (ex : Word → Word) (rows n : Nat) (x : Array Word) :
    Array Word :=
  let mx := Array.ofFn (n := rows) fun r => rowMax n x r.val
  let e := Array.ofFn (n := rows * n) fun q =>
    ex (toWord (Model.sub (ofW x[q.val]!) (ofW mx[q.val / n]!)))
  let s := Array.ofFn (n := rows) fun r => dot n (fun t => e[r.val * n + t]!) (fun _ => oneW)
  Array.ofFn (n := rows * n) fun q => toWord (Model.div (ofW e[q.val]!) (ofW s[q.val / n]!))

theorem softmaxExp_expW (dot : DotFn) : softmaxExp dot expW = softmaxWith dot := rfl

/-- SiLU with the fast sigmoid. -/
def siluF (x : Array Word) : Array Word :=
  x.map fun w => toWord (Model.mul (ofW w) (ofW (FastAct.sigmoidF w)))

theorem siluF_eq : siluF = siluSpec := by
  funext x
  simp only [siluF, siluSpec, FastAct.sigmoidF_eq]

/-- Exact 1x1 convolution fast path; all other shapes use the generic exact implementation. -/
def pointwiseConv (s : ConvShape) (relu : Bool) (inp wts b : Array Word) : Array Word :=
  if s.kh = 1 ∧ s.kw = 1 ∧ s.strideH = 1 ∧ s.strideW = 1 ∧
      s.padT = 0 ∧ s.padL = 0 ∧ s.padB = 0 ∧ s.padR = 0 ∧
      s.dilH = 1 ∧ s.dilW = 1 ∧ s.groups = 1 then
    Array.ofFn (n := s.cout * (s.ho * s.wo)) fun q =>
      let o := q.val / (s.ho * s.wo)
      let y := q.val / s.wo % s.ho
      let x := q.val % s.wo
      act relu <| dotSel (s.cin + 1)
        (fun t => if t < s.cin then
          if y >= s.h ∨ x >= s.w then 0 else inp[(t * s.h + y) * s.w + x]!
          else oneW)
        (fun t => if t < s.cin then wts[o * s.cin + t]! else b[o]!)
  else conv2dWith Pinned.dotFn s relu inp wts b

theorem pointwiseConv_eq : pointwiseConv = conv2dWith Pinned.dotFn := by
  funext s relu inp wts b
  unfold pointwiseConv
  split_ifs with h
  · rcases h with ⟨hkh, hkw, hsh, hsw, hpt, hpl, hpb, hpr, hdh, hdw, hgrp⟩
    have htaps : s.taps = s.cin := by simp [ConvShape.taps, ConvShape.cpg, hgrp, hkh, hkw]
    have hopg : s.opg = s.cout := by simp [ConvShape.opg, hgrp]
    apply Array.ext (by simp [conv2dWith, ConvShape.taps, ConvShape.cpg, hgrp, hkh, hkw])
    intro q hq1 hq2
    have hp : 0 < s.ho * s.wo := by
      have hh : 0 < s.ho := by simp [ConvShape.ho]
      have hw : 0 < s.wo := by simp [ConvShape.wo]
      exact Nat.mul_pos hh hw
    have hq : q < s.cout * (s.ho * s.wo) := by
      simpa [conv2dWith, ConvShape.taps, ConvShape.cpg, hgrp, hkh, hkw] using hq1
    have hqout : q / (s.ho * s.wo) < s.cout :=
      (Nat.div_lt_iff_lt_mul hp).2 hq
    have hgroup : q / (s.ho * s.wo) / s.cout = 0 := Nat.div_eq_of_lt hqout
    simp [conv2dWith, windowAt, hkh, hkw, hsh, hsw, hpt, hpl, hpb, hpr, hdh, hdw,
      ConvShape.cpg, hgrp, htaps, hopg, hgroup, Nat.mod_one, dotSel_eq]
  · simpa only [dotSel_eq]

/-- For a depthwise layer, every output group has exactly one output channel. -/
def convFast (s : ConvShape) (relu : Bool) (inp wts b : Array Word) : Array Word :=
  if 0 < s.cin ∧ s.groups = s.cin ∧ s.cout = s.cin then
    Array.ofFn (n := s.cout * (s.ho * s.wo)) fun q =>
      let o := q.val / (s.ho * s.wo)
      let y := q.val / s.wo % s.ho
      let x := q.val % s.wo
      act relu <| Pinned.dotFn (s.taps + 1)
        (fun t => if t < s.taps then windowAt s inp o y x t else oneW)
        (fun t => if t < s.taps then wts[o * s.taps + t]! else b[o]!)
  else pointwiseConv s relu inp wts b

theorem convFast_eq : convFast = conv2dWith Pinned.dotFn := by
  funext s relu inp wts b
  unfold convFast
  split_ifs with h
  · rcases h with ⟨hcin, hgroups, hcout⟩
    have hcpg : s.cpg = 1 := by
      simp [ConvShape.cpg, hgroups, Nat.div_self hcin]
    have hopg : s.opg = 1 := by
      simp [ConvShape.opg, hcout, hgroups, Nat.div_self hcin]
    apply Array.ext (by simp [conv2dWith])
    intro q hq1 hq2
    simp [conv2dWith, hopg, dotSel_eq]
  · simpa only [pointwiseConv_eq, dotSel_eq]

/-- The entry: the pinned season-1 kernels, with fast `attn` and `rnn` families. -/
def kernels : Kernels :=
  { Pinned.kernels with
    conv := { Pinned.kernels.conv with conv2d := convFast }
    attn := ⟨scoresWith dotSel, mixWith dotSel, softmaxExp dotSel FastAct.expF, siluF⟩
    rnn := ⟨lstmWith Pinned.dotFn FastAct.sigmoidF FastAct.tanhF,
      gruWith Pinned.dotFn FastAct.sigmoidF FastAct.tanhF⟩ }

theorem correct : Obligation kernels := by
  have h := Pinned.correct
  simp only [Obligation, Pinned.kernels] at h
  simp only [Obligation, kernels, Pinned.kernels, siluF_eq, FastAct.sigmoidF_eq, FastAct.tanhF_eq,
    FastAct.expF_eq, softmaxExp_expW, dotSel_eq, convFast_eq, Pinned.dotFn_eq_dotOf]
  rw [← h]
  simp only [specAttn, specRnn, scoresSpec, mixSpec, softmaxSpec, siluSpec, lstmSpec, gruSpec,
    Pinned.dotFn_eq_dotOf]

end Submission
