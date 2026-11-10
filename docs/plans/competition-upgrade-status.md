# Status: operations to add to the TorchLean competition

As of 2026-11-10, `main` (private repo), which includes the merged branch `feat/exact-competition-upgrade`, which combines
`feat/competition-upgrade-ops`, `feat/floatlib3-attention`, `feat/real-model-benchmark` and
`feat/recurrent-family`. Tracks the Notion task "Operations to add to TorchLean competition – part of the
upgrade". The plan is in [competition-upgrade.md](competition-upgrade.md).

Legend: ✅ done · ⏳ in progress · ❌ not started · ➖ not needed

The columns follow the Notion page's five steps for every addition:

1. **Spec**: defined in `lean/contract/ContractExact.lean` (exact result, NaN, infinity and zero
   rules).
2. **exactrt**: runs the spec's own definition.
3. **exact-c**: implements it and is tested against `exactrt`.
4. **Importer**: `tools/onnx2exrt.py` maps the ONNX operation.
5. **Competition** (Part 2 only): miners make it fast under proof.

Steps 2–4 live in `conjectures-io/exact-inference`; the port of the new specification there is in
progress on its branch `feat/sync-competition-upgrade`.

## Summary

| | Items | Done | In progress | Not started |
|---|---|---|---|---|
| Part 1 – we build ourselves | 8 | 0 | 0 | 8 |
| Part 2 – subnet competition | 6 | 5 (spec level) | 0 | 1 |

Done at spec level: the **convolution variants** and **pooling variants** (proved, cross-checked
against an independent oracle and ONNX Runtime); **`exp` activations** (FloatLib 3's correctly
rounded `exp`, and correctly rounded `sigmoid` and `tanh` proved in
`ExactToolkit/ActivationProof.lean`); **attention** (`scores`, `mix`, `softmax`, `silu`); and
**recurrent** (`lstm`, `gru`, with a `recurrent` board in `overall`, nine trained real fixtures and
meaning proofs in `ExactToolkit/RecurrentProofs.lean`). See
[recurrent-family.md](recurrent-family.md). Not started: int8.

## Part 1 – we build ourselves

None started. Most of this work happens in `exact-inference`. What exists today is the
partial coverage from before this upgrade.

| Addition | ONNX operations | 1 Spec | 2 exactrt | 3 exact-c | 4 Importer | Notes |
|---|---|---|---|---|---|---|
| Element-wise math | Add Sub Mul Div Max Min Abs Neg Sqrt Reciprocal Where Greater Less Equal | ❌ | ❌ | ❌ | ❌ | Today only a residual `Add` and a per-channel constant `Add`/`Mul` exist, through `addSaved` and `bn` |
| Cheap activations | Clip LeakyRelu PRelu HardSigmoid HardSwish | ❌ | ❌ | ❌ | ❌ | Only ReLU exists, fused into layers |
| Reductions | ReduceSum/Mean/Max/Min/L2/SumSquare, ArgMax | ❌ | ❌ | ❌ | ❌ | Will reuse the competed `sum`/`dot` kernels |
| Data movement | Transpose Pad Slice Gather Tile Expand Resize (nearest) | ❌ | ❌ | ❌ | ❌ | Importer only aliases Flatten/Reshape/Squeeze/Unsqueeze |
| Graph model format | Concat, Split, branches | ❌ | ❌ | ❌ | ❌ | Needs `.exrt` v2 and a graph semantics in the spec (plan D2). Blocks attention and LSTM models |
| Exact preprocessing | DFT, mel filter bank | ➖ | ❌ | ❌ | ❌ | Fixed-weight `linear` layers; tooling only |
| Tree ensembles | TreeEnsembleClassifier/Regressor | ❌ | ❌ | ❌ | ❌ | One new spec operation |
| ONNX importer | mappings for all of the above | ➖ | ➖ | ➖ | ❌ | Needs a rewrite onto the graph format |

## Part 2 – subnet competition

| Addition | ONNX operations | 1 Spec | 2 exactrt | 3 exact-c | 4 Importer | 5 Competition |
|---|---|---|---|---|---|---|
| Convolution variants | dilation, asymmetric padding, `auto_pad=SAME`, separate strides, general groups, native 1-D, `ConvTranspose` | ✅ | ❌ | ❌ | ❌ | ✅ kernels and proofs; ❌ no benchmark model, not launched |
| Pooling variants | padded/strided pooling, `ceil_mode`, `GlobalMaxPool`, non-square global average | ✅ | ❌ | ❌ | ❌ | ✅ kernels and proofs; ❌ no benchmark model, not launched |
| `exp`/`log` activations | Exp Log Tanh Sigmoid Softmax LogSoftmax SiLU GELU Elu Softplus | ⏳ Exp, Sigmoid, Tanh, Softmax, SiLU (correctly rounded); others ❌ | ⏳ | ⏳ | ❌ | ✅ through attention and recurrent kernels |
| Attention | Attention, scaled MatMul + Softmax | ✅ `Layer.attention` | ⏳ | ❌ | ❌ | ➖ no scored board (transformers left out for small devices) |
| int8 | QLinearConv QLinearMatMul QuantizeLinear DequantizeLinear | ❌ | ❌ | ❌ | ❌ | ❌ Needs an int8 tensor type end to end |
| Recurrent | LSTM GRU | ✅ `lstm`, `gru`, `lastStep` | ⏳ | ⏳ | ⏳ | ✅ `recurrent` board in `overall`, nine trained fixtures |

