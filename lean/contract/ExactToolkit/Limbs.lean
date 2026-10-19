import Mathlib

/-!
# An exact accumulator in 32-bit limbs

The accumulator is an array of natural numbers, limb `i` weighing `2^(32·i)`. Adding
`m · 2^idx` splits `m · 2^(idx % 32)` into its low 32 bits (limb `idx / 32`) and the rest (the
next limb). Limbs never carry, so each add is two array updates; the value is recovered once at
the end with `limbVal`.
-/

namespace ExactRuntime

/-- The value of a limb list: `a₀ + 2^32·a₁ + 2^64·a₂ + …`. -/
def limbVal : List Nat → Nat
  | [] => 0
  | a :: as => a + 2 ^ 32 * limbVal as

/-- The same value, computed from the array with one right fold (the kernel's final step). A
shift, not `2 ^ 32 * acc`: the compiled power and multiply cost about three times as much. -/
@[inline] def limbValA (a : Array Nat) : Nat := a.foldr (fun x acc => (acc <<< 32) + x) 0

theorem limbValA_eq (a : Array Nat) : limbValA a = limbVal a.toList := by
  unfold limbValA
  rw [← Array.foldr_toList]
  induction a.toList with
  | nil => rfl
  | cons x xs ih => rw [List.foldr_cons, ih, limbVal, Nat.shiftLeft_eq]; ring

theorem limbVal_modify (l : List Nat) (i a : Nat) (hi : i < l.length) :
    limbVal (l.modify i (· + a)) = limbVal l + a * 2 ^ (32 * i) := by
  induction l generalizing i with
  | nil => simp at hi
  | cons x xs ih =>
    cases i with
    | zero => simp [limbVal]; ring
    | succ k =>
      simp only [List.modify_succ_cons, limbVal]
      rw [ih k (by simpa using hi)]
      ring

/-- Add `m · 2^idx` to the accumulator. -/
@[inline] def addLimbs (a : Array Nat) (m idx : Nat) : Array Nat :=
  let l := idx / 32
  let r := idx % 32
  (a.modify l (· + ((m % (1 <<< (32 - r))) <<< r))).modify (l + 1) (· + (m >>> (32 - r)))

theorem addLimbs_val (a : Array Nat) (m idx : Nat) (h : idx / 32 + 1 < a.size) :
    limbVal (addLimbs a m idx).toList = limbVal a.toList + m * 2 ^ idx := by
  simp only [addLimbs, Array.toList_modify, Nat.one_shiftLeft]
  rw [limbVal_modify _ _ _ (by simpa using h), limbVal_modify _ _ _ (by simp; omega)]
  rw [Nat.shiftLeft_eq, Nat.shiftRight_eq_div_pow]
  have hr : idx % 32 < 32 := Nat.mod_lt _ (by norm_num)
  have hidx : idx = 32 * (idx / 32) + idx % 32 := (Nat.div_add_mod idx 32).symm
  set r := idx % 32
  set q := idx / 32
  have hsplit : m = m % 2 ^ (32 - r) + 2 ^ (32 - r) * (m / 2 ^ (32 - r)) :=
    (Nat.mod_add_div m _).symm
  have h32 : 2 ^ (32 - r) * 2 ^ r = 2 ^ 32 := by rw [← Nat.pow_add]; congr 1; omega
  have key : m % 2 ^ (32 - r) * 2 ^ r + m / 2 ^ (32 - r) * 2 ^ 32 = m * 2 ^ r := by
    conv_rhs => rw [hsplit]
    rw [← h32]; ring
  rw [hidx, pow_add, pow_mul, pow_mul, pow_succ]
  calc _ = limbVal a.toList + (m % 2 ^ (32 - r) * 2 ^ r + m / 2 ^ (32 - r) * 2 ^ 32) *
        (2 ^ 32) ^ q := by ring
    _ = _ := by rw [key]; ring

/-- `addLimbs` with the split done in 64-bit machine words: the same result, ~13x faster in
compiled code (natural-number left shifts are not inlined by Lean's runtime). -/
@[inline] def addLimbs64 (a : Array Nat) (m : UInt64) (idx : Nat) : Array Nat :=
  let l := idx / 32
  let r := (idx % 32).toUInt64
  let lo := ((m % ((1 : UInt64) <<< (32 - r))) <<< r).toNat
  let hi := (m >>> (32 - r)).toNat
  (a.modify l (· + lo)).modify (l + 1) (· + hi)

theorem addLimbs64_eq (a : Array Nat) (m idx : Nat) (hm : m < 2 ^ 64) :
    addLimbs64 a m.toUInt64 idx = addLimbs a m idx := by
  have hr : idx % 32 < 32 := Nat.mod_lt _ (by norm_num)
  have hrn : ((idx % 32).toUInt64).toNat = idx % 32 := by
    simp [Nat.toUInt64_eq]; omega
  have hsub : (32 - (idx % 32).toUInt64).toNat = 32 - idx % 32 := by
    rw [UInt64.toNat_sub_of_le _ _ (by rw [UInt64.le_iff_toNat_le, hrn]; simp; omega), hrn]; rfl
  have hmn : m.toUInt64.toNat = m := by simp [Nat.toUInt64_eq]; omega
  have hpow : ((1 : UInt64) <<< (32 - (idx % 32).toUInt64)).toNat = 2 ^ (32 - idx % 32) := by
    rw [UInt64.toNat_shiftLeft, hsub, Nat.mod_eq_of_lt (by omega : 32 - idx % 32 < 64)]
    simp only [UInt64.toNat_one, Nat.one_shiftLeft]
    exact Nat.mod_eq_of_lt (Nat.pow_lt_pow_right (by norm_num) (by omega))
  have hlo : ((m.toUInt64 % ((1 : UInt64) <<< (32 - (idx % 32).toUInt64))) <<<
      (idx % 32).toUInt64).toNat = (m % (1 <<< (32 - idx % 32))) <<< (idx % 32) := by
    rw [UInt64.toNat_shiftLeft, UInt64.toNat_mod, hpow, hmn, hrn,
      Nat.mod_eq_of_lt (by omega : idx % 32 < 64), Nat.one_shiftLeft]
    apply Nat.mod_eq_of_lt
    rw [Nat.shiftLeft_eq]
    have h1 : m % 2 ^ (32 - idx % 32) < 2 ^ (32 - idx % 32) := Nat.mod_lt _ (by positivity)
    calc m % 2 ^ (32 - idx % 32) * 2 ^ (idx % 32) < 2 ^ (32 - idx % 32) * 2 ^ (idx % 32) :=
          Nat.mul_lt_mul_of_pos_right h1 (by positivity)
      _ = 2 ^ 32 := by rw [← Nat.pow_add]; congr 1; omega
      _ < 2 ^ 64 := by norm_num
  have hhi : (m.toUInt64 >>> (32 - (idx % 32).toUInt64)).toNat = m >>> (32 - idx % 32) := by
    rw [UInt64.toNat_shiftRight, hsub, hmn, Nat.mod_eq_of_lt (by omega : 32 - idx % 32 < 64)]
  simp only [addLimbs64, addLimbs, hlo, hhi]

theorem addLimbs64_size (a : Array Nat) (m : UInt64) (idx : Nat) :
    (addLimbs64 a m idx).size = a.size := by
  simp [addLimbs64]

/-- Zero limbs at the bottom only scale the value. -/
theorem limbVal_drop (l : List Nat) (k : Nat) (h : ∀ i, i < k → l.getD i 0 = 0) :
    limbVal l = 2 ^ (32 * k) * limbVal (l.drop k) := by
  induction k generalizing l with
  | zero => simp
  | succ k ih =>
    cases l with
    | nil => simp [limbVal]
    | cons x xs =>
      have hx : x = 0 := by simpa using h 0 (by omega)
      rw [limbVal, hx, ih xs (fun i hi => by simpa using h (i + 1) (by omega)), List.drop_succ_cons]
      ring

/-- The first limb index where either limb array is nonzero (or past the end). -/
def lowLimb (pos neg : Array Nat) : Nat → Nat → Nat
  | 0, i => i
  | fuel + 1, i => if pos[i]! = 0 ∧ neg[i]! = 0 then lowLimb pos neg fuel (i + 1) else i

theorem lowLimb_zero (pos neg : Array Nat) (fuel i : Nat) :
    ∀ j, i ≤ j → j < lowLimb pos neg fuel i → pos[j]! = 0 ∧ neg[j]! = 0 := by
  induction fuel generalizing i with
  | zero => intro j h1 h2; simp [lowLimb] at h2; omega
  | succ fuel ih =>
    intro j h1 h2
    simp only [lowLimb] at h2
    split at h2
    · rename_i hz
      rcases Nat.eq_or_lt_of_le h1 with rfl | hlt
      · exact hz
      · exact ih (i + 1) j (by omega) h2
    · omega

theorem getD_toList (a : Array Nat) (j : Nat) : a.toList.getD j 0 = a[j]! := by
  rw [List.getD_eq_getElem?_getD, Array.getElem?_toList]
  rcases Nat.lt_or_ge j a.size with h | h
  · simp [h]
  · simp [h]

end ExactRuntime
