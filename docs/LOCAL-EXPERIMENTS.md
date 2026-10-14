# Local single-kernel experiments

The paid upload is the full four-kernel entry. Single-kernel slots are useful for
isolating a local change; their historical API boards are read-only and unpaid.
An improvement here must be included with all four proofs in a full entry before
uploading.

## Slots and contracts

| Slot | Contract import | Definition and theorem |
| --- | --- | --- |
| `add` | `Contract` | `Submission.add`, `add_spec` |
| `mul` | `Contract` | `Submission.mul`, `mul_spec` |
| `fma` | `Contract` | `Submission.fma`, `fma_spec` |
| `div` | `Contract` | `Submission.div`, `div_spec` |
| `sqrt` | `Contract` | `Submission.sqrt`, `sqrt_spec` |
| `sum` | `ContractTensor` | `Submission.sum`, `sum_spec` |
| `bmm` | `ContractTensor` | `Submission.bmm`, `bmm_spec` |

The gate defaults to `full`; specify the slot for these experiments:

```bash
source .venv/bin/activate
cp -r kernels/add/incumbent my-add
python gate.py --slot add --bench my-add
python gate.py --slot add my-add
```

The local slot's shipped reference is not the current full-entry server champion.
Use the full benchmark to assess the combined effect.

## Example implementations

| Directory | Technique illustrated |
| --- | --- |
| `kernels/add/word` | Call the certified word backend directly |
| `kernels/add/direct` | Avoid carrier-codec work on a guarded path |
| `kernels/add/mirror` | Mirror the word algorithm and prove equivalence |
| `kernels/add/fastlog` | Replace a helper with a proved equivalent binary-search logarithm |
| `kernels/add/tuned` | Special-value tests and word-level rounding helpers |
| `kernels/add/textbook-unproved` | An unfinished alternative rounding algorithm; not uploadable |
| `kernels/mul/fast` | Reuse certified rounding and multiplication proofs |
| `kernels/sum/tuned` | Compose certified addition in the required fold order |
| `kernels/bmm/fast` | Compose certified arithmetic in matrix loops |
| `kernels/full/incumbent` | Combine all four implementations and proofs |

These are source examples and test fixtures, not claims about their ranking or
speed on current production hardware. [expected.tsv](../kernels/expected.tsv)
records test expectations. Keep those test bounds separate from public metrics.

## Development techniques

- Guard a fast path and fall back to the certified implementation for other cases.
- Replace a helper only after proving equivalence over its required domain.
- Experiment with allowed inlining attributes and inspect the measured result.
- Move invariant indexing work out of tensor loops while proving the indices equal.
- Measure the full entry after changing arithmetic used inside tensor kernels.

FloatLib's FMA specification rounds an exact product-plus-addend once; separate
rounded multiplication and addition are not equivalent in general. Similarly,
reordering a sum can violate exact equality. The `wrong` fixtures illustrate
statement failures; they are not examples to upload.

See [Proofs](PROOFS.md) for the actual obligations and policy, and
[Benchmarking](BENCHMARKING.md) for measurement methodology.
