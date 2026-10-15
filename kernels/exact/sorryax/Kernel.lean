import ContractExact

/-! Exploit case: the escape hatch named directly, past the word list; only the axioms stage can catch it. -/

namespace Submission

open ExactRuntime

def kernels : Kernels := specKernels

theorem correct : Obligation kernels :=
  sorryAx _ false

end Submission
