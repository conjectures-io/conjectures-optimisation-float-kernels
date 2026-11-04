# Plan: operations upgrade and the next competition season

Status: draft, 2026-11-04. Branch `feat/competition-upgrade-ops` in the private copy
(`conjectures-optimisation-float-kernels-new-competition`). Source: Notion "Operations to add to
TorchLean competition – part of the upgrade".

This work stays private until launch. The public repository is the `upstream` remote; merge
`upstream/main` into this branch regularly so launch is a fast-forward, not a rebase of months of
public fixes.

## Where things are today

| Piece | Where | Today |
|---|---|---|
| Specification | this repo, `lean/contract/ContractExact.lean` + `ExactToolkit/` | 13 kernels in 5 families since 15402ba (12 before); `Layer` (12 cases) and `runModel` (one activation + one saved slot) |
| Competition | this repo, `validator/`, `lean/{verify,bench,audit}` | One submission implements all 13 kernels; boards are **model families** (`overall`, `mlp`, `resnet`, `mobilenet`, `normalized_mlp`, `mixer`), not kernels |
| `exactrt` | `conjectures-io/exact-inference`, `runtime/` | Runs `runModel` with the champion's kernels; `spec/` is a copy synced from this repo by `runtime/sync.sh` |
| `exact-c` | `exact-inference`, `c/` | Integer-only C99, tested bit for bit against `exactrt` |
| `.exrt` v1, importer | `exact-inference`, `tools/` | Sequential layers, implicit activation chain, binary32 only; ONNX importer maps ~15 ops with tight restrictions |

Facts that shape the plan:

1. **No season mechanism exists.** Rollouts happen by changing `proof_identity`/`benchmark_identity`
   (`fingerprint.py`, `validator/config.py:141`). Adding a field to `Kernels` makes every existing
   champion's source fail to compile; the board then pauses (`validator/db/evaluations.py:504-517`).
   What does help: a board name that is new gets an `activation_boundary`, so only later submissions
   compete on it (`evaluations.py:110-117,171`).
2. **A kernel earns only through models that use it.** The new kernels pay off only if the season
   adds model families that exercise them (`lean/bench/ExactSuite.lean`).
3. **FloatLib's `exp`/`log`/`tanh` (pinned rev `0d91825`) are deterministic approximations with no
   proved error or ULP bound, and are not correctly rounded.** They are opt-in imports
   (`FloatLib.Floats.Formats.BinaryInterchange.Transcendentals`).
4. **The model language can't express graphs.** `runModel` carries one activation and one saved
   slot. Concat/Split/branches, attention (Q/K/V from one input) and LSTM all need named buffers in
   the *specification*, not just in `.exrt`.
5. **The spec is shared.** `exact-inference` says its `spec/` is "not edited here". Anything
   `exactrt` runs must be defined in this repo first.

## Decisions to make first

**D1. Where the operations we build ourselves (Part 1) are defined.** Recommended: in this repo's
contract, but **outside the `Kernels` record**. A new `lean/contract/ExactOps.lean` holds plain spec
definitions (Add, Where, Transpose, …) that miners never implement. Reductions call `K.core.sum` /
`K.core.dot`, so miner speed-ups still reach them. The record and the miner obligation stay
unchanged for Part 1. `exactrt` can ship these early by syncing from this private branch, before any
season launches.

**D2. Graph semantics.** Recommended: a new `lean/contract/ExactGraph.lean` with buffers
(`Array (Array Word)`), nodes with input and output buffer ids, `runGraph K g`, and
`runGraph_eq : K = specKernels → runGraph K g = runGraph specKernels g`. Nodes are either competed
kernels (through `K`) or `ExactOps`. Keep `Layer`/`runModel` as they are, so `.exrt` v1 and the
current benchmark keep working. The new season's model families are written as graphs.

**D3. The definition of `exp`/`log`/`tanh`.** This decides what miners prove against.
- (a) **Pin FloatLib's `Model.exp`/`log`/`tanh` bit for bit**, as the Notion page assumes. This
  works today. The costs: miners prove equality to one particular approximation, `exact-c` must port
  FloatLib's algorithm exactly, and the "exact" brand weakens because results are not nearest to the
  true value.
