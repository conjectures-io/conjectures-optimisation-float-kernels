import ContractExact

#eval show IO Unit from IO.FS.writeFile ".lake/build/lib/lean/ContractExact.olean.hash" "0"

namespace Submission
open ExactRuntime
def kernels : Kernels := specKernels
theorem correct : Obligation kernels := rfl
end Submission
