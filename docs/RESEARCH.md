# Research background

These are development directions and optional targets. They do not define the
current competition's workload, reward policy, or leaderboard.

## TorchLean specification links

The gate's tensor contract restates the pinned TorchLean sum and batched matrix
multiplication definitions over FloatLib. The optional
[Stage2/Link.lean](../lean/stage2/Stage2/Link.lean) proves that these definitions
agree with TorchLean's. The gate uses the restated contract; it does not build
TorchLean during each submission.

The optional stage2 targets retain implementations, composition proofs, and
exploratory benchmarks:

```bash
cd lean
lake build Stage2 stage2bench fused
```

These targets require a C compiler for TorchLean's native library. Setup fetches
TorchLean sources even though the gate builds different targets.

## Exact tensor optimisations

[Stage2/Kernels.lean](../lean/stage2/Stage2/Kernels.lean) composes the repository's
certified scalar arithmetic into tensor loops. Further experiments include raw
word loops, fused internal representations, and zero-heavy input shortcuts.
These need their own correctness proofs before becoming submissions.

A matching checksum on an experiment does not establish bitwise equivalence on
all inputs. In particular, signed zeros, infinities, NaNs, and accumulation order
can invalidate otherwise familiar shortcuts.

## Earlier exploratory directions

Earlier investigations considered CROWN certificate checking, end-to-end TorchLean
training, and ports to zkVM instruction sets. Those workloads have different
bottlenecks and are outside the current full-entry contract. Framework overhead,
proof-checking cost, and instruction-set support must be measured directly for
those settings; kernel benchmark improvements do not establish gains there.

Evaluate those directions with reproducible measurements of their actual target
workloads and hardware.
