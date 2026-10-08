import ContractTensor
import Submission

/-!
# The full entry's gate: one statement, written by the verifier, never by a miner

An entry proves all four at once: FloatLib's `add` and `mul`, and TorchLean's `sum` and `bmm`. If
any `_spec` proves anything weaker than its pinned obligation, this does not typecheck.
`#print axioms` must then show Lean's three and nothing else.
-/

theorem accepted :
    Competition.Obligation Submission.add ∧ Competition.MulObligation Submission.mul ∧
      Competition.SumObligation Submission.sum ∧ Competition.BmmObligation Submission.bmm :=
  ⟨Submission.add_spec, Submission.mul_spec, Submission.sum_spec, Submission.bmm_spec⟩

#print axioms accepted