- (b) **Correctly rounded binary32 `exp`/`log`/`tanh` defined in the contract** (nearest binary32 to
  the real function), with an executable slow reference. The semantics are cleaner and miners have
  more room to optimise. The costs: real proof work in our toolkit, and miners must prove error
  bounds and handle hard cases.

  Recommendation: decide after a one-week spike (task S1). If FloatLib's `exp` is fast enough to
  serve as a benchmark reference and portable to C, take (a) for this season.

**D4. Season mechanics.** Recommended: a season means a new contract, new board names (for example
`s2.overall`, `s2.transformer`, …), new identities and a new incumbent. The old boards are retired
from the config at the switch. This reuses the activation-boundary logic instead of new
champion-migration code. To decide: the board naming scheme, whether old champions get a farewell
payout window, and how the API in `conjectures-validator` and the dashboard show seasons.

**D5. Season scope.** Which Part 2 items enter this season and which wait. Suggested cut:
- **Season 2:** conv and pool variants; `exp`-family activations + Softmax; attention.
- **Season 3:** int8 (needs a dtype story, see P2.5); LSTM/GRU (candidate).

## Part 1 – built by us (mostly in `exact-inference`)

Order matters: the graph format unblocks most of the rest.

| # | Task | Repo | Notes |
|---|---|---|---|
| P1.0 | Fix `sync.sh` so it also copies `ContractExactChecked.lean` | exact-inference | `spec/ExactToolkit/TensorProofs.lean:1` imports it; building the spec library fails today |
| P1.1 | `ExactGraph.lean` (D2) and `ExactOps.lean` skeleton | this repo | Contract-only; does not change `Kernels`. Check its effect on `validator/provenance.py` `specification_identity`, which hashes all `lean/contract/**`. Hold back from public `main` until the season |
| P1.2 | `.exrt` v2: buffer table, per-node in/out ids, dtype field reserved for int8 | exact-inference | `runtime/Runtime.lean`, `c/include/exact.h` (`param[9]`, `t[2]`, `ER_MAX_LAYERS` limits), `c/src/model.c`, `c/src/run.c` (replace the x/y ping-pong with a liveness-planned scratch), `tools/exrt.py`, `tests/gen.py`. `tests/check.sh:18` uses version 2 as its "bad version", so change it to 3. v1 stays readable |
| P1.3 | Element-wise binary/unary: Add Sub Mul Div Max Min Abs Neg Sqrt Reciprocal Where Greater Less Equal | both | Specs in `ExactOps` on FloatLib `Model.*`. Pin broadcasting rules and the boolean encoding (Where, comparisons). C: export `sqrt_word` from `c/src/norm.c:315` as `er_sqrt`; add min/abs/neg/compare to `scalar.c` |
| P1.4 | Cheap activations: Clip LeakyRelu PRelu HardSigmoid HardSwish | both | Specify each rounding step (HardSwish = `x·min(max(x+3,0),6)/6`: say where it rounds, or define it as one exact rational rounded once) |
| P1.5 | Reductions: ReduceSum/Mean/Max/Min/L2/SumSquare, ArgMax | both | Through `K.core.sum`/`K.core.dot`, then a correctly rounded div/sqrt. ArgMax tie rule: first index, as `exactrt`'s `argmax` already does |
| P1.6 | Data movement: Transpose Pad Slice Gather Tile Expand Resize(nearest) | both | No arithmetic; the spec is index maps. Pin the out-of-range and negative-index semantics |
| P1.7 | Concat, Split, branches | both | Fall out of P1.2 |
| P1.8 | DFT and mel filter bank as fixed-weight `linear` | exact-inference | Importer and tooling only. Pin how the weights are generated (exact rationals, then rounded) so they reproduce |
| P1.9 | ONNX-ML TreeEnsembleClassifier/Regressor | both | One new spec op: comparisons, then an exact sum of leaf values (`dot` with ones). Pin the post-transform and aggregate (SUM only at first) |
| P1.10 | ONNX importer rewrite onto the graph | exact-inference | `tools/onnx2exrt.py`: replace the main-path/`saved` model (`:206-243,284-285`) with a name→buffer map; check domain and opset; make `Constant` variants raise `Unsupported` instead of `KeyError` |
| P1.11 | Tests per op | exact-inference | `tests/gen.py` cases + MANIFEST regenerated by `exactrt`; `fuzz.py` kinds; `onnx_test.py` graphs (its Sigmoid-refused check moves to an op that stays unsupported); new `.c` files go into both `c/build.sh:16` and `tests/sanitize.sh:13` |

