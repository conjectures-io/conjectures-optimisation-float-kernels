import ContractExact

/-! Checked public runtime boundary. The miner obligation remains universal equality.
All dimension arithmetic is over unbounded naturals; deployment resource limits are separate. -/

namespace ExactRuntime

def LinearValid (rows inner outputs : Nat) (x w b : Array Word) : Prop :=
  rows > 0 ∧ inner > 0 ∧ outputs > 0 ∧ x.size = rows * inner ∧
    w.size = outputs * inner ∧ b.size = outputs

def GemmValid (batch m k n : Nat) (a b : Array Word) : Prop :=
  batch > 0 ∧ m > 0 ∧ k > 0 ∧ n > 0 ∧
    a.size = batch * m * k ∧ b.size = batch * k * n

/-- Positive sizes, strides, dilations and groups; groups divide both channel counts. -/
def ConvShape.Geometry (s : ConvShape) : Prop :=
  s.cin > 0 ∧ s.cout > 0 ∧ s.h > 0 ∧ s.w > 0 ∧ s.kh > 0 ∧ s.kw > 0 ∧
    s.strideH > 0 ∧ s.strideW > 0 ∧ s.dilH > 0 ∧ s.dilW > 0 ∧ s.groups > 0 ∧
    s.cin % s.groups = 0 ∧ s.cout % s.groups = 0

/-- A convolution whose dilated filter fits the padded input. -/
def ConvShape.Valid (s : ConvShape) : Prop :=
  s.Geometry ∧ s.ekh ≤ s.h + s.padT + s.padB ∧ s.ekw ≤ s.w + s.padL + s.padR

/-- Weights are `cout × (cin / groups) × kh × kw`; depthwise uses `s.depthwise`'s geometry. -/
def ConvValid (s : ConvShape) (depthwise : Bool) (x w b : Array Word) : Prop :=
  let s := if depthwise then s.depthwise else s
  s.Valid ∧ x.size = s.cin * s.h * s.w ∧ w.size = s.cout * s.taps ∧ b.size = s.cout

/-- A transposed convolution with at least one output row and column, and output padding below
the stride or the dilation (PyTorch's rule; ONNX Runtime also requires it below the stride, so
every model it runs is valid here). -/
def ConvTShape.Valid (s : ConvTShape) : Prop :=
  s.Geometry ∧
    s.padT + s.padB < s.strideH * (s.h - 1) + s.outPadH + s.ekh ∧
    s.padL + s.padR < s.strideW * (s.w - 1) + s.outPadW + s.ekw ∧
    (s.outPadH < s.strideH ∨ s.outPadH < s.dilH) ∧ (s.outPadW < s.strideW ∨ s.outPadW < s.dilW)

/-- Weights are `cin × (cout / groups) × kh × kw`. -/
def ConvTValid (s : ConvTShape) (x w b : Array Word) : Prop :=
  s.Valid ∧ x.size = s.cin * s.h * s.w ∧ w.size = s.cin * s.opg * s.kh * s.kw ∧
    b.size = s.cout

/-- Positive sizes and strides, a window that fits the padded input, and padding smaller than
the window, so that every window holds at least one image cell. -/
def PoolShape.Valid (s : PoolShape) : Prop :=
  s.c > 0 ∧ s.h > 0 ∧ s.w > 0 ∧ s.kh > 0 ∧ s.kw > 0 ∧ s.strideH > 0 ∧ s.strideW > 0 ∧
    s.kh ≤ s.h + s.padT + s.padB ∧ s.kw ≤ s.w + s.padL + s.padR ∧
    s.padT < s.kh ∧ s.padB < s.kh ∧ s.padL < s.kw ∧ s.padR < s.kw

def PoolValid (s : PoolShape) (x : Array Word) : Prop := s.Valid ∧ x.size = s.c * s.h * s.w

instance (r i o : Nat) (x w b : Array Word) : Decidable (LinearValid r i o x w b) :=
  inferInstanceAs (Decidable (_ ∧ _ ∧ _ ∧ _ ∧ _ ∧ _))
instance (batch m k n : Nat) (a b : Array Word) : Decidable (GemmValid batch m k n a b) :=
  inferInstanceAs (Decidable (_ ∧ _ ∧ _ ∧ _ ∧ _ ∧ _))
instance (s : ConvShape) : Decidable s.Geometry := by unfold ConvShape.Geometry; infer_instance
instance (s : ConvShape) : Decidable s.Valid := by unfold ConvShape.Valid; infer_instance
instance (s : ConvShape) (dw : Bool) (x w b : Array Word) : Decidable (ConvValid s dw x w b) := by
  unfold ConvValid; infer_instance
instance (s : ConvTShape) : Decidable s.Valid := by unfold ConvTShape.Valid; infer_instance
instance (s : ConvTShape) (x w b : Array Word) : Decidable (ConvTValid s x w b) := by
  unfold ConvTValid; infer_instance
instance (s : PoolShape) : Decidable s.Valid := by unfold PoolShape.Valid; infer_instance
instance (s : PoolShape) (x : Array Word) : Decidable (PoolValid s x) := by
  unfold PoolValid; infer_instance

/-- A fast path can assume validity while the exposed function remains universally equal. -/
def guarded {α β : Type} (valid : α → Prop) [DecidablePred valid]
    (fast spec : α → β) (x : α) : β := if valid x then fast x else spec x

