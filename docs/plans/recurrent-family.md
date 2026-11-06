# Plan: a recurrent (LSTM/GRU) model family, its kernels, datasets and trained fixtures

Status: draft, 2026-11-06. Branch `feat/recurrent-family`, on top of `feat/real-model-benchmark`
(mixed synthetic + real suite, a511aef). Builds on [competition-upgrade.md](competition-upgrade.md)
item P2.6 ("LSTM / GRU *(candidate)*") and follows the path the attention family took in b5b71c0.

Scope decisions (2026-11-06): the target is **small devices**. This ships as a **new competition**
whose `overall` includes `recurrent`. **No transformer board**: transformers may be too big for the
target. The `attn` kernels stay in the record but no scored model uses them.

## Implementation status (2026-11-06)

- **R2a is decided and done**: `ExactToolkit/Activation.lean` defines `tanhCR`/`sigmoidCR` as an
  enclosure search on FloatLib's certified `exp` enclosure, then exact log comparisons when the
  search does not settle. `ExactToolkit/ActivationProof.lean` proves both are the nearest-even
  rounding of the real function for every finite input (`toReal_tanhModel`,
  `toReal_sigmoidModel`), with only Lean's standard axioms. The staged R2b fallback is not
  needed. `siluSpec` now uses the correctly rounded sigmoid.
- Contract: `rnn` family (`lstm`, `gru`), `Layer.lstm`/`gru`/`lastStep`, checked validity, shape
  proofs, incumbent (`lstmWith dotFn …`), synthetic `rnn` cells and their suite proofs, the
  `recurrent-v1` real adapter, `realEval`, spec probe ops, audit cases and mutants.
- Python: validator/config (`recurrent` board, adapter validation, `--real-recurrent`), the
  independent oracle (correctly rounded activations, LSTM/GRU), training pipeline under
  `training/recurrent/`.
- Docs: CONTRACT, RUNTIME, BENCHMARKING, README, MANUAL.

Evidence (this machine, AMD Ryzen 5 PRO laptop):

- The independent Python oracle agrees with the Lean spec probe on 14,468 cases (seed 20261019)
  and 20,948 cases (seed 7, 200 per kernel), including 404 `sigmoid`, 404 `tanh`, 417 `lstm`,
  417 `gru`, their checked variants, and 69 model traces with the `rnn` builders. The first run
  exposed a resource blow-up: `tanh`/`sigmoid` of inputs like `5.7e6` sent FloatLib's binary-grid
  `exp` to millions of bits. The enclosures now use constant bounds there (`|x| > 16` for tanh,
  `x > 32` and `x < −128` for sigmoid), proved in `contains_tanhEnclosure` and
  `contains_sigmoidEnclosure`.
- Seven trained fixtures (UCI-HAR LSTM and GRU, Appliance GRU, MNIST-rows LSTM, Spoken Arabic Digit
  LSTM, Tiny Shakespeare LSTM, MIT-BIH GRU) evaluated with `realEval` (the bit-exact spec):
  accuracy equal to PyTorch's and argmax agreement 100% on the evaluated prefixes, largest
  output difference 1e-5.
- Cost: one correctly rounded activation is about 20 µs. Synthetic `rnn:b1` cells take
  about 65–250 ms (mean about 120 ms); real fixtures 90–310 ms per inference (about 1.5 s for one
  of each). The recurrent family's measurement plan comes to about 3 minutes against its 6-minute
  cap.
- Builds: `VerifyExact` (incumbent with `lstmWith dotFn …`), `benchExact`, `exactSpecProbe`,
  `realEval`, `exactDev`, `auditExactWorkloads`, `validateExact`, `ExactSuiteProofs`.
  `ExactSuiteProofs`, `ExactDev` and `WorkloadAudit` had been broken since 15402ba (the
  generalised convolution); they are ported here. Python: 488 tests pass.

Still open:

- `exactAudit` and `auditExactShapes` (`ExactAuditCore`, `SuiteAudit`) are still on the
  pre-15402ba shapes on this branch. The port exists as e62e762 on `feat/competition-upgrade-ops`;
  merge it, then the recurrent audit cases and mutants added here compile.
- Speech Commands v2 fixtures: loader and configs are written, but the 2.4 GB download and the
  training were not run. MIT-BIH is under-trained (94.5%, 15 epochs).
- S-R2 calibration on the benchmark workers; the new asset bundle revision on Hugging Face.
- Phase F (`exact-inference`).

## Summary

- Add a sixth spec family `rnn` with two **whole-sequence** kernels, `lstm` and `gru` (17 → 19
  kernels), plus `Layer.lstm`, `Layer.gru` and a pure `Layer.lastStep`. The record field gets a
  default (`rnn := specRnn`), as `attn` did, so every existing champion still compiles and proves.