Each op follows the Notion flow: spec, then `exactrt` (true by definition), then `exact-c` (tested),
then the importer. `tests/run-cases.sh` already skips C output that says "is not implemented yet",
so `exactrt` can land first.

## Part 2 – new competition kernels (this repo, next season)

### Per-kernel work

Each kernel needs every item below. The file list comes from the repo survey.

1. **Spec** in `ContractExact.lean`, in the `xWith dot` style where a dot is involved: field in a
   family (or a new family), `spec*`, `specKernels`, family obligation, `obligation_of_families`.
2. **Validity**: `ContractExactChecked.lean` `*Valid` predicate, `Layer.valid`/graph equivalent,
   size lemma; `ExactToolkit/ModelProofs.lean` shape rules; `TensorProofs.lean` entry lemma.
3. **Incumbent**: `kernels/exact/incumbent/Kernel.lean` with a proof, plus `baseline` (`rfl`). The
   negative fixtures (`wrong`, `forge`, …) must compile against the new record.
4. **Audit**: `lean/audit/ExactAuditCore.lean` (`runCase`, `kernelNames`, mutants), `SpecProbe`,
   `SuiteAudit`, `WorkloadAudit`, `SpecProofs`; Python oracle `scripts/audit-exact-spec.py`
   (`reference`, `cases`, `edge_cases`) and `audit-exact-models.py`.
5. **Docs**: `docs/CONTRACT.md` (semantics, NaN/±0/inf), `RUNTIME.md`, `MATHEMATICAL-PROOFS.md`.

### Kernels

Status: P2.1 and P2.2 are implemented in 15402ba (contract, validity, toolkit lemmas, incumbent and
fixtures; 13 kernels) and documented in `docs/CONTRACT.md`. Open follow-ups:

- No benchmark model family uses `convTranspose` or the new conv/pool shapes yet (the builders
  use `ConvShape.simple` and `PoolShape.square`), so these features earn nothing until a model
  family that exercises them is added (D4/D5).
- `exact-inference` is not updated: `.exrt` tags, `exact-c` kernels and the importer still have
  the old conv/pool restrictions (Part 4).

| # | Kernel | Spec notes |
|---|---|---|
| P2.1 | Convolution variants *(done, 15402ba)* | Generalise `ConvShape` (`ContractExact.lean:90`): per-axis stride, dilation, 4-sided padding (SAME is resolved by the importer, not the spec), groups (depthwise becomes a special case; keep the field or derive it), 1-D as `h=1`. **ConvTranspose** is a new kernel: each output is one exact dot over the contributing taps. Keep the "bias inside the dot, one rounding" rule |
| P2.2 | Pooling variants *(done, 15402ba)* | `PoolShape` with kernel, stride, 4-sided padding, `ceil_mode`. Pin what padded cells are: max ignores them; for avg, decide `count_include_pad` (the divisor changes). GlobalMaxPool; non-square global average |
| P2.3 | `exp`-family activations | Exp, Log, Tanh, Sigmoid, Softmax, LogSoftmax, SiLU, GELU (tanh form), Elu, Softplus, on D3's `exp`. Pin each composition's rounding points, e.g. Softmax = `e_i = exp(x_i − max)` rounded, `s` = exact sum rounded once, `y_i = div(e_i, s)` |
| P2.4 | Attention | Scaled `QKᵀ` (scale folded into the exact dot, or applied after: pick one), mask (optional, later), Softmax from P2.3, then an exact dot with V. Q/K/V/output projections stay `linear`. Per head; multi-head is the graph's job |
| P2.5 | int8 *(season 3?)* | QuantizeLinear, DequantizeLinear, QLinearConv, QLinearMatMul. Needs an int8 tensor type in the contract (today everything is `Word = UInt32` binary32) and in `.exrt` v2. Rescaling rule: an exact rational `(Σ(x−zx)(w−zw))·sx·sw/sy`, rounded half-even once, `+zy`, saturate. FloatLib has `Numerics/Quantization/Affine.lean` over ℚ to build on |
| P2.6 | LSTM / GRU *(candidate)* | Each gate is an exact dot then Sigmoid/Tanh from P2.3; pin the gate order and the rounding of the state update. Probably a graph pattern rather than a monolithic kernel, unless profiling says otherwise |

### Benchmark: new model families (and therefore new boards)

