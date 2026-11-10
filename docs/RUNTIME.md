# Kernel guide

A submission is one file importing only `ContractExact`, with `Submission.kernels` and
`Submission.correct : ExactRuntime.Obligation Submission.kernels`. The obligation is equality
with `specKernels`, for all inputs. Start from [the shipped implementation](../kernels/exact/incumbent/Kernel.lean)
and follow the [manual](../MANUAL.md) to verify and submit it.

## Kernel record

| Family field | Kernels | Meaning |
|---|---|---|
| `core` | `sum`, `dot` | Exact sum / sum of products, rounded once. |
| `dense` | `linear`, `gemm` | Exact dot per output; linear includes bias before rounding and optional ReLU. |
| `conv` | `conv2d`, `depthwise`, `convTranspose` | Exact dot of the contributing taps plus bias, rounded once; optional ReLU. Grouped, dilated, strided and padded; depthwise is `conv2d` with one group per channel. |
| `elem` | `avgpool`, `maxpool`, `addRelu`, `scaleShift` | Average/max pooling over `PoolShape` windows, residual addition, channelwise fused scale and shift. |
| `norm` | `rmsnorm`, `layernorm` | Exact rational statistics with pinned denominator and output rounding. |
| `attn` | `scores`, `mix`, `softmax`, `silu` | Attention dots, softmax and SiLU (no scored board uses them in this competition). |
| `rnn` | `lstm`, `gru` | Whole-sequence recurrent layers: exact gate dots, correctly rounded `sigmoid`/`tanh`, exactly rounded state updates. |

The names of these proof/record families are **not** separate reward boards. Boards evaluate
complete inference models. Every entry supplies every kernel, even when only one is changed.
The core functions do not earn independent scores merely by becoming faster; their benefit must
reach the models that use them. See [the benchmark](BENCHMARKING.md).

## Starting implementation

The shipped incumbent (dev entry #15; the telemetry baseline is the same file) builds on the
contract's `Pinned.kernels`: an exact limb accumulator, digit-based final rounding (ending in
`FastRound.roundW`) and word-level maximum pooling. Dot-based layers reuse that exact dot
implementation; residual addition and scale/shift use the specs. Normalization uses
`rmsnormWith dotFn` and `layernormWith dotFn`. The incumbent replaces `conv2d` and supplies the
`attn` and `rnn` families.

The accumulator sums finite products exactly before final binary32 rounding. Its proof connects
that integer representation to FloatLib's exact dot product. Special inputs follow the pinned
FloatLib semantics.

Its own fast paths, each proved equal to its spec:

- **`conv2d` (`convFast`):**
  - Depthwise layers (one group per channel, `cout = cin`) take one exact dot per output over the
    window taps, without the general group arithmetic.
  - Pointwise 1×1 layers (stride 1, no padding or dilation, one group) take one dot over the input
    channels plus the bias, through `dotSel`.
  - Every other shape uses the generic `conv2dWith dotFn` (`convFast_eq`, `pointwiseConv_eq`).
- **`sigmoid`, `tanh` and `exp`:**
  - `FastAct.sigmoidF`, `FastAct.tanhF` and `FastAct.expF`; `exp` covers `x ≤ 0`, the softmax
    case.
  - `e^(−|x|)` is enclosed in a 64-bit fixed-point interval: tables of `e^(−n)` and `e^(−j/64)`,
    and a short Taylor sum. Every product is rounded in the safe direction.
  - Each quotient is a `Float` guess, then checked exactly.
  - The result is accepted only when both ends of the enclosure round to the same binary32 word.
    Otherwise, and for `|x| ≥ 17`, the spec decides.
- **Word rounding (`FastRound.roundW`, in the contract):**
  - Rounds `±m · 2^e` to binary32 by bit operations when the result is a normal number below
    `2^127`.
  - Proved equal to FloatLib's `roundDyadic`, about 9× faster than `finishWord`.
- **Attention (`dotSel`):**
  - `scores`, `mix` and the softmax sum use `dotSel`. Sums of more than two terms go to `dotWin`;
    the others stay with `dotFn`.
  - `dotWin` sums the products on a window below the largest exponent, truncated, in 64-bit
    words.
  - The exact total then lies within `n` window units. When both ends round to the same word,
    that word is `dotFn`'s (`dotWin_spec`).
- **LSTM and GRU:** they keep `dotFn`. Their operands are computed on each read, and the window
  path reads them twice; measured, it was slower.

The other season-1 kernels keep the launch implementation. Open:
- a single-pass window sum for computed operands;
- a fast range beyond `|x| < 17`;
- reusing decoded weights across the `T` steps.

You may use a different representation, traversal or fast path, provided the complete equality
proof passes.

## Proof structure

`CoreObligation`, `DenseObligation`, `ConvObligation`, `ElementObligation`, `NormObligation`,
`AttnObligation` and `RnnObligation` allow family proofs to be composed with `obligation_of_families`. Alternatively, rewrite individual
kernel equalities to prove `Obligation` directly, as the starting file does.

The [toolkit](../lean/contract/ExactToolkit/) supplies word decoding, exact accumulators and
rounding lemmas. The [contract](CONTRACT.md) documents tensor layouts, valid shapes, special
values and normalization. The [mathematical inventory](MATHEMATICAL-PROOFS.md) connects these
executable definitions to rational/real mathematics and model-shape semantics.

## Useful local tools

After setup, copy your entry to `lean/work/Submission.lean` and build the target in `lean/`.

| Target / command | Purpose |
|---|---|
| `VerifyExact` | Check the complete correctness statement. |
| `benchExact` | Native inference timings/checksums; see the manual. |
| `exactDev profile mlp` | Layer costs; also accepts `cnn`, `mnet`, `nmlp`, `mixer`, `tfm`, `rnn`. |
| `exactDev` | Developer comparisons of exact reductions with reference/native arithmetic. |
| `exactAudit --json 20 971` | Compare all kernels with their specs on generated and directed cases. |
| `exactAudit --malformed 20 971` | Include arrays shorter than their declared shapes. |
| `exactAudit --selftest` | Check the spec against itself and test the audit's planted mutants. |

Direct Lean/native commands are development tools. Run the full [proof gate](PROOFS.md) before
submitting; matching test outputs does not replace a proof.
