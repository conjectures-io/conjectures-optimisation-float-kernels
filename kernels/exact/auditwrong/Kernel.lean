import ContractExact

/-!
# Exact runtime, `auditwrong` fixture

Kernels that differ from the spec only on subnormal inputs to `sum`, with no proof. It stands for
a wrong kernel that got past the proof checks (a Lean bug or an exploit): the proof stages reject
this file, but the audit (`gate.py --audit`) must catch it from its outputs alone.
-/

namespace Submission

open ExactRuntime

/-- The spec's kernels, except that `sum` flushes subnormal inputs to zero. -/
def kernels : Kernels :=
  { specKernels with core := { specCore with
      sum := fun x => sumSpec (x.map fun w => if expOf w == 0 then w &&& 0x80000000 else w) } }

end Submission