- Gates are exact dots (input, recurrent and both biases as terms of one dot, rounded once); the
  state updates are exact 2- and 3-term dots rounded once. `sigmoid` and `tanh` are **correctly
  rounded** (R2), proved in our toolkit on FloatLib's exact log comparisons. A time-boxed spike
  decides this; the staged definitions are the fallback.
- New board `recurrent` (Lean cell model `rnn`), part of `overall` from the start of the new
  competition.
- The cost of the spec's correctly rounded `exp` dominates recurrent inference (~30 µs per
  activation vs ~14 ns per MAC, measured below), so the models must be small, and the first
  proved fast `sigmoid`/`tanh` will move this board a lot. That is deliberate but must be sized.
- Real fixtures: start with three pools already in the bundle, whose sample rows are already
  time-major `T × F`: UCI-HAR, Appliance Energy and MNIST read row by row. Then add Speech
  Commands v2, Spoken Arabic Digit, MIT-BIH (rebuilt from PhysioNet) and Tiny Shakespeare
  (see [Datasets](#datasets)). Training code is committed this time (`training/recurrent/`),
  unlike the existing fixtures, whose trainers live only under the ignored `data/` tree.

## Where things are today

| Piece | Where | Relevant fact |
|---|---|---|
| Spec | `lean/contract/ContractExact.lean` | 17 kernels, 6 record families; `Layer` (14 cases) runs on `(activation, saved)`; no per-step state, no element-wise multiply, no slicing |
| Attention precedent | b5b71c0 (54 files) | Monolithic `Layer.attention` + `AttnKernels` with a default in `Kernels`; `sigmoidW`, `expW` already in the contract |
| FloatLib 3.0.0 (3fd7c01) | `Transcendentals/CorrectlyRounded/Runtime.lean` | Correctly rounded `exp`, `expMinus1`, `log`, `logPlus1`, `expMul`. **No correctly rounded `tanh`** |
| Synthetic suite | `lean/bench/ExactSuite.lean` | `cells`, `cellModel`, `profileModel` (typical/scale/boundary); weights `wts` use a ReLU gain of 4 |
| Real suite | `lean/bench/RealSuite.lean`, `MainExact.lean` | Adapters `image-zoo-v1`, `uci-har-v1`, `appliance-mixer-v1`; each real row is reported under its family's `b1:normal` synthetic cell (`realCellModel`), so a family needs a synthetic cell first |
| Fixture build | `scripts/split-real-fixtures.py` | Converts already trained `.bin` files from `data/real-model-backtest/` (not in git) into weights + content-addressed sample pools + manifests; labels omitted |
| Validator | `validator/config.py` (`MODELS`, `BATCHES`), `benchmark.py` (`LEAN_MODELS`), `workload.py` (`RealWorkloadSpec.parse`/`validate_assets`), `scripts/exact-config.py` | Board names and adapters are hard-coded lists |

### Cost measurement (rough, needs spike S-R1)

FloatLib 3 `benchExact` from `results/bench-2026-11-05/wt-fl3`, seed 42, typical profile, one run:
`mlp:b1` 0.75 ms (55k MACs) and `tfm:b1` 24.2 ms (75k MACs, 640 exp-based activations plus two
norms). After subtracting MACs at ~14 ns and the norms, each activation (exp + add + div [+ mul])
costs roughly **30 µs**. A recurrent step needs `5H` activations (LSTM) or `3H` (GRU):

| Model | MACs | Activations | Est. spec time |
|---|---:|---:|---:|
| LSTM H=32, T=128, F=9 (UCI-HAR as is) | 0.7 M (≈10 ms) | 20,480 | ≈0.6 s |
| LSTM H=32, T=64, F=9 | 0.35 M | 10,240 | ≈0.3 s |
| GRU H=32, T=64, F=9 | 0.26 M | 6,144 | ≈0.19 s |
| GRU H=32, T=24, F=8 (Appliance as is) | 0.08 M | 2,304 | ≈0.07 s |
| LSTM H=64, T=128 | 2.5 M | 40,960 | ≈1.5 s |

Activations are ~97% of the time. Two consequences: (1) keep hidden sizes ≤ 48 and sequences
≤ ~100 steps; (2) the first entry that proves a fast `sigmoid`/`tanh` gets a very large ratio on
this board. After that, the MAC side (weight reuse across steps) decides.

## Decisions

**R1. Kernel granularity: whole-sequence kernels (recommended).** `lstm`/`gru` take the whole
`T × inW` input and return all `T × H` hidden states. Alternatives: a per-step cell kernel driven
by the model, or a graph pattern (`ExactGraph`, D2 of the upgrade plan, not built). A whole
sequence lets an entry decode/lay out the weights once for all `T` steps and hoist the input
projection out of the recurrence. That reuse is the interesting optimisation for RNNs, and it
needs no graph spec. It is also the attention precedent.

**R2. `sigmoid` and `tanh` semantics.** Options:
- (a) **Correctly rounded `tanhCR` and `sigmoidCR` (recommended, behind spike S-R0)**: the nearest
  binary32 to the real function, ties to even. Proved in our toolkit (like `SqrtProof.lean`), not
  upstream. Both reduce to comparisons FloatLib already proves (`ElementaryComparison.compareLog`,
  exposed in FloatLib 3), plus Mathlib's `Real.artanh` (`tanh_artanh`, `strictMonoOn_artanh`):
  - for `0 < b < 1`: `tanh x < b ⟺ 2x < log((1+b)/(1−b))`;
  - for `0 < b < 1`: `σ(x) < b ⟺ x < log(b/(1−b))`.

  Rounding is `AlgebraicRounding.round fmt sign (compare x)`, the same fallback `expMinus1Rat`
  uses, so termination and the rounding theorem are inherited. `tanh x` is irrational for
  rational `x ≠ 0`, so it never lands on a midpoint. Specials: NaN quieted, `±∞ → ±1`, `±0 → ±0`
  (tanh); `+∞ → 1`, `−∞ → +0` (sigmoid). For speed, an enclosure search goes first: an
  `exp`/`expMinus1` enclosure mapped through the monotone `e/(e+2)` (tanh) or `1/(1+e)`
  (sigmoid). It returns early when both ends round alike; otherwise the comparisons decide.
- (b) **Staged (fallback)**: `tanhW` = `a = |x|`, `t = expm1(−(a + a))`,
  `sign(x) · (−t / (t + 2))`, FloatLib steps in this order; `sigmoidW` as in the contract today.
  No overflow or NaN for finite inputs, accurate near 0, about 2–3 ulp, not correctly rounded.
- (c) `tanh = 2·sigmoid(2x) − 1`: cancellation near 0. Reject.

  Spike S-R0 (3–4 days): the comparison-only `tanhCR` with its theorem
  `toReal_tanhCR_eq_roundAt` and the Python oracle, then the enclosure fast path and a timing
  against `tanhW`. If it lands, do `sigmoidCR` the same way and use both. If not, ship (b). Either
  way `siluSpec` follows `sigmoid`, so the record stays consistent.

**R3. Biases.** Keep PyTorch's two bias vectors (`b_ih`, `b_hh`) as separate exact terms. The
export is then a straight copy of `nn.LSTM`/`nn.GRU` state, and GRU needs `b_hn` separate anyway.

**R4. Rollout: a new competition.** New contract, `VERSION`, proof/benchmark identities and
incumbent. The scored boards are `mlp`, `resnet`, `mobilenet`, `normalized_mlp`, `mixer`,
`recurrent` and `overall`, which includes `recurrent`. `transformer` is neither scored nor
generated by default. The `rnn` field keeps a default (`specRnn`), as `attn` does, so negative
fixtures and copies of older entries still compile, but no champion carries over. Season
naming and board namespacing follow D4 of the upgrade plan. S-R2 checks that the whole `overall`
plan, recurrent included, fits the 20-minute budget before launch.

**R5. Sizing rule.** Target, with spec activations: synthetic `rnn` cells ≤ ~100 ms mean and
≤ 250 ms worst per inference; each real fixture ≤ ~300 ms; the family's real+synthetic plan
fits the 6-minute family cap at 12 paired rounds with warm-ups. Re-derive after S-R1.

## Specification

Lean sketch for `ContractExact.lean` (names final, bodies indicative; write the step loop as a
`List.foldl` over `List.range T`, as `runModel` does, not `Id.run do`, so rewriting proofs stay
`simp`-friendly):

```lean
-- R2a: `sigmoidCR`, `tanhCR : Word → Word` come from `ExactToolkit/ActivationProof.lean`, with
-- `toReal_tanhCR_eq_roundAt : finite x → (tanhCR x).toReal = roundAt (Real.tanh x.toReal)`.
-- R2b fallback, staged:
/-- FloatLib's correctly rounded `e^x − 1` on a word. -/
@[inline] def expm1W (w : Word) : Word :=
  toWord (Model.Transcendentals.CorrectlyRounded.expMinus1 (ofW w))

/-- `tanh x = sign(x) · (−t / (t + 2))` with `t = expm1 (−2|x|)`, FloatLib's steps in this order. -/
def tanhW (w : Word) : Word :=
  let a := w &&& 0x7FFFFFFF
  let t := expm1W (negW (toWord (Model.add (ofW a) (ofW a))))
  toWord (Model.div (ofW (negW t)) (Model.add (ofW t) (ofW twoW))) ||| (w &&& 0x80000000)

/-- One LSTM layer, zero initial state, PyTorch gate order `i, f, g, o`.
`wih : 4H × inW`, `whh : 4H × H`, `bih bhh : 4H`; `x : T × inW`; output `T × H` (every `h_t`).
Step: `a_k = R(Σ wih[k]·x_t + Σ whh[k]·h + bih[k] + bhh[k])` (one exact dot);
`i f o = sig a`, `g = tanh a`; `c' = R(f·c + i·g)` (exact 2-term dot); `h' = R(o · tanh c')`. -/
@[specialize] def lstmWith (dot : DotFn) (sig tanh : Word → Word) (T inW H : Nat)
    (x wih whh bih bhh : Array Word) : Array Word := …

/-- One GRU layer, zero initial state, PyTorch gate order `r, z, n`.
`r, z = sig(one exact dot of x_t, h, both biases)`; `hn = R(Σ whh[n]·h + bhh[n])`;
`n = tanh(R(Σ wih[n]·x_t + bih[n] + r·hn))` (one exact dot, `hn` a term);
`h' = R(n − z·n + z·h)` (exact 3-term dot `[n, n, h]·[1, −z, z]`). -/
@[specialize] def gruWith (dot : DotFn) (sig tanh : Word → Word) (T inW H : Nat)
    (x wih whh bih bhh : Array Word) : Array Word := …

-- `sigmoidCR`/`tanhCR` if S-R0 lands (R2a), otherwise `sigmoidW`/`tanhW` (R2b)
def lstmSpec := lstmWith dotOf sigmoidCR tanhCR
def gruSpec := gruWith dotOf sigmoidCR tanhCR

structure RecurrentKernels where
  lstm : Nat → Nat → Nat → Array Word → Array Word → Array Word → Array Word → Array Word → Array Word
  gru : Nat → Nat → Nat → Array Word → Array Word → Array Word → Array Word → Array Word → Array Word

def specRnn : RecurrentKernels := ⟨lstmSpec, gruSpec⟩

structure Kernels where
  …
  attn : AttnKernels := specAttn
  rnn : RecurrentKernels := specRnn

inductive Layer where
  …
  | lstm (T inW H : Nat) (wih whh bih bhh : Array Word)
  | gru (T inW H : Nat) (wih whh bih bhh : Array Word)
  | lastStep (T n : Nat)          -- keep the last of `T` rows of `n` (no arithmetic)
```

Contract text to pin (`docs/CONTRACT.md`): gate orders; that `h0 = c0 = +0` and these zeros are
dot terms (so `+0·∞ = NaN` and zero-sign rules apply as for convolution padding); every rounding
point above; the activations' NaN payloads (quiet); stacking by consecutive
layers; `lastStep` on `T = 0` is empty. Checked domain: positive `T`, `inW`, `H`; exact lengths
`x = T·inW`, `wih = G·H·inW`, `whh = G·H·H`, biases `G·H` (`G = 4` LSTM, `3` GRU); finite weights
are not required (raw semantics are universal).

Why parameterise by `sig` and `tanh`: an entry proves a fast scalar `sigFast = sigmoidCR` once and
rewrites `lstmWith dotFn sigFast tanhFast` to the spec, exactly as `dotFn_eq_dotOf` works today.
No separate array `sigmoid`/`tanh` kernels: no model would call them, and a kernel earns only
through models.

Out of scope for v1 (can follow as layer flags without new kernels): bidirectional (`reverse`
flag), initial state inputs, projections (`proj_size`), peepholes, Elman `RNN_TANH`.

## Work list (this repo)

Phase A: contract and proofs, checked against the attention commit's file list.

| File | Change |
|---|---|
| `lean/contract/ContractExact.lean` | `sigmoidCR`/`tanhCR` re-exported from the toolkit (or `expm1W`/`tanhW` for R2b), `siluSpec` on the chosen sigmoid, `lstmWith`, `gruWith`, specs, `RecurrentKernels`, `specRnn`, `RnnObligation`, extend `obligation_of_families` (7 families), three `Layer` cases and `apply` |
| `lean/contract/ContractExactChecked.lean` | `LstmValid`/`GruValid`, `Layer.valid` cases, `lstmSpec_size`/`gruSpec_size` |
| `lean/contract/ExactToolkit/ModelProofs.lean` | Shape rules (`(T·H, saved)`, `lastStep → n`) and validity |
| `lean/contract/ExactToolkit/TensorProofs.lean` | Entry lemmas for the new kernels; activation facts (`tanh_neg`, finite → `|y| ≤ 1`, `sigmoid` in `[0, 1]`) |
| `lean/contract/ExactToolkit/` (new `Recurrent.lean`?) | Helper lemmas: `lstmWith_congr` (equal `dot`, `sig`, `tanh` ⇒ equal kernel), so entries rewrite in one step |
| `lean/contract/ExactToolkit/ActivationProof.lean` (new, R2a) | `sigmoidCR`, `tanhCR` runtime (enclosure search, then comparisons) and `toReal_*_eq_roundAt`, special-value lemmas; `fingerprint.py` picks it up via the `ExactToolkit/*.lean` glob |
| `kernels/exact/incumbent/Kernel.lean` | `rnn := ⟨lstmWith dotFn sigmoidCR tanhCR, gruWith dotFn sigmoidCR tanhCR⟩`; extend `correct` |
| `kernels/exact/{baseline,wrong,forge,…}` | Must still compile (default field); add one `wrong` variant on a gate order |
| `docs/MATHEMATICAL-PROOFS.md` | Connect the gate dots and state updates to their real-number formulas |

Phase B: benchmark harness (Lean).

| File | Change |
|---|---|
| `lean/bench/ExactSuite.lean` | `wtsGain` (gain 1 for recurrent weights, so pre-activations sit mostly in `|a| < 4`, not saturated); `rnnLayers kind seed T F H classes` = `[lstm T F H, gru T H H, lastStep T H, linear 1 H classes]` so every workload exercises both kernels; `cells` gets `rnn:b1:*`, `rnn:b4:*`; `cellModel`/`profileModel` sizes per R5 (indicative: typical `T∈[8,24]`, `F∈[8,24]`, `H∈{16,24,32}`; scale `T∈[32,64]`, `H=32`; boundary odd `T`, `H∈{15,17,31,33}`) |
| `lean/bench/RealSuite.lean` | Adapter `recurrent-v1` (below) |
| `lean/bench/MainExact.lean` | `realCellModel "recurrent" = "rnn"` |
| `lean/bench/ExactDev.lean` | `profile rnn`, layer labels |
| `lean/audit/{ExactAuditCore,SpecProbe,SuiteAudit,WorkloadAudit,SpecProofs,ExactSuiteProofs}.lean` | Kernel names, cases, mutants (swapped `f`/`i`; `tanh` via `2σ(2x)−1`; `c'` rounded twice; GRU `r` applied after rounding `hn+…`), cell-validity proofs for `rnn` cells |

**Adapter `recurrent-v1`.** One adapter for all recurrent fixtures, self-describing so new
datasets need no Lean change: tensor 0 is a header of raw `UInt32`s
`[version=1, T, F, classes, nLayers, kind₀, H₀, kind₁, H₁, …]` (`kind`: 0 = LSTM, 1 = GRU),
followed per layer by `wih, whh, bih, bhh`, then the head `w (classes × H_last)`, `b`.
Layers: the stack, `lastStep`, `linear 1 H classes`. `validate_assets` recomputes every expected
length from the header and rejects anything else; caps (`T ≤ 256`, `H ≤ 64`, `nLayers ≤ 2`) stop a
fixture from blowing the budget.

Phase C: validator and config (Python).

| File | Change |
|---|---|
| `validator/config.py` | `MODELS += ("recurrent",)`; `BATCHES["recurrent"] = (1, 4)`; `VERSION` bump |
| `validator/benchmark.py` | `LEAN_MODELS["recurrent"] = "rnn"` |
| `validator/workload.py` | `recurrent-v1` in the adapter map; header-driven `validate_assets` |
| `scripts/exact-config.py` | Board choice `recurrent` in the default board list and in `overall`'s model weights; `transformer` out of the generated boards; `--real-recurrent` switch (all manifests under `models/recurrent/`) |
| `scripts/{audit-exact-models,plot-*,compare-model-families,replay-model-family-admission,summarize-kernel-optimizations,test-exact-workflow}.py` | Board/model lists |
| `scripts/audit-exact-spec.py` | Python oracle: `tanh32`/`sigmoid32` correctly rounded from Decimal with adaptive precision, as `exp32` does today (tiny `x` needs precision above `−log10|x| + 40`), or the staged versions for R2b; `lstm`, `gru`; edge cases (NaN/±∞/±0/subnormal gates, `T = 1`, `H = 1`). For R2a also an exhaustive 2³² check of `tanhCR`/`sigmoidCR` against an independent correctly rounded C implementation (e.g. CORE-MATH's `tanhf`), offline |
| `deploy/platform/CONTRACT.md` | Board list |

Phase D: tests. `tests/test_exact_config.py` (new board, switch), `test_benchmark_budgets.py`
(recurrent budget), `test_workload_quality.py` / `test_model_family_experiment.py` (model lists),
`test_exact_spec_oracle.py` (oracle against `exactAudit --json` on recurrent cases),
`test_tensor_workflow.py` (a recurrent real workload end to end with a tiny fixture),
`workload.py` adapter rejection cases (bad header, oversize `T`, wrong lengths).

Phase E: docs. `docs/CONTRACT.md`, `RUNTIME.md` (kernel table: 19 kernels, family `rnn`),
`BENCHMARKING.md` (board, fixtures, budgets), `README.md`/`MANUAL.md` kernel count and boards,
`exactDev profile rnn`.

Phase F: `exact-inference` (outside this repo; Part 4 of the upgrade plan). Sync the contract,
add `.exrt` tags for `lstm`/`gru`/`lastStep`, port the chosen `sigmoid`/`tanh` to `exact-c` (for R2a any correctly rounded binary32 `tanhf`/sigmoid is bit-identical by definition, e.g. CORE-MATH), and map
ONNX `LSTM`/`GRU` (forward, default activations, `linear_before_reset = 1` for PyTorch-style GRU;
reject everything else).

## Datasets

Survey 2026-11-06 (licences and split sizes checked on primary pages; values marked (u) are typical
figures not confirmed at the source). Cost = activations per inference at the proposed size, times
the ~30 µs estimate. Tiny-RNN accuracies in the literature are for larger hidden sizes; ours are to
be measured.

### Recommended

| # | Dataset | Task | Fixture input (T×F) | Model(s) | Test pool | Licence | Est. spec time |
|---|---|---|---|---|---|---|---|
| 1 | UCI-HAR *(in bundle)* | 6-class activity | 64×9 (2× decimated) or 128×9 as is | LSTM H=32, GRU H=32 | 2,947 | CC BY 4.0 (already used) | 0.31 / 0.19 s at T=64 |
| 2 | Appliance Energy *(in bundle)* | 6-step forecast (regression) | 24×8, **existing pool byte for byte** | GRU H=32 | 2,932 | CC BY 4.0 (already used) | 0.07 s |
| 3 | Sequential MNIST by rows *(in bundle)* | 10-class | 28×28, **existing MNIST pool byte for byte** (rows of the 784 vector) | LSTM H=32 | 10,000 | as the MNIST fixtures (CC BY-SA 3.0 per Keras docs; record it) | 0.13 s |
| 4 | Google Speech Commands v2, 12-class | Keyword spotting | 49×40 MFCC (40 ms window, 20 ms hop) | GRU H=32; 2-layer LSTM→GRU H=32 (stack coverage) | 4,890 (TFDS 12-class test) | CC BY 4.0 | 0.14 s; stack ≈0.38 s |
| 5 | Spoken Arabic Digit (UCI) | 10-class spoken digits, MFCC given | ≤93×13 variable → resample to 48×13 (or pre-pad to 64) | LSTM H=32 | 2,199 (separate test file, other speakers) | CC BY 4.0 | 0.23 s |
| 6 | MIT-BIH heartbeats, rebuilt from PhysioNet `mitdb` with Kachuee's 187-sample steps | 5-class AAMI beats | 187×1 → 2× decimated 94×1 | GRU H=32 | ≈21,900 | ODC-By 1.0 (PhysioNet); **not** the Kaggle copy ("Unknown") | 0.27 s |
| 7 | Tiny Shakespeare | Next-character prediction (65 classes) | 64×65 one-hot (no embedding layer needed; see below) | LSTM H=32 | ≥1,000 windows from the last 10% of the text | text public domain (u); check the char-rnn copy's terms | 0.31 s |

Phase 1 = rows 1–3: no new downloads or licence review, and rows 2–3 reuse existing sample
files (they are content-addressed, so they deduplicate in the bundle). Phase 2 = rows 4–7: each adds
a modality the bundle lacks (speech features, precomputed MFCC with short sequences, univariate
physiology, text). All eight to nine fixtures together cost about 2–2.5 s of spec inference per
"one of each". At 12 rounds × (candidate + champion) × (scored + warm-up), that is about 2 min,
within the 6-minute family cap; S-R1 must confirm this.

Preprocessing notes:
- **Speech Commands**: pad or trim to 16,000 samples; log-mel → DCT, 40 coefficients, in numpy with
  pinned parameters; ship only features. The 2.4 GB of audio is needed once, offline, and is never
  in the bundle. Use the official `validation_list.txt`/`testing_list.txt` (hash-based split).
  Literature (Hello Edge, 12-class v1): GRU 93.5%, LSTM 92.9% at ~4–5 M ops. Expect lower at H=32.
- **Spoken Arabic Digit**: split the UCI files on blank lines. Variable lengths need a fixed `T`:
  resampling keeps the last step meaningful, which matters because the model reads `lastStep`. If
  you pad instead, **pre-pad** (zeros before the utterance), never post-pad. z-score with training
  statistics.
- **MIT-BIH**: resample to 125 Hz, segment on the annotated R-peaks, normalise to [0, 1], pad to
  187, then decimate. The standard split shares patients between train and test, so accuracy is
  optimistic (inter-patient de Chazal splits are harder). That is fine for a latency benchmark;
  record it. Credit PhysioNet and the MIT-BIH paper.
- **Tiny Shakespeare**: the vocabulary comes from the whole file and is recorded in the manifest. A
  one-hot input makes `wih·x_t` mostly `+0` terms, so a proved zero-skipping fast path is legitimate
  and realistic for embedding-free char models. If we later want a true embedding, add a pure
  `Layer.embedding` (a gather, no kernel), with input words that are integer ids.
- Everything is float32, standardised with training statistics only, fixed-length, labels kept
  out of the bundle (accuracy is recorded in the manifest and README).

### Considered and rejected

| Dataset | Why not |
|---|---|
| ETTh1/ETTm1 | CC BY-ND: no derivatives, so preprocessed windows can't be redistributed |
| AG News, IMDB, SST-2, Penn Treebank | Non-commercial, unclear or LDC-licensed text; SST-2 test labels are hidden |
| Jena Climate | No licence found |
| Kaggle "ECG Heartbeat Categorization" copy | Licence "Unknown"; rebuild from PhysioNet instead (row 6) |
| UCR/UEA archive copies (ECG5000, FordA, ElectricDevices, Wafer, StarLightCurves) | Archive grants no licence (password-protected zip). Use UCI/PhysioNet originals where they exist. FordA/StarLightCurves are also too long for the activation budget |
| FSDD, JapaneseVowels, UEA Epilepsy/Heartbeat | Test pools of 138–370: too small for hidden sampling |
| AudioMNIST | No official split |
| WISDM, PAMAP2, UCI Electricity | CC BY 4.0 and usable, but repeat UCI-HAR / Appliance Energy; keep as reserves |
| enwik8/text8 | 100 MB and CC BY-SA; Tiny Shakespeare covers char-level text |
| Character Trajectories (UCI, CC BY 4.0, 1,436 test, 20 classes, ≤182×3) | Good reserve: swap in for Spoken Arabic Digit if we prefer a non-audio sensor modality |

Sources: TFDS `speech_commands` card; arXiv 1804.03209 (Speech Commands); arXiv 1711.07128
(Hello Edge); UCI datasets 195, 175, 128, 321, 507, 231; physionet.org `mitdb` 1.0.0;
github.com/zhouhaoyi/ETDataset LICENSE; TFDS `tiny_shakespeare`; arXiv 1911.01071.

## Model and training plan

### Code layout (new, committed)

```text
training/recurrent/
  README.md            how to reproduce every fixture; pinned versions; expected accuracies
  requirements.txt     torch (CPU), numpy, scikit-learn, pinned
  data.py              one loader per dataset → (train, val, test) float32 arrays [N, T, F] + labels,
                       train-only normalisation statistics, deterministic splits
  models.py            RecurrentClassifier(kind, F, hidden sizes, classes): nn.LSTM/nn.GRU stack
                       (batch_first, bias=True, no dropout at export), last step, nn.Linear head
  train.py             --dataset --kind --hidden --layers --seed; Adam, clip 1.0, cosine LR,
                       early stopping on val; writes checkpoint + metrics.json
  export.py            checkpoint → recurrent-v1 weights.bin (header + PyTorch tensors, f32 LE)
  spec_eval.py         runs the Lean spec on the test pool (via `realEval`, below) and compares
                       predictions with PyTorch fp32
```

### Steps

1. **Spike S-R1 (1–2 days): activation cost.** Microbench `sigmoidCR`/`tanhCR` (or `sigmoidW`/`tanhW`) and `expW` per call
   on the FloatLib 3 build (a small `lean_exe`), and a 1-layer LSTM/GRU spec at H ∈ {16, 32, 48},
   T ∈ {24, 64, 128}. Replace the ~30 µs estimate; fix the R5 sizes.
2. **Contract + incumbent (Phase A)** before any training, so the export layout is final.
3. **`realEval` executable** (`lean/bench/RealEval.lean`): loads a fixture and a sample pool,
   runs `specKernels` (or the incumbent, same outputs) on every row, prints the argmax/outputs.
   Used only offline; it validates the adapter end to end and gives the *spec* accuracy.
4. **Train phase 1 on existing pools** (no new data licensing):
   - `uci-har-lstm`: LSTM H=32, 1 layer, T=64 (UCI-HAR decimated 2× by averaging pairs; a new
     sample pool) or T=128 if S-R1 allows; 9 → 6 classes. Expected ≈ 88–92% test accuracy.
   - `uci-har-gru`: GRU H=32, same input.
   - `appliance-gru`: GRU H=32, T=24, F=8 → 6-step forecast (regression head, 6 outputs), the
     existing chronological pool reused byte for byte. Metric: test RMSE vs the Mixer fixture.
   - `mnist-rows-lstm`: LSTM H=32 over the 28 rows of the existing MNIST pool. Expected ≈ 97–98%.
5. **Train phase 2 on new datasets** (rows 4–7 of [Datasets](#datasets)): Speech Commands GRU
   and a 2-layer LSTM→GRU stack (so stacking is covered by a real model), Spoken Arabic Digit
   LSTM, MIT-BIH GRU, Tiny Shakespeare LSTM.
6. **Acceptance per fixture** (recorded in its manifest and `training/recurrent/README.md`):
   - spec accuracy within 0.5 pp of PyTorch fp32 (regression: RMSE within 1%), and argmax
     agreement ≥ 99.5% on the full test pool;
   - accuracy clearly above a trivial baseline (majority class / last-value forecast);
   - gate saturation: fraction of gate pre-activations with `|a| > 8` below 50% on the test pool
     (a model whose gates are all saturated rewards shortcut fast paths, not real speed);
   - est. spec inference within R5;
   - weights finite; inputs standardised with train statistics only; no test example used for
     training or early stopping.
7. **Package**: extend `scripts/split-real-fixtures.py` (or add `build-recurrent-fixtures.py` on
   the same helpers) to write `models/recurrent/<dataset>/<kind>/{weights.bin,manifest.json}` and
   content-addressed `samples/recurrent-v1/<dataset>/<hash>/samples.bin` (labels omitted, as now).
   Add `license`, `attribution` and `source_url` to each manifest and an `ATTRIBUTION.md` at the
   bundle root (CC-BY datasets require it). Run `scripts/index-benchmark-assets.py`, push to the
   private Hugging Face dataset repo, pin the commit in the config.
8. **Calibrate (S-R2)**: self-comparison of the incumbent on `recurrent` and on `overall`
   (12 rounds) to check noise, that `realCellModel` rows report, and that `overall` with
   recurrent fits its 20-minute budget. If it doesn't, cut recurrent fixtures or rounds before
   launch, not the board.

### Training details

- Seeds fixed (`torch.manual_seed`, `numpy`, deterministic algorithms on CPU); record torch
  version and dataset checksums in `metrics.json`.
- Optimiser Adam (lr 2e-3, cosine to 1e-5), gradient-norm clip 1.0, batch 64, up to 60 epochs,
  early stopping on a validation split carved from *train* (UCI-HAR: hold out training subjects,
  so validation stays subject-disjoint like the test split).
- Hidden size and length are chosen by R5's cost budget first, accuracy second.
- Weight decay 1e-4; no dropout at export (inference is deterministic).
- Export order per layer: `weight_ih_l{k}`, `weight_hh_l{k}`, `bias_ih_l{k}`, `bias_hh_l{k}`
  (PyTorch row-major layouts already match `wih : G·H × inW`), then head weight and bias.
- `spec_eval.py` is the authority for fixture acceptance; PyTorch is only the trainer.

## Order and rough effort

| Step | Effort |
|---|---|
| S-R0 correctly rounded `tanh` (go/no-go for R2a) | 3–4 d |
| `sigmoidCR`, enclosure fast paths, exhaustive C cross-check (if S-R0 lands) | 4–7 d |
| S-R1 activation cost spike | 1–2 d |
| Phase A contract, toolkit lemmas, incumbent, fixtures compile | 4–6 d |
| Phase B synthetic `rnn` cells, adapter, audits/mutants | 3–4 d |
| Phase C/D validator, config, oracle, tests | 3 d |
| `realEval`, training code, phase-1 fixtures | 3 d |
| Phase-2 datasets: loaders, training, packaging, licensing review | 3–5 d |
| S-R2 calibration, docs (Phase E) | 2 d |
| Phase F `exact-inference` | separate, after the contract freezes |

## Risks and open questions

- **Activation-dominated board.** Until someone proves a fast `exp`, the board measures the spec's
  `exp`; after that, ratios jump once. Acceptable? An alternative is to ship the incumbent with a
  proved faster `sigmoid`/`tanh` (our proof work) so the board starts MAC-dominated.
- **R2a proof risk**: some FloatLib proof helpers the `exp` proofs use are `private`, so we may
  re-prove them. The comparison-only path may be too slow as the benchmark reference until the
  enclosure fast path is proved. If R2b ships instead, document the ~2–3 ulp bound or prove it.
- **Saturation shortcuts.** `sigmoid`/`tanh` are trivial for large `|x|`; synthetic gain and the
  saturation check keep the workload realistic, the real fixtures anchor it.
- **Budget.** The overall 20-minute ceiling is shared by six families; recurrent fixtures must be
  counted in `test_benchmark_budgets.py` from the start.
- **New-competition mechanics** (D4 of the upgrade plan): board naming, identities, incumbent, and
  how the API and dashboard present a fresh competition.
- **Licensing**: confirm each phase-2 dataset's terms allow redistributing preprocessed test
  examples in the (private, later possibly public) asset repo.
