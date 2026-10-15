import ContractExact

/-!
# Exact runtime, `wrong` fixture

Proves something weaker than the obligation; the gate must reject it at the statement stage.
-/

namespace Submission

open ExactRuntime

def kernels : Kernels := specKernels

theorem correct : kernels.core.sum = sumSpec := rfl

end Submission
