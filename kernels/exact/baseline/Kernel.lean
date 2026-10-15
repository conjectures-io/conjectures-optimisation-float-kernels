import ContractExact

/-!
# Exact runtime, baseline

FloatLib's own kernels: correct by definition, about 70x slower than the champion on the MLP.
-/

namespace Submission

open ExactRuntime

def kernels : Kernels := specKernels

theorem correct : Obligation kernels := rfl

end Submission
