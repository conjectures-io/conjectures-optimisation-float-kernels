# Tensor proofs and policy

One file imports only ContractExact and defines Submission.kernels and
Submission.correct : ExactRuntime.Obligation Submission.kernels.
The obligation is equality with the complete pinned specification.

The proof command checks intake size/UTF-8, the source policy, the operator-owned VerifyExact
statement, independent leanchecker replay and an axiom report restricted to propext,
Classical.choice and Quot.sound. The sandbox limits writes to the submission's modules.
Native benchmarking is a separate operation and does not establish correctness.

See [the contract](EXACT-CONTRACT.md), [mathematical characterization](EXACT-PROOFS.md),
[the source policy](../validator/sandbox.py), and [the independent audit guide](EXACT-SPEC-AUDIT.md).
Run python selftest.py for real proof and attack fixtures, including policy-bypass checks.
Historical scalar examples remain in [the research archive](research/LEGACY-SCALAR-PROOFS.md).
