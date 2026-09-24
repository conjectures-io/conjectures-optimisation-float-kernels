import Contract
import Submission

/-!
# The gate: three lines, written by the verifier, never by a miner

If `Submission.add_spec` proves anything weaker than the pinned obligation, this does not
typecheck. `#print axioms` must then show Lean's three and nothing else.
-/

theorem accepted : Competition.Obligation Submission.add := Submission.add_spec

#print axioms accepted
