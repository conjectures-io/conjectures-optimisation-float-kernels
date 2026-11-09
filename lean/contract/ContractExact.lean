import ExactToolkit.Spec
import ExactToolkit.FastRound
import ExactToolkit.Pinned

/-!
# The exact-runtime contract

The specs and the obligation (`ExactToolkit.Spec`), the word rounding shared by fast paths
(`ExactToolkit.FastRound`) and the pinned season-1 kernels (`ExactToolkit.Pinned`). An entry
supplies the `attn` and `rnn` families; its `kernels` takes every other family from
`Pinned.kernels`.
-/
