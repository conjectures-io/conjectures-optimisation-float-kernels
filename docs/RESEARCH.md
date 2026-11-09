# Research background

These are development directions and optional targets. They do not define the
current competition's workload, reward policy, or leaderboard.

## TorchLean specification links

An earlier scalar contract restated TorchLean's left-fold sum and batched matrix multiplication over
FloatLib, and `lean/stage2/Stage2/Link.lean` proved them equal to TorchLean's. Those links concerned
that sequential-rounding contract, not the current round-once tensor contract, and were removed
with the move to FloatLib 3.0.0: the TorchLean revision they used builds only against the earlier
FloatLib. The scalar kernels they composed remain in `lean/stage2/Scalar`.

## Exact tensor optimisations

Experiments such as raw word loops, fused internal representations and zero-heavy input shortcuts
need their own correctness proofs before becoming submissions.

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
