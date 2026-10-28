# Proof checks and source policy

One nonempty UTF-8 file, at most 256,000 bytes, imports only `ContractExact` and defines
`Submission.kernels` and `Submission.correct : ExactRuntime.Obligation Submission.kernels`.
The obligation is equality with the complete pinned specification, for every input.

## Gate stages

1. **Intake:** size, UTF-8 and file checks.
2. **Policy:** permitted import, options and attributes; no forbidden constructs.
3. **Statement:** the operator-owned `VerifyExact` checks the full obligation.
4. **Kernel replay:** `leanchecker` independently replays `Submission` and `VerifyExact`.
5. **Axioms:** only `propext`, `Classical.choice` and `Quot.sound` are permitted.

Builds are sandboxed without network access, with read-only shared code, private writable build
outputs and resource limits. Native benchmarking is a separate operation and does not establish
correctness. A statistical rejection does not invalidate a proved implementation.

The policy rejects `sorry`, extra axioms, native-decision proof shortcuts, external/unsafe code,
custom macros/elaborators, startup commands and other escapes. Only explicitly allowed options
and attributes are accepted. The exact allowlist, limits and token checks are in
[validator/sandbox.py](../validator/sandbox.py); avoid relying on incidental parser tricks.

```bash
PYTHONPATH=.:validator .venv/bin/python -m verification my-entry --verdict results/proof.json
```

Run `.venv/bin/python selftest.py` for real proof and attack fixtures, including policy-bypass
checks. See [the contract](CONTRACT.md), [mathematical characterizations](MATHEMATICAL-PROOFS.md)
and [independent audits](SPEC-AUDIT.md) for the guarantees and their assumptions.