### Convolution variants, feature by feature

| Feature | Status | How |
|---|---|---|
| Dilation | ✅ | `ConvShape.dilH/dilW` |
| Asymmetric padding | ✅ | `ConvShape.padT/padL/padB/padR`; padding cells are +0 terms, as in ONNX |
| `auto_pad=SAME` | ➖ in the spec / ❌ in the importer | The spec takes explicit pads; the importer will compute SAME padding into them |
| Separate strides | ✅ | `ConvShape.strideH/strideW` |
| General groups | ✅ | `ConvShape.groups`; weights `cout × cin/groups × kh × kw`. `depthwise` is now `conv2d` with one group per channel (same bits as before) |
| Native 1-D | ✅ | A shape with `h = kh = 1`; no separate kernel |
| `ConvTranspose` | ✅ new kernel `conv.convTranspose` | `ConvTShape` adds `outPadH/outPadW`. Each output is an exact sum over only the taps that reach the input, plus the bias, rounded once. `convTranspose_total` proves an output equals the bias plus input × weight over exactly the taps with `iy·strideH + dy·dilH = y + padT` (and likewise for columns) inside the image |

### Pooling variants, feature by feature

| Feature | Status | How |
|---|---|---|
| Padded and strided pooling | ✅ | `PoolShape` with rectangular `kh × kw` windows, per-axis strides, four-sided padding. Padding is never a value |
| `ceil_mode` | ✅ | `PoolShape.ceilMode`. A last window that would start in the padding is dropped (the ONNX Runtime/PyTorch rule) |
| `GlobalMaxPool` | ✅ | `maxpool` with `PoolShape.global c h w` |
| Non-square global average | ✅ | `avgpool` with `PoolShape.global c h w` |
| `count_include_pad` (extra) | ✅ | `PoolShape.countIncludePad` picks average pooling's divisor |

### Competition-side work for convolution and pooling

| Piece | Status | Where |
|---|---|---|
| Spec and validity rules | ✅ | `lean/contract/ContractExact.lean`, `ContractExactChecked.lean` |
| Shape and meaning proofs | ✅ | `lean/contract/ExactToolkit/ModelProofs.lean`, `TensorProofs.lean` |
| Incumbent and fixtures proved against the 19-kernel contract | ✅ | `kernels/exact/*/Kernel.lean`. The incumbent (dev entry #15, also the telemetry baseline) builds on the contract's pinned season-1 kernels and adds proved fast paths: correctly rounded `sigmoid`, `tanh` and `exp`, a window dot product for attention, and depthwise and 1×1 `conv2d`. Generalised convolution and pooling and `convTranspose` have no fast paths |
| Independent oracle | ✅ | `lean/audit/SpecProbe.lean`, `scripts/audit-exact-spec.py`. Agrees on 16,883 cases and 51 models. Bit-identical to ONNX Runtime 1.31 for conv2d, depthwise, convTranspose and both pools |
| Differential audit with planted mutants | ✅ | `lean/audit/ExactAuditCore.lean`. 13 kernels; new valid-shape generators for every conv/pool feature; `--selftest` catches 57/57 mutants (20 new, e.g. convTranspose adding +0 for taps that reach no input, maxpool reading padding as +0, ceil mode without the last-window rule) and fails if a geometry feature goes untested; `exactAudit 20 42` passes on the incumbent (5,460 cases, 0 mismatches) |
| Shape/workload audits, proof inventory, showcase, dev bench | ✅ | `lean/audit/{SuiteAudit,WorkloadAudit,ExactSuiteProofs,SpecProofs}.lean`, `lean/showcase/Showcase.lean`, `lean/bench/ExactDev.lean`, `scripts/audit-exact-models.py`, `scripts/prepare-kernel-optimizations.py`. All targets build; the Python shape mirror agrees with Lean on 6,048 valid and invalid shapes; proof-axiom check passes (58 theorems, standard axioms only) |
| Docs | ✅ | `docs/CONTRACT.md`, `RUNTIME.md`, `MATHEMATICAL-PROOFS.md`, `README.md`, `MANUAL.md` |
| Benchmark model family using the new features | ❌ | Boards are model families, so these kernels earn nothing until a model uses them (plan D4/D5) |
| Season rollout (boards, identities, config) | ❌ | Plan Part 3 |
| Full CI gate on the branch | ✅ except one sandbox-only check | Run locally: all 12 Lean targets build; proof-axiom check (64 theorems, standard axioms only), audit self-test, incumbent audit, oracle, model and workload checks, fast tests and ruff pass. Gate fixture matrix (`selftest.py`, run without the sandbox): 10 of 11 verdicts match, including the incumbent, baseline, `wrong`, `sorryax` and every policy attack. The 11th, `tamper` with the policy bypassed, is caught only by the sandbox's overlay layer, so it needs bubblewrap 0.11.2, which this machine lacks (`sudo apt-get install meson ninja-build pkg-config libcap-dev && bash scripts/install-bubblewrap.sh`) |

## Decisions blocking further progress

- **D3 (decided):** `exp`, `sigmoid` and `tanh` are correctly rounded. `log` and the remaining
  activations are still open.
- **D2:** graph semantics in the spec. Blocks the Part 1 graph format and multi-branch models.
- **D4/D5:** season mechanics and scope, including which model families and boards carry the
  new kernels.
