import Contract
import Submission

/-!
# The multiplication slot's gate: three lines, written by the verifier, never by a miner

If `Submission.mul_spec` proves anything weaker than the pinned obligation, this does not
typecheck. `#print axioms` must then show Lean's three and nothing else.
-/

theorem accepted : Competition.MulObligation Submission.mul := Submission.mul_spec

#print axioms accepted