`lean/bench/ExactSuite.lean` builders, `cells`, `profileModel`; `validator/config.py` (`MODELS`,
`KINDS`, the hard-coded `/6` cell weight at `:268-273`); `validator/benchmark.py:25`
(`LEAN_MODELS`); `scripts/exact-config.py` (board names, weights, references). Candidate
families:

- `transformer`: attention, Softmax, GELU, LayerNorm, residuals (needs `ExactGraph`); replaces
  the Mixer's "ReLU instead of GELU" compromise.
- `mobilenet_v3`: HardSwish, squeeze-and-excite (Sigmoid), dilated/strided conv.
- `cnn_v2` or update `resnet`: padded/strided pooling, `ceil_mode`, grouped conv.
- `quantized_cnn` (with int8), `recurrent` (with LSTM/GRU) in a later season.

The benchmark harness runs `runModel`; it must run `runGraph` for the new families.

## Part 3 – season mechanism (this repo + `conjectures-validator`)

- Config: a `season` id; board names namespaced by season (D4); new `VERSION`
  (`validator/config.py:20`); regenerate `proof_identity`, `benchmark_identity`, references.
- Check the switch path end to end: pending evaluations are cancelled when the config hash changes
  (`evaluations.py:125-129`); old proved submissions are not re-verified (`:182-185`); a new board's
  first reference comes from `reference.sha256` (`:186-191`). Find out who is paid on a new board
  before its first champion.
- Source policy (`validator/sandbox.py:403-406`) allows only `import ContractExact`. Either
  `ContractExact` imports the new modules, or the allow-list grows.
- `fingerprint.py` `CONTRACT` (`:20-22`): add `ExactOps.lean`/`ExactGraph.lean` if they sit outside
  `ExactToolkit/`.
- API and dashboard (`conjectures-validator`, `conjectures-dashboard`): season label, archived
  boards. The central API moved to `conjectures-validator` (commit `69815cf`).
- Miner-facing: `MANUAL.md`, `README.md`, `docs/BENCHMARKING.md` (kernel count updated to 13 with
  P2.1/P2.2; they still list the current boards), and a migration note on carrying a Season 1 entry's kernels over.
- Rehearsal: the full local run in `docs/LOCAL-EXPERIMENTS.md` with the new contract, then a dev
  deployment, before the public merge.

## Part 4 – Part 2 kernels in `exactrt` / `exact-c`

After the contract freezes: `sync.sh` the new contract and incumbent, then add `.exrt` tags and C
kernels for P2.x, then importer mappings (conv/pool restrictions in `onnx2exrt.py:413-454` lifted
to match the spec). `exact-c` gets speed only from techniques we port (Notion step 5).

## Spikes before committing to scope

- **S1. `exp` cost and portability (D3).** Benchmark FloatLib `Model.exp` through `exactrt --spec`
  on a Softmax-sized input. Read `expWithProvider` with `Config.forFormat` and estimate porting it
  to integer C.
- **S2. Graph spec prototype (D2).** Write `ExactGraph.lean` with three node kinds and prove
  `runGraph_eq`. Check that miners' proofs are unaffected (they prove `K = specKernels`, and the
  graph only consumes `K`).
- **S3. Season dry run (D4).** On a dev DB, rename one board and change identities. Watch
  cancellation, the activation boundary, reference pickup and rewards.

## Suggested order

1. Decide D1–D5; run S1–S3 in parallel (S1 informs D3, S3 informs D4).
2. P1.0–P1.2 (graph + `.exrt` v2): the foundation for everything else.
3. P1.3–P1.11 in `exact-inference`, shipping `exactrt` releases from the private spec as each op
   lands.
4. Part 2 kernels on this branch, one family at a time (conv/pool → `exp` family → attention), each
   with spec, incumbent, audit and oracle before moving on.
5. New model families and boards; Part 3 season mechanics; rehearsal.
6. Launch: merge into public `upstream/main` with the season config, deploy coordinator, workers
   and auditor together (`docs/OPERATIONS.md:34-38,136-140`).
7. Part 4 follow-through in `exact-inference`.

## Open questions

- Is int8 in season 2 or 3 (it needs a new tensor type end to end)?
- Who owns `exact-inference` access? The current SSH key cannot clone it (only API reads work).
- Do tree ensembles stay non-competed (Notion: "could become a board later")?
- How is the season announced to miners ahead of time without revealing the contract early?
