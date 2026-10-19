import ContractExact
import Submission

/-!
# The exact-runtime slot's gate: one statement, written by the verifier, never by a miner

The entry's 12 kernels, in five families, equal the pinned specs, so every model in the suite runs
exactly as the spec models do (`ExactRuntime.runModel_eq`). `#print axioms` must show Lean's three
and nothing else.
-/

theorem accepted : ExactRuntime.Obligation Submission.kernels := Submission.correct

#print axioms accepted
