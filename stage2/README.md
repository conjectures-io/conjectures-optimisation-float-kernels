# Stage 2 draft: tensor kernels

Stage 2 adds tensor slots next to stage 1's scalar ones: a miner submits a faster `sum` or
batched matmul over binary32 and a Lean proof that it equals TorchLean's reference. It reuses the
stage-1 gate model (fixed statement, kernel replay, allowed axioms, timed against an incumbent);
this directory is the draft contract, kernels and measurements, kept in its own Lake workspace so
the stage-1 gate in `../lean` is untouched. Nothing here is wired into `gate.py` yet.

## The contract

`Stage2/Contract.lean` states two obligations against
[TorchLean](https://github.com/lean-dojo/TorchLean)'s `NN/Runtime/Autograd/Engine/Cuda/KernelSpec.lean`:

| Slot | Kernel type | Must equal |
|---|---|---|
| sum | `Vector Binary32 n → Binary32` | `reduceSumLeftSpec`: a left fold of `ExecFloat.add` from +0 |
| bmm | `(batch m n p) → Vector Binary32 (batch*m*n) → Vector Binary32 (batch*n*p) → Vector Binary32 (batch*m*p)` | `bmmSpec`: row-major, each output a left fold of `add acc (mul a b)` from +0 |

Equality is exact, so the accumulation order is part of the contract. TorchLean's scalar is
FloatLib's `ExecFloat.Binary 8 23`, the same type as stage 1's `Binary32`.

## What is proved

`Stage2/Kernels.lean` implements both slots in the specs' order with stage 1's best proven scalar
kernels (`Scalar/TunedAdd.lean` from `submissions/tuned`, `Scalar/FastMul.lean` from
`submissions/mul-fast`, namespaces renamed). `Stage2/Proofs.lean` proves `SumObligation sumFast`
and `BmmObligation bmmFast` in about 40 lines, by composition of the stage-1 theorems with list
lemmas, and checks that they rest only on `propext`, `Classical.choice` and `Quot.sound`.

## What was measured

One pinned core, best of 5–7 runs; ratios against the proved stage-1 composition unless noted.
Every variant produces the reference checksum, so all are bit-identical to the spec.

| Kernel | Ratio |
|---|---|
| sum, proved composition, vs `ExecFloat` reference | 0.53 |
| bmm 64³ and 128³, proved composition, vs reference | 0.57 |
| raw `UInt32` word loops (no `Binary32` wrapper) | 0.92–0.96 |
| fused dot, product passed to the add as fields | 0.87–0.91 |
| fused sum, accumulator kept unpacked | 0.99–1.15 |
| matmul, four output chains interleaved | 1.06–1.07 |
| **matmul 64³, half of A zero, skipping `acc + (±0 · finite)`** | **0.48** |

Findings:

- Most of a tensor kernel's speed comes from its scalar ops, so the stage-2 incumbent has to be
  built from the current stage-1 winners; otherwise the slot pays for copying.
- On dense data, loop structure and fusion are worth 5–10% at most: the scalar op is
  throughput-bound, not latency-bound.
- The real stage-2 headroom is data-aware exact shortcuts on realistic inputs. Post-ReLU
  activations are about half zeros; stage 1 never times zeros. The bench should time a dense class
  and a ReLU-like class (possibly pruned weights too).

The zero-skip kernel (`bench/Fused.lean`, `mmSkip`) is not proved yet. It needs two FloatLib
facts: a signed zero times a finite value is a signed zero, and a finite nonzero plus a signed
zero is unchanged. That proof is the next step.

## Building

```bash
cd stage2
lake update
lake build                                # Scalar, Stage2 (proofs), both benches
taskset -c 3 .lake/build/bin/stage2bench  # reference vs proved composition
taskset -c 3 .lake/build/bin/fused        # unproved headroom probes
```

Requirements beyond stage 1:
- A C compiler on `PATH` as `cc`. TorchLean compiles a small C library that Lean loads even for
  plain imports; install `build-essential` or clang.
- The Mathlib build is shared with stage 1 (same revision). Copying `../lean/.lake/packages` into
  `.lake/` before `lake update` avoids rebuilding it.

## Open decisions

- Reward split across the five slots, and whether stage 2 opens only after stage 1 has run.
- The timed data classes and their weights.
- When a stage-1 winner is promoted, the stage-2 incumbent is rebuilt from it; the gate needs a
  rule for that.
