import Contract
import Submission

/-!
# The fused multiply-add slot's gate: three lines, written by the verifier, never by a miner

If `Submission.fma_spec` proves anything weaker than the pinned obligation, this does not
typecheck. `#print axioms` must then show Lean's three and nothing else.
-/

theorem accepted : Competition.FmaObligation Submission.fma := Submission.fma_spec

#print axioms accepted
