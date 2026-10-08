import ContractTensor
import Submission

/-!
# The bmm slot's gate: three lines, written by the verifier, never by a miner

If `Submission.bmm_spec` proves anything weaker than the pinned obligation, this does not
typecheck. `#print axioms` must then show Lean's three and nothing else.
-/

theorem accepted : Competition.BmmObligation Submission.bmm := Submission.bmm_spec

#print axioms accepted
