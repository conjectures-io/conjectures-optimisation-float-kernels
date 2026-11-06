# Recurrent benchmark models

Small LSTM/GRU models trained on public datasets and packaged as real-suite fixtures for the
`recurrent` family (`adapter = "recurrent-v1"`). The Lean benchmark runs these models bit-exactly;
PyTorch is only the trainer. See `docs/plans/recurrent-family.md` for the design.

## Model semantics

Input `x` is `T × F` float32, time-major. One or two recurrent layers, each a PyTorch `nn.LSTM`
or `nn.GRU` (batch_first, bias, one direction, no projection, zero initial state; the GRU is
PyTorch's: `n = tanh(W_in x + b_in + r * (W_hn h + b_hn))`). The last time step's hidden state
goes through `Linear(H_last → outputs)` with no activation. Classifiers use the argmax; the
Appliance model is a 6-output regression.

## Files

| File | Purpose |
|---|---|
| `data.py` | One loader per dataset → train/val/test `[N, T, F]` float32 + labels; pinned download URLs and SHA-256 digests; licences and attribution |
| `models.py` | `RecurrentModel(features, [(kind, H), ...], outputs)` |
| `train.py` | `--config NAME`; deterministic CPU training; writes `runs/<config>/` |
| `export.py` | Model → `recurrent-v1` `weights.bin` |
| `reference.py` | Numpy float64 forward pass from `weights.bin` + `samples.bin`; PyTorch agreement and gate saturation |
| `../../scripts/build-recurrent-fixtures.py` | Checks each run and writes the fixtures into the asset bundle |

The authoritative spec check (spec accuracy within 0.5 pp of PyTorch, argmax agreement ≥ 99.5%)
is the Lean `realEval` executable. `reference.py` is the float64 sanity check of the export.

## Reproduce

```sh
cd training/recurrent
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python --index-url https://download.pytorch.org/whl/cpu torch==2.15.0+cpu
uv pip install --python .venv/bin/python numpy==2.5.4 scipy==1.18.1 wfdb==4.3.1
for c in uci-har-lstm uci-har-gru appliance-gru mnist-rows-lstm \
         spoken-arabic-digit-lstm tiny-shakespeare-lstm mitbih-gru; do
  .venv/bin/python -I train.py --config "$c"
done
cd ../..
training/recurrent/.venv/bin/python -I scripts/build-recurrent-fixtures.py --overwrite
python3 scripts/index-benchmark-assets.py
```

Loaders download missing raw files over HTTPS into `data/raw/<dataset>/` (gitignored) and refuse
files whose SHA-256 differs from the pinned digest. Archives are read in place with `zipfile`
(only text, CSV, IDX and WFDB files are read; MIT-BIH's 48 records are extracted to
`data/raw/mitdb/extracted/` because `wfdb` reads from disk). Nothing from a download is
executed.

Each run writes `runs/<config>/{checkpoint.pt, weights.bin, metrics.json, test_outputs.npy,
test_labels.npy}` (gitignored). The test labels stay there, out of the asset bundle, for the
spec-accuracy evaluation. The builder writes
`models/recurrent/<dataset>/<fixture>/{weights.bin, manifest.json}`, the content-addressed pool
`samples/recurrent-v1/<dataset>/<sha256[:12]>/samples.bin`, and `ATTRIBUTION.md` under the asset
root (`data/benchmark/assets/`). It rebuilds every test pool from `data.py` and refuses to
write a fixture if:

- the pool's hash differs from the pool the model was evaluated on;
- the Appliance or MNIST pool is not byte-identical to the existing `appliance-mixer-v1` /
  `image-zoo-v1` pool built by `scripts/split-real-fixtures.py`;
- accuracy is less than 10 points above the majority class (regression: RMSE not below the
  last-value forecast);
- reference argmax agreement < 99.5% or the max output difference > 1e-3;
- gate saturation (share of gate pre-activations with `|a| > 8` on the test pool) ≥ 50%.

## Training

Seed 0, `torch.use_deterministic_algorithms(True)`, CPU. Adam (lr 2e-3, weight decay 1e-4),
cosine schedule to 1e-5 over the configured maximum epochs, gradient-norm clip 1.0, batch 64,
early stopping on validation accuracy (regression: validation MSE) with patience 15, best
checkpoint restored. Inputs are standardised with statistics of the training (fit) split only,
except MNIST (pixels / 255, so the pool equals the existing MNIST pool), Tiny Shakespeare
(one-hot) and MIT-BIH ([0, 1] per 10 s window, as in Kachuee et al.). Validation is carved from
the official training data; the test split is never used for training or early stopping.
4 threads by default; MIT-BIH uses 1 thread (faster for tiny GRU steps on a busy machine).
Re-running a config with the same thread count reproduces `weights.bin` bit for bit (checked for
`uci-har-gru`).

Tiny Shakespeare is trained with a per-step loss (the head on every step's hidden state, on
overlapping stride-32 windows); the exported model and its evaluation read only the last step.

## Results (seed 0, PyTorch fp32 on the full test pool)

| Workload | Data `T×F` | Model | Test pool | Test metric | Trivial baseline | Ref. max \|Δ\| | Argmax agree | Gate sat. | Epochs (best) |
|---|---|---|---|---|---|---|---|---|---|
| `recurrent-uci-har-lstm` | 64×9 | LSTM 32 | 2,947 | 89.51% | 18.22% majority | 2.3e-5 | 100% | 3.4e-6 | 36 (21) |
| `recurrent-uci-har-gru` | 64×9 | GRU 32 | 2,947 | 89.82% | 18.22% | 5.1e-6 | 100% | 1.5e-5 | 33 (18) |
| `recurrent-appliance-gru` | 24×8 | GRU 32 | 2,932 | RMSE 0.732 | 0.863 last value, 0.849 zero | 3.6e-7 | — | 0 | 18 (3) |
| `recurrent-mnist-rows-lstm` | 28×28 | LSTM 32 | 10,000 | 98.60% | 11.35% | 5.8e-6 | 100% | 2.8e-8 | 49 (34) |
| `recurrent-spoken-arabic-digit-lstm` | 48×13 | LSTM 32 | 2,200 | 95.86% | 10.00% | 1.1e-4 | 100% | 0 | 55 (40) |
| `recurrent-tiny-shakespeare-lstm` | 64×65 | LSTM 32 | 1,742 | 39.90% | 14.98% | 5.1e-6 | 100% | 0 | 40 (33) |
| `recurrent-mitbih-gru` | 94×1 | GRU 32 | 21,829 | 94.47% | 82.78% | 9.0e-5 | 100% | 4.9e-4 | 15 (13) |

Appliance RMSE is in standardised `Appliances` units over all six horizons. Not packaged:
`uci-har-lstm-t128` (LSTM 32 at the full 128 steps) reached 86.73% test accuracy, below the
decimated T=64 model, so T=64 is kept. MIT-BIH is capped at 15 epochs (~13 min on one busy CPU
thread) and was still improving; published 5-class results at this split are ~98%, so more
epochs would help if the time is available.

Gate saturation is far below the 50% limit for every model: these small, weight-decayed models
keep their gates in the non-saturated range, so activation fast paths cannot shortcut them.

## Datasets

| Dataset | Licence | Source | Split |
|---|---|---|---|
| UCI-HAR | CC BY 4.0 | archive.ics.uci.edu dataset 240 | official test (subjects disjoint); val = 4 training subjects |
| Appliances Energy Prediction | CC BY 4.0 | archive.ics.uci.edu dataset 374 | chronological 70/15/15 |
| MNIST | CC BY-SA 3.0 (per Keras docs) | yann.lecun.com (404 now); Google `cvdf-datasets` mirror, MD5s match the published ones | official test; val = fixed 5,000 train images |
| Spoken Arabic Digit | CC BY 4.0 | archive.ics.uci.edu dataset 195 | official test (22 other speakers); val = 6 training speakers |
| Tiny Shakespeare | text public domain; char-rnn repo MIT | github.com/karpathy/char-rnn | last 10% test, 85–90% val |
| MIT-BIH Arrhythmia (PhysioNet `mitdb` 1.0.0) | ODC-By 1.0 | physionet.org | stratified random 80/20 beats (patients overlap), val 10% of train |
| Speech Commands v2 | CC BY 4.0 | download.tensorflow.org | loader only, not downloaded |

Preprocessing details (also in each manifest):

- **UCI-HAR**: 9 inertial channels, 2× decimation by averaging adjacent steps, z-score.
- **Appliance**: replicates `appliance_evaluation` in `scripts/split-real-fixtures.py`; target =
  next 6 standardised `Appliances` values.
- **MNIST rows**: pixels / 255, row t is step t; pool = existing MNIST pool byte for byte.
- **Spoken Arabic Digit**: 13 MFCCs, each utterance (4–93 frames) linearly resampled to 48
  frames, z-score. Validation assumes each speaker's 10 repetitions are consecutive blocks.
- **Tiny Shakespeare**: 65-symbol vocabulary of the whole file (sorted; in the manifest),
  one-hot inputs, non-overlapping 64-character test windows, label = next character.
- **MIT-BIH**: lead 0, 360→125 Hz polyphase resampling, 10 s windows min-max normalised, each
  annotated beat cut from its R-peak for 1.2× the window's median RR interval, zero-padded to
  187, every other sample kept (94); AAMI N/S/V/F/Q. Beat-level split, so accuracy is optimistic.
- **Speech Commands v2** (not run): pinned numpy MFCC (16 kHz, 40 ms Hann window, 20 ms hop,
  1024-point FFT, 40 HTK mel bands 20–7600 Hz, log, orthonormal DCT-II, 40 coefficients → 49×40),
  12 classes, official list-based train/val split, the separate 4,890-clip test archive.
  Configs `speech-commands-gru` and `speech-commands-lstm-gru` (2-layer stack) are ready; extract
  both archives under `data/raw/speech-commands/extracted/{train,test}` first (2.4 GB).
