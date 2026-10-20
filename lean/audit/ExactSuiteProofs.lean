import ExactSuite
import ExactToolkit.ModelProofs

/-! Operator-side proofs of the benchmark builders, independent of sampled execution. -/
namespace ExactSuite
open ExactRuntime

theorem lcg_size (n : Nat) (seed : UInt32) : (lcg n seed).size = n := by
  let step := fun (st : Array UInt32 × UInt32) (_ : Nat) =>
    (st.1.push (st.2 * 1664525 + 1013904223), st.2 * 1664525 + 1013904223)
  have loop (indices : List Nat) (st : Array UInt32 × UInt32) :
      (indices.foldl step st).1.size = st.1.size + indices.length := by
    induction indices generalizing st with
    | nil => simp
    | cons i rest ih => simp [List.foldl_cons, ih, step]; omega
  unfold lcg
  simp only [Std.Legacy.Range.forIn_eq_forIn_range', List.forIn_pure_yield_eq_foldl]
  change ((List.range' 0 [:n].size).foldl step (Array.mkEmpty n, seed)).1.size = n
  rw [loop]
  simp [Std.Legacy.Range.size]

@[simp] theorem gen_size (kind : String) (n : Nat) (seed : UInt32) :
    (gen kind n seed).size = n := by simp [gen, lcg_size]

@[simp] theorem scales_size (n : Nat) (seed : UInt32) : (scales n seed).size = n := by
  simp [scales]

@[simp] theorem wts_size (kind : String) (seed : UInt32) (k fanIn n : Nat) :
    (wts kind seed k fanIn n).size = n := by simp [wts]

@[simp] theorem biases_size (seed : UInt32) (k n : Nat) : (biases seed k n).size = n := by
  simp [biases]

private def mlpChain (kind : String) (seed : UInt32) (rows total offset : Nat)
    (widths : List Nat) : List Layer :=
  (widths.zip widths.tail).zipIdx offset |>.map fun ((i, o), k) =>
    .linear rows i o (k + 1 < total)
      (wts kind seed (2 * k) i (i * o)) (biases seed (2 * k + 1) o)

private theorem mlpChain_shape (kind : String) (seed : UInt32) (rows total : Nat)
    (hr : 0 < rows) (tail : List Nat) (first offset saved : Nat)
    (hp : ∀ w ∈ first :: tail, 0 < w) :
    ModelWellShaped (mlpChain kind seed rows total offset (first :: tail)) (rows * first, saved) ∧
    (mlpChain kind seed rows total offset (first :: tail)).foldl
      (fun shape l => l.shapeStep shape) (rows * first, saved) =
      (rows * tail.foldl (fun _ w => w) first, saved) := by
  induction tail generalizing first offset saved with
  | nil => simp [mlpChain, ModelWellShaped]
  | cons next rest ih =>
    have hf : 0 < first := hp first (by simp)
    have hn : 0 < next := hp next (by simp)
    have ht : ∀ w ∈ next :: rest, 0 < w := by
      intro w hw
      exact hp w (by simp only [List.mem_cons] at hw ⊢; tauto)
    obtain ⟨hv, ho⟩ := ih next (offset + 1) saved ht
    have head : mlpChain kind seed rows total offset (first :: next :: rest) =
        .linear rows first next (offset + 1 < total)
          (wts kind seed (2 * offset) first (first * next))
          (biases seed (2 * offset + 1) next) ::
          mlpChain kind seed rows total (offset + 1) (next :: rest) := by
      simp [mlpChain, List.zipIdx_cons]
    rw [head]
    constructor
    · exact ⟨by simp [Layer.acceptsShape, hr, hf, hn, Nat.mul_comm], hv⟩
    · simpa only [List.foldl_cons, Layer.shapeStep] using ho

/-- Any nonempty list of positive widths yields a compatible MLP, not only the scored depth. -/
theorem mlp_wellShaped (kind : String) (seed : UInt32) (rows first saved : Nat)
    (tail : List Nat) (hr : 0 < rows) (hp : ∀ w ∈ first :: tail, 0 < w) :
    ModelWellShaped (mlpLayers kind seed rows (first :: tail)) (rows * first, saved) := by
  exact (mlpChain_shape kind seed rows ((first :: tail).zip tail).length hr tail first 0 saved hp).1

theorem mlp_outputShape (kind : String) (seed : UInt32) (rows first saved : Nat)
    (tail : List Nat) (hr : 0 < rows) (hp : ∀ w ∈ first :: tail, 0 < w) :
    (mlpLayers kind seed rows (first :: tail)).foldl (fun shape l => l.shapeStep shape)
      (rows * first, saved) = (rows * tail.foldl (fun _ w => w) first, saved) := by
  exact (mlpChain_shape kind seed rows ((first :: tail).zip tail).length hr tail first 0 saved hp).2

theorem nmlp_wellShaped (kind : String) (seed : UInt32) (rows i h h2 out saved : Nat)
    (hr : 0 < rows) (hi : 0 < i) (hh : 0 < h) (hh2 : 0 < h2) (ho : 0 < out) :
    ModelWellShaped (nmlpLayers kind seed rows i h h2 out) (rows * i, saved) := by
  simp [nmlpLayers, ModelWellShaped, Layer.acceptsShape, Layer.shapeStep, hr, hi, hh, hh2, ho]

theorem nmlp_outputShape (kind : String) (seed : UInt32) (rows i h h2 out saved : Nat) :
    (nmlpLayers kind seed rows i h h2 out).foldl (fun shape l => l.shapeStep shape)
      (rows * i, saved) = (rows * out, saved) := by
  simp [nmlpLayers, Layer.shapeStep]

theorem mixer_wellShaped (kind : String) (seed : UInt32) (T C classes saved : Nat)
    (ht : 0 < T) (hc : 0 < C) (hcl : 0 < classes) :
    ModelWellShaped (mixerLayers kind seed T C classes) (T * C, saved) := by
  simp [mixerLayers, ModelWellShaped, Layer.acceptsShape, Layer.shapeStep, ht, hc, hcl,
    Nat.mul_comm, Nat.mul_left_comm]

theorem mixer_outputShape (kind : String) (seed : UInt32) (T C classes saved : Nat) :
    (mixerLayers kind seed T C classes).foldl (fun shape l => l.shapeStep shape)
      (T * C, saved) = (classes, T * C) := by
  simp [mixerLayers, Layer.shapeStep]

@[simp] theorem conv3_ho (ci n co : Nat) (hn : 0 < n) :
    (ConvShape.mk ci n n co 3 3 1 1).ho = n := by
  simp only [ConvShape.ho, Nat.mul_one, Nat.div_one]
  omega

@[simp] theorem conv3_wo (ci n co : Nat) (hn : 0 < n) :
    (ConvShape.mk ci n n co 3 3 1 1).wo = n := by
  simp only [ConvShape.wo, Nat.mul_one, Nat.div_one]
  omega

@[simp] theorem conv1_ho (ci n co : Nat) (hn : 0 < n) :
    (ConvShape.mk ci n n co 1 1 1 0).ho = n := by
  simp only [ConvShape.ho, Nat.mul_zero, Nat.add_zero, Nat.div_one]
  omega

@[simp] theorem conv1_wo (ci n co : Nat) (hn : 0 < n) :
    (ConvShape.mk ci n n co 1 1 1 0).wo = n := by
  simp only [ConvShape.wo, Nat.mul_zero, Nat.add_zero, Nat.div_one]
  omega

theorem cnn_wellShaped (kind : String) (seed : UInt32) (ci n c1 c2 classes saved : Nat)
    (hi : 0 < ci) (hn : 2 ≤ n) (h1 : 0 < c1) (h2 : 0 < c2) (hc : 0 < classes) :
    ModelWellShaped (cnnLayers kind seed ci n c1 c2 classes) (ci * n * n, saved) := by
  have hn0 : 0 < n := by omega
  have hn20 : 0 < n / 2 := by omega
  have hf : 3 ≤ n + 2 := by omega
  have hf2 : 3 ≤ n / 2 + 2 := by omega
  simp [cnnLayers, ModelWellShaped, Layer.acceptsShape, Layer.shapeStep, ConvShape.Valid,
    hi, h1, h2, hc, hn, hn0, hn20, hf, hf2, Nat.mul_assoc]

theorem cnn_outputShape (kind : String) (seed : UInt32) (ci n c1 c2 classes saved : Nat)
    (hn : 2 ≤ n) :
    (cnnLayers kind seed ci n c1 c2 classes).foldl (fun shape l => l.shapeStep shape)
      (ci * n * n, saved) = (classes, c1 * (n * n)) := by
  have hn0 : 0 < n := by omega
  simp [cnnLayers, Layer.shapeStep, hn0]

theorem mnet_wellShaped (kind : String) (seed : UInt32) (ci n c1 c2 classes saved : Nat)
    (hi : 0 < ci) (hn : 2 ≤ n) (h1 : 0 < c1) (h2 : 0 < c2) (hc : 0 < classes) :
    ModelWellShaped (mnetLayers kind seed ci n c1 c2 classes) (ci * n * n, saved) := by
  have hn0 : 0 < n := by omega
  have hn20 : 0 < n / 2 := by omega
  have hf : 3 ≤ n + 2 := by omega
  have hf2 : 3 ≤ n / 2 + 2 := by omega
  simp [mnetLayers, ModelWellShaped, Layer.acceptsShape, Layer.shapeStep, ConvShape.Valid,
    hi, h1, h2, hc, hn, hn0, hn20, hf, hf2, Nat.mul_assoc]
  exact ⟨hn0, hn20⟩

theorem mnet_outputShape (kind : String) (seed : UInt32) (ci n c1 c2 classes saved : Nat) :
    (mnetLayers kind seed ci n c1 c2 classes).foldl (fun shape l => l.shapeStep shape)
      (ci * n * n, saved) = (classes, saved) := by
  simp [mnetLayers, Layer.shapeStep]

/-- Shape conditions and classifier output count selected by a seeded cell. -/
def CellModelValid (c : Cell) (seed : UInt32) : Prop :=
  let (layers, samples, size) := cellModel c seed
  0 < samples ∧ 0 < size ∧ ModelWellShaped layers (size, 0) ∧
    samples * modelOutputSize layers size = c.batch * 10

theorem cellModel_valid (c : Cell) (seed : UInt32) (hb : 0 < c.batch) :
    CellModelValid c seed := by
  let r := lcg 8 (seed ^^^ 0x5bd1e995)
  let pick := fun i lo span : Nat => lo + r[i]!.toNat % span
  let side := if r[5]! % 2 == 0 then 12 else 16
  have hp (i lo span : Nat) (hl : 0 < lo) : 0 < pick i lo span := by
    dsimp [pick]
    omega
  have hs : 2 ≤ side := by unfold side; split <;> decide
  have hs0 : 0 < side := by omega
  unfold CellModelValid cellModel
  dsimp only
  split
  · change 0 < 1 ∧ 0 < c.batch * pick 0 256 529 ∧
      ModelWellShaped (mlpLayers c.kind seed c.batch
        [pick 0 256 529, pick 1 64 193, pick 2 32 97, 10]) (c.batch * pick 0 256 529, 0) ∧
      1 * modelOutputSize (mlpLayers c.kind seed c.batch
        [pick 0 256 529, pick 1 64 193, pick 2 32 97, 10]) (c.batch * pick 0 256 529) = c.batch * 10
    refine ⟨by decide, Nat.mul_pos hb (hp _ _ _ (by decide)), ?_, ?_⟩
    · apply mlp_wellShaped _ _ _ _ _ _ hb
      simp only [List.mem_cons, List.not_mem_nil, or_false]
      intro w hw
      rcases hw with h | h | h | h <;> subst w
      · exact hp _ _ _ (by decide)
      · exact hp _ _ _ (by decide)
      · exact hp _ _ _ (by decide)
      · decide
    · simp [modelOutputSize, mlpLayers, Layer.shapeStep]
  · change 0 < c.batch ∧ 0 < 3 * side * side ∧
      ModelWellShaped (cnnLayers c.kind seed 3 side (pick 3 4 5) (pick 4 8 9) 10)
        (3 * side * side, 0) ∧
      c.batch * modelOutputSize (cnnLayers c.kind seed 3 side (pick 3 4 5) (pick 4 8 9) 10)
        (3 * side * side) = c.batch * 10
    refine ⟨hb, by positivity, cnn_wellShaped _ _ _ _ _ _ _ _ (by decide) hs
      (hp _ _ _ (by decide)) (hp _ _ _ (by decide)) (by decide), ?_⟩
    simp [modelOutputSize, cnn_outputShape _ _ _ _ _ _ _ _ hs]
  · change 0 < c.batch ∧ 0 < 3 * side * side ∧
      ModelWellShaped (mnetLayers c.kind seed 3 side (pick 3 4 5) (pick 4 8 9) 10)
        (3 * side * side, 0) ∧
      c.batch * modelOutputSize (mnetLayers c.kind seed 3 side (pick 3 4 5) (pick 4 8 9) 10)
        (3 * side * side) = c.batch * 10
    refine ⟨hb, by positivity, mnet_wellShaped _ _ _ _ _ _ _ _ (by decide) hs
      (hp _ _ _ (by decide)) (hp _ _ _ (by decide)) (by decide), ?_⟩
    simp [modelOutputSize, mnet_outputShape]
  · change 0 < 1 ∧ 0 < c.batch * pick 0 128 385 ∧
      ModelWellShaped (nmlpLayers c.kind seed c.batch (pick 0 128 385)
        (pick 1 64 193) (pick 2 32 97) 10) (c.batch * pick 0 128 385, 0) ∧
      1 * modelOutputSize (nmlpLayers c.kind seed c.batch (pick 0 128 385)
        (pick 1 64 193) (pick 2 32 97) 10) (c.batch * pick 0 128 385) = c.batch * 10
    refine ⟨by decide, Nat.mul_pos hb (hp _ _ _ (by decide)),
      nmlp_wellShaped _ _ _ _ _ _ _ _ hb (hp _ _ _ (by decide))
        (hp _ _ _ (by decide)) (hp _ _ _ (by decide)) (by decide), ?_⟩
    simp [modelOutputSize, nmlp_outputShape]
  · change 0 < c.batch ∧ 0 < pick 6 8 9 * pick 7 16 33 ∧
      ModelWellShaped (mixerLayers c.kind seed (pick 6 8 9) (pick 7 16 33) 10)
        (pick 6 8 9 * pick 7 16 33, 0) ∧
      c.batch * modelOutputSize (mixerLayers c.kind seed (pick 6 8 9) (pick 7 16 33) 10)
        (pick 6 8 9 * pick 7 16 33) = c.batch * 10
    refine ⟨hb, Nat.mul_pos (hp _ _ _ (by decide)) (hp _ _ _ (by decide)),
      mixer_wellShaped _ _ _ _ _ _ (hp _ _ _ (by decide)) (hp _ _ _ (by decide)) (by decide), ?_⟩
    simp [modelOutputSize, mixer_outputShape]

/-- The actual suite contains only positive batch sizes. No seed enumeration is needed. -/
theorem cells_batch_pos : ∀ c ∈ cells, 0 < c.batch := by simp [cells]

/-- A generated cell has valid batching, exact input length, compatible layers and ten logits
per batch item. Numerical finiteness and input sensitivity are separate properties. -/
def PreparedCell.ShapeValid (p : PreparedCell) (batch : Nat) : Prop :=
  0 < p.samples ∧ 0 < p.size ∧ p.input.size = p.samples * p.size ∧
    ModelWellShaped p.layers (p.size, 0) ∧
    p.samples * modelOutputSize p.layers p.size = batch * 10

theorem prepareCell_valid (c : Cell) (seed : UInt32) (hb : 0 < c.batch) :
    (prepareCell c seed).ShapeValid c.batch := by
  have h := cellModel_valid c seed hb
  unfold CellModelValid at h
  unfold PreparedCell.ShapeValid prepareCell
  rcases hm : cellModel c seed with ⟨layers, samples, size⟩
  simp only [hm] at h
  exact ⟨h.1, h.2.1, gen_size _ _ _, h.2.2.1, h.2.2.2⟩

/-- Every scored cell is well-shaped for every UInt32 seed, rather than a sampled seed set. -/
theorem all_cells_valid (c : Cell) (hc : c ∈ cells) (seed : UInt32) :
    (prepareCell c seed).ShapeValid c.batch :=
  prepareCell_valid c seed (cells_batch_pos c hc)

/-- Any accepted kernel set produces exactly ten logits per batch item on each generated cell. -/
theorem accepted_cell_output_size (K : Kernels) (hk : Obligation K)
    (c : Cell) (hc : c ∈ cells) (seed : UInt32) :
    let p := prepareCell c seed
    (runModel K p.layers p.samples p.size p.input).size = c.batch * 10 := by
  have hv := all_cells_valid c hc seed
  exact (accepted_model_shape K hk _ _ _ _ hv.2.2.1).trans hv.2.2.2.2

/-- Every sample slice has its advertised length, without overflow-prone machine arithmetic. -/
theorem prepared_sample_size (p : PreparedCell) (hlen : p.input.size = p.samples * p.size)
    (sample : Nat) (hs : sample < p.samples) :
    (p.input.extract (sample * p.size) ((sample + 1) * p.size)).size = p.size := by
  have he : (sample + 1) * p.size ≤ p.input.size := by
    rw [hlen]
    exact Nat.mul_le_mul_right p.size (Nat.succ_le_of_lt hs)
  have he' : sample * p.size + p.size ≤ p.input.size := by simpa [Nat.add_mul] using he
  simp [Array.size_extract, Nat.add_mul, Nat.min_eq_left he']

/-- Each actual generated sample supplies compatible shapes at every layer, including residuals,
for every accepted implementation. This transfers the static builder proof to execution. -/
theorem accepted_cell_sample_shapes (K : Kernels) (hk : Obligation K)
    (c : Cell) (hc : c ∈ cells) (seed : UInt32) (sample : Nat)
    (hs : sample < (prepareCell c seed).samples) :
    let p := prepareCell c seed
    ExecutionWellShaped K p.layers
      (p.input.extract (sample * p.size) ((sample + 1) * p.size), #[]) := by
  have hv := all_cells_valid c hc seed
  apply accepted_preserves_valid_shapes K hk
  simpa only [arrayShape, prepared_sample_size _ hv.2.2.1 sample hs, Array.size_empty]
    using hv.2.2.2.1

end ExactSuite
