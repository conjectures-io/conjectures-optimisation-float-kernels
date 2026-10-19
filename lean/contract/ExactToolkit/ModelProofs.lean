import ExactToolkit.TensorProofs

/-! Shape propagation through arbitrary model programs, not only the benchmark builders. -/

namespace ExactRuntime

def arrayShape (state : Array Word × Array Word) : Nat × Nat := (state.1.size, state.2.size)

/-- Shape semantics independently tracks the activation and saved residual lengths. -/
def Layer.shapeStep (layer : Layer) (shape : Nat × Nat) : Nat × Nat :=
  match layer with
  | .linear rows _ outputs _ _ _ => (rows * outputs, shape.2)
  | .gemm m _ n _ => (m * n, shape.2)
  | .conv s _ _ _ => (s.cout * (s.ho * s.wo), shape.2)
  | .depthwise s _ _ _ => (s.cin * (s.ho * s.wo), shape.2)
  | .bn _ c hw _ _ => (c * hw, shape.2)
  | .maxpool c h w k | .avgpool c h w k => (c * (h / k * (w / k)), shape.2)
  | .rmsnorm rows n _ _ | .layernorm rows n _ _ _ => (rows * n, shape.2)
  | .save => (shape.1, shape.1)
  | .addSaved _ => shape

theorem layer_shape (layer : Layer) (state : Array Word × Array Word) :
    arrayShape (layer.apply specKernels state) = layer.shapeStep (arrayShape state) := by
  cases layer <;>
    simp [arrayShape, Layer.apply, Layer.shapeStep, specKernels, specDense, specConv,
      specElem, specNorm, avgpoolSpec, avgpoolWith, maxpoolSpec, scaleShiftSpec, addReluSpec]

theorem layers_shape (layers : List Layer) (state : Array Word × Array Word) :
    arrayShape (layers.foldl (fun st l => l.apply specKernels st) state) =
      layers.foldl (fun shape l => l.shapeStep shape) (arrayShape state) := by
  induction layers generalizing state with
  | nil => rfl
  | cons l rest ih => simp only [List.foldl_cons, ih, layer_shape]

def modelOutputSize (layers : List Layer) (inputSize : Nat) : Nat :=
  (layers.foldl (fun shape l => l.shapeStep shape) (inputSize, 0)).1

