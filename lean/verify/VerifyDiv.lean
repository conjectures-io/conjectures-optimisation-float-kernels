import Contract
import Submission

/-!
# The division slot's gate: three lines, written by the verifier, never by a miner

If `Submission.div_spec` proves anything weaker than the pinned obligation, this does not
typecheck. `#print axioms` must then show Lean's three and nothing else.
-/

theorem accepted : Competition.DivObligation Submission.div := Submission.div_spec

#print axioms accepted