theorem guarded_eq {α β : Type} (valid : α → Prop) [DecidablePred valid]
    (fast spec : α → β) (h : ∀ x, valid x → fast x = spec x) :
    guarded valid fast spec = spec := by
  funext x
  by_cases hx : valid x
  · simp [guarded, hx, h x hx]
  · simp [guarded, hx]

def Layer.valid (l : Layer) (state : Array Word × Array Word) : Bool :=
  let (x, saved) := state
  match l with
    | .linear r i o _ w b => decide (LinearValid r i o x w b)
    | .gemm m k n a => decide (GemmValid 1 m k n a x)
    | .conv s _ w b => decide (ConvValid s false x w b)
    | .depthwise s _ w b => decide (ConvValid s true x w b)
    | .convTranspose s _ w b => decide (ConvTValid s x w b)
    | .bn _ c hw s t => c > 0 && hw > 0 && x.size == c * hw && s.size == c && t.size == c
    | .maxpool s | .avgpool s => decide (PoolValid s x)
    | .rmsnorm r n eps g => stableNormDomain r n eps x g
    | .layernorm r n eps g b => stableNormDomain r n eps x g && b.size == n && b.all finiteWord
    | .save => true
    | .addSaved _ => x.size == saved.size && !x.isEmpty

/-- Validate before calling any miner code; the rejected branch does not evaluate it. -/
def Layer.checked (K : Kernels) (l : Layer) (state : Array Word × Array Word) :
    Except String (Array Word × Array Word) :=
  if l.valid state then .ok (l.apply K state)
  else .error "invalid layer shape, array length, or normalization domain"

def runModelChecked (K : Kernels) (layers : List Layer) (samples size : Nat) (x : Array Word) :
    Except String (Array Word) := do
  if samples == 0 || size == 0 || x.size != samples * size then
    throw "invalid model input shape"
  let mut output := #[]
  for i in [0:samples] do
    let mut state := (x.extract (i * size) (i * size + size), #[])
    for l in layers do state ← l.checked K state
    output := output ++ state.1
  return output

/-- Public checks do not change successful single-layer computations. -/
theorem checked_layer_eq (K : Kernels) (l : Layer) (st out : Array Word × Array Word)
    (h : l.checked K st = .ok out) : out = l.apply K st := by
  unfold Layer.checked at h
  split at h <;> simp_all

@[simp] theorem linearSpec_size (r i o : Nat) (relu : Bool) (x w b : Array Word) :
    (linearSpec r i o relu x w b).size = r * o := by simp [linearSpec, linearWith]

@[simp] theorem gemmSpec_size (batch m k n : Nat) (a b : Array Word) :
    (gemmSpec batch m k n a b).size = batch * (m * n) := by simp [gemmSpec, gemmWith]

@[simp] theorem conv2dSpec_size (s : ConvShape) (relu : Bool) (x w b : Array Word) :
    (conv2dSpec s relu x w b).size = s.cout * (s.ho * s.wo) := by simp [conv2dSpec, conv2dWith]

@[simp] theorem depthwiseSpec_size (s : ConvShape) (relu : Bool) (x w b : Array Word) :
    (depthwiseSpec s relu x w b).size = s.cin * (s.ho * s.wo) := by
  simp [depthwiseSpec, depthwiseWith, conv2dWith, ConvShape.depthwise, ConvShape.ho,
    ConvShape.wo, ConvShape.ekh, ConvShape.ekw]

@[simp] theorem convTransposeSpec_size (s : ConvTShape) (relu : Bool) (x w b : Array Word) :
    (convTransposeSpec s relu x w b).size = s.cout * (s.ho * s.wo) := by
  simp [convTransposeSpec, convTransposeWith]

@[simp] theorem avgpoolSpec_size (s : PoolShape) (x : Array Word) :
    (avgpoolSpec s x).size = s.c * (s.ho * s.wo) := by simp [avgpoolSpec, avgpoolWith]

@[simp] theorem maxpoolSpec_size (s : PoolShape) (x : Array Word) :
    (maxpoolSpec s x).size = s.c * (s.ho * s.wo) := by simp [maxpoolSpec]

@[simp] theorem rmsnormSpec_size (r n : Nat) (eps : Word) (x g : Array Word) :
    (rmsnormSpec r n eps x g).size = r * n := by
  unfold rmsnormSpec rmsnormWith
  split <;> simp [rmsnormStable, rmsnormLegacyWith]

@[simp] theorem layernormSpec_size (r n : Nat) (eps : Word) (x g b : Array Word) :
    (layernormSpec r n eps x g b).size = r * n := by
  unfold layernormSpec layernormWith
  split <;> simp [layernormStable, layernormLegacyWith]

theorem rmsnorm_stable_on_domain (r n : Nat) (eps : Word) (x g : Array Word)
    (h : stableNormDomain r n eps x g = true) :
    rmsnormSpec r n eps x g = rmsnormStable r n eps x g := by
  simp [rmsnormSpec, rmsnormWith, h]

theorem layernorm_stable_on_domain (r n : Nat) (eps : Word) (x g b : Array Word)
    (h : stableNormDomain r n eps x g = true) (hb : b.size = n) (hf : b.all finiteWord = true) :
    layernormSpec r n eps x g b = layernormStable r n eps x g b := by
  have hg : (stableNormDomain r n eps x g && b.size == n && b.all finiteWord) = true :=
    Bool.and_eq_true_iff.mpr ⟨Bool.and_eq_true_iff.mpr ⟨h, by simpa using hb⟩, hf⟩
  simp only [layernormSpec, layernormWith, hg, ite_true]

end ExactRuntime
