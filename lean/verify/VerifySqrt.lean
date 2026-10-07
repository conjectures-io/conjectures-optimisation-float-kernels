import Contract
import Submission

/-!
# The square-root slot's gate: three lines, written by the verifier, never by a miner

If `Submission.sqrt_spec` proves anything weaker than the pinned obligation, this does not
typecheck. `#print axioms` must then show Lean's three and nothing else.
-/

theorem accepted : Competition.SqrtObligation Submission.sqrt := Submission.sqrt_spec

#print axioms accepted
