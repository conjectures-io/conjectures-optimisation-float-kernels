# Specification and kernel audits

Proof checking, executable testing and independent specification checks answer different questions.
None of these developer commands submits work or publishes weights.

| Check | What it establishes |
|---|---|
| Submission proof gate | The implementation equals the pinned specification under Lean's permitted axioms. |
| Differential kernel audit | Tested compiled outputs agree with the specifications, including directed edge cases. |
| Independent arithmetic oracle | Tested specification outputs agree with a separate rational/rounding implementation. |
| Mathematical characterizations | The executable definitions satisfy the stated tensor and arithmetic theorems. |
| Model validation | Generated shapes, finite activations and input sensitivity on the selected cases. |

## Differential test of an entry

After Linux setup:

```bash
PYTHONPATH=.:validator GATE_AUDIT_CASES=20 GATE_AUDIT_SEED=971 \
  .venv/bin/python -m audit my-entry --verdict results/my-entry-audit.json
```

This rebuilds the source in the sandbox and compares all kernels with `specKernels` on random
and directed inputs. The report includes case count, mismatch count and seed. A clean audit is
finite test evidence, not a replacement for the correctness proof.

For direct developer runs, copy the entry to `lean/work/Submission.lean`, then:

```bash
(cd lean && ~/.elan/bin/lake build exactAudit)
lean/.lake/build/bin/exactAudit --json 20 971
lean/.lake/build/bin/exactAudit --malformed 20 972
lean/.lake/build/bin/exactAudit --selftest
```

Directed cases include zeros/signs, subnormals, exponent gaps, cancellation, ties, overflow,
NaNs/infinities, and small/unequal shapes. `--selftest` checks the harness against the spec
and planted incorrect implementations. Native commands run trusted local code directly.

## Independent oracle for the specification

The Python oracle uses exact rational arithmetic and an independently implemented nearest-even
binary32 rounder. It checks words and small complete model traces, not floating-point tolerances.

```bash
(cd lean && ~/.elan/bin/lake build exactSpecProbe auditExactShapes)
.venv/bin/python scripts/audit-exact-spec.py --native --edge-cases \
  --cases-per-kernel 200 --seed 42 --output data/spec-audit/seed-42.json
.venv/bin/python scripts/audit-exact-models.py --random-seeds 4
.venv/bin/python scripts/check-exact-proofs.py --replay
```

`exactSpecProbe` tests the specification rather than a submitted kernel. The last command checks
the operator's mathematical proof modules and their axioms, with independent kernel replay.
See [MATHEMATICAL-PROOFS.md](MATHEMATICAL-PROOFS.md) for exact theorem statements and assumptions.

## Assurance limits

The proof establishes equality with the written contract. Human review and independent oracles
help establish that the contract expresses the intended operation. Shape theorems do not establish
numerical safety for every possible input; finite tests do not establish universal correctness.
Compiler/runtime/hardware behavior and measurement noise remain separate concerns.

The optional [champion auditor](WORKERS.md#independent-champion-auditor) performs report-only
checks independently of processing and rewards. Its findings require operator investigation.
Earlier measured results are preserved as [dated research records](research/README.md), not current
coverage counts or promises about a newly changed kernel.
