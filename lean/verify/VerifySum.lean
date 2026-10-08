import ContractTensor
import Submission

/-!
# The sum slot's gate: three lines, written by the verifier, never by a miner

If `Submission.sum_spec` proves anything weaker than the pinned obligation, this does not
typecheck. `#print axioms` must then show Lean's three and nothing else.
-/

theorem accepted : Competition.SumObligation Submission.sum := Submission.sum_spec

#print axioms accepted