/-- Exact output length for any model and correctly sized batch, including residual layers. -/
theorem runModel_shape (layers : List Layer) (samples size : Nat) (x : Array Word)
    (hlen : x.size = samples * size) :
    (runModel specKernels layers samples size x).size = samples * modelOutputSize layers size := by
  have sample_size (i : Nat) (hi : i < samples) :
      (x.extract (i * size) (i * size + size)).size = size := by
    have hu : i * size + size ≤ x.size := by
      rw [hlen]
      have h := Nat.mul_le_mul_right size (Nat.succ_le_of_lt hi)
      nlinarith
    simp [Array.size_extract, Nat.min_eq_left hu, Nat.min_eq_left (show i * size ≤ x.size by omega)]
  have single_size (i : Nat) (hi : i < samples) :
      (layers.foldl (fun st l => l.apply specKernels st)
        (x.extract (i * size) (i * size + size), #[])).1.size = modelOutputSize layers size := by
    have hs := congrArg Prod.fst (layers_shape layers
      (x.extract (i * size) (i * size + size), #[]))
    simpa [arrayShape, sample_size i hi, modelOutputSize] using hs
  have loop : ∀ (indices : List Nat), (∀ i ∈ indices, i < samples) → ∀ (out : Array Word),
      (indices.foldl (fun acc i => acc ++ (layers.foldl (fun st l => l.apply specKernels st)
        (x.extract (i * size) (i * size + size), #[])).1) out).size =
          out.size + indices.length * modelOutputSize layers size := by
    intro indices
    induction indices with
    | nil => intro _ out; simp
    | cons i rest ih =>
      intro hidx out
      simp only [List.foldl_cons]
      rw [ih (by intro j hj; exact hidx j (by simp [hj]))]
      rw [Array.size_append, single_size i (hidx i (by simp))]
      simp only [List.length_cons]
      ring
  unfold runModel
  simpa using loop (List.range samples) (by intro i hi; simpa using hi) #[]

/-- Every universally proved implementation inherits the same shape theorem. -/
theorem accepted_model_shape (K : Kernels) (h : Obligation K) (layers : List Layer)
    (samples size : Nat) (x : Array Word) (hlen : x.size = samples * size) :
    (runModel K layers samples size x).size = samples * modelOutputSize layers size := by
  rw [h]
  exact runModel_shape layers samples size x hlen

/-- Static shape conditions, separate from numerical finiteness and epsilon conditions. -/
def Layer.acceptsShape (layer : Layer) (shape : Nat × Nat) : Prop :=
  match layer with
  | .linear r i o _ w b =>
      r > 0 ∧ i > 0 ∧ o > 0 ∧ shape.1 = r * i ∧ w.size = o * i ∧ b.size = o
  | .gemm m k n a => m > 0 ∧ k > 0 ∧ n > 0 ∧ a.size = m * k ∧ shape.1 = k * n
  | .conv s _ w b => s.Valid ∧ shape.1 = s.cin * s.h * s.w ∧
      w.size = s.cout * s.cin * s.kh * s.kw ∧ b.size = s.cout
  | .depthwise s _ w b => s.Valid ∧ shape.1 = s.cin * s.h * s.w ∧
      w.size = s.cin * s.kh * s.kw ∧ b.size = s.cin
  | .bn _ c hw s t => c > 0 ∧ hw > 0 ∧ shape.1 = c * hw ∧ s.size = c ∧ t.size = c
  | .maxpool c h w k | .avgpool c h w k =>
      c > 0 ∧ h > 0 ∧ w > 0 ∧ k > 0 ∧ k ≤ h ∧ k ≤ w ∧ shape.1 = c * h * w
  | .rmsnorm r n _ g => r > 0 ∧ n > 0 ∧ shape.1 = r * n ∧ g.size = n
  | .layernorm r n _ g b => r > 0 ∧ n > 0 ∧ shape.1 = r * n ∧ g.size = n ∧ b.size = n
  | .save => True
  | .addSaved _ => shape.1 = shape.2 ∧ shape.1 > 0

/-- Shape constraints are decidable without running any kernels. -/
instance (layer : Layer) (shape : Nat × Nat) : Decidable (layer.acceptsShape shape) := by
  cases layer <;> unfold Layer.acceptsShape <;> infer_instance

/-- A model is well-shaped if every layer accepts its predecessor's symbolic output shape. -/
def ModelWellShaped : List Layer → Nat × Nat → Prop
  | [], _ => True
  | l :: rest, shape => l.acceptsShape shape ∧ ModelWellShaped rest (l.shapeStep shape)

/-- The property being established about actual execution, rather than symbolic sizes. -/
def ExecutionWellShaped (K : Kernels) : List Layer → Array Word × Array Word → Prop
  | [], _ => True
  | l :: rest, state => l.acceptsShape (arrayShape state) ∧
      ExecutionWellShaped K rest (l.apply K state)

/-- Static shape validation suffices for every actual layer input, independently of values. -/
theorem model_preserves_valid_shapes (layers : List Layer) (state : Array Word × Array Word)
    (h : ModelWellShaped layers (arrayShape state)) : ExecutionWellShaped specKernels layers state := by
  induction layers generalizing state with
  | nil => trivial
  | cons l rest ih =>
    refine ⟨h.1, ih _ ?_⟩
    rw [layer_shape]
    exact h.2

theorem accepted_preserves_valid_shapes (K : Kernels) (hk : Obligation K) (layers : List Layer)
    (state : Array Word × Array Word) (h : ModelWellShaped layers (arrayShape state)) :
    ExecutionWellShaped K layers state := by
  rw [hk]
  exact model_preserves_valid_shapes layers state h

end ExactRuntime
