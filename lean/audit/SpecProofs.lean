import ExactToolkit.ModelProofs

/-! Additional operator-side mathematical facts, checked separately from miner proofs. -/

namespace ExactRuntime.Audit

/-- Variance is nonnegative for every rational center, before any rounding. -/
theorem rowVariance_nonneg (n : Nat) (x : Array UInt32) (row : Nat) (center : ℚ) :
    0 ≤ rowVariance n x row center := by
  unfold rowVariance
  apply div_nonneg _ (by positivity)
  have loop : ∀ (indices : List Nat) (total : ℚ), 0 ≤ total →
      0 ≤ indices.foldl (fun acc j => acc + (rationalWord x[row * n + j]! - center) ^ 2) total := by
    intro indices
    induction indices with
    | nil => intro total ht; exact ht
    | cons j rest ih =>
      intro total ht
      apply ih
      exact add_nonneg ht (sq_nonneg _)
  exact loop _ 0 le_rfl

/-- The public model interface inherits equality from the miner's universal obligation. -/
theorem checked_model_eq (K : Kernels) (h : Obligation K) (layers : List Layer)
    (samples size : Nat) (x : Array UInt32) :
    runModelChecked K layers samples size x = runModelChecked specKernels layers samples size x := by
  rw [h]

end ExactRuntime.Audit

#print axioms ExactRuntime.sqrtLower_bounds
#print axioms ExactRuntime.guarded_eq
#print axioms ExactRuntime.checked_layer_eq
#print axioms ExactRuntime.Audit.rowVariance_nonneg
#print axioms ExactRuntime.Audit.checked_model_eq
#print axioms ExactRuntime.sqrtAccuracy_correct
#print axioms ExactRuntime.sqrtRational_eq_math
#print axioms ExactRuntime.TensorMath.linear_entry
#print axioms ExactRuntime.TensorMath.gemm_entry
#print axioms ExactRuntime.TensorMath.window_coordinates
#print axioms ExactRuntime.TensorMath.conv2d_entry
#print axioms ExactRuntime.TensorMath.depthwise_entry
#print axioms ExactRuntime.TensorMath.avgpool_entry
#print axioms ExactRuntime.TensorMath.maxpool_entry
#print axioms ExactRuntime.TensorMath.addRelu_entry
#print axioms ExactRuntime.TensorMath.scaleShift_entry
#print axioms ExactRuntime.TensorMath.rmsnorm_entry
#print axioms ExactRuntime.TensorMath.layernorm_entry
#print axioms ExactRuntime.TensorMath.rowMean_math
#print axioms ExactRuntime.TensorMath.variance_second_moment
#print axioms ExactRuntime.TensorMath.dsum_math
#print axioms ExactRuntime.TensorMath.dotSpec_nonzero_math
#print axioms ExactRuntime.TensorMath.dotSpec_zero_math
#print axioms ExactRuntime.TensorMath.linear_total
#print axioms ExactRuntime.TensorMath.linear_nonzero_math
#print axioms ExactRuntime.TensorMath.conv_total
#print axioms ExactRuntime.TensorMath.depthwise_total
#print axioms ExactRuntime.TensorMath.pool_total
#print axioms ExactRuntime.TensorMath.sum_total
#print axioms ExactRuntime.TensorMath.relu_finite_math
#print axioms ExactRuntime.TensorMath.maxpool_math
#print axioms ExactRuntime.accepted_model_shape
#print axioms ExactRuntime.accepted_preserves_valid_shapes
