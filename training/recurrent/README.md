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
| `spec_eval.py` | Runs the bit-exact Lean spec (`lean/.lake/build/bin/realEval`) on each fixture's pool; spec accuracy/RMSE and agreement with PyTorch |
| `../../scripts/build-recurrent-fixtures.py` | Checks each run and writes the fixtures into the asset bundle |

The authoritative spec check (spec accuracy within 0.5 pp of PyTorch, argmax agreement ≥ 99.5%)
is the Lean `realEval` executable. `reference.py` is the float64 sanity check of the export.

## Reproduce

```sh
cd training/recurrent
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python --index-url https://download.pytorch.org/whl/cpu torch==2.15.0+cpu
uv pip install --python .venv/bin/python numpy==2.5.4 scipy==1.18.1 wfdb==4.3.1
for c in uci-har-lstm uci-har-gru appliance-gru mnist-rows-lstm spoken-arabic-digit-lstm \
         tiny-shakespeare-lstm mitbih-gru speech-commands-gru speech-commands-lstm-gru; do
  .venv/bin/python -I train.py --config "$c"
done
cd ../..
training/recurrent/.venv/bin/python -I scripts/build-recurrent-fixtures.py --overwrite
# bit-exact spec scores (needs `lake build realEval` in lean/), then record them in the manifests
training/recurrent/.venv/bin/python -I training/recurrent/spec_eval.py --jobs 6 > spec.jsonl
training/recurrent/.venv/bin/python -I scripts/build-recurrent-fixtures.py --attach-spec spec.jsonl
python3 scripts/index-benchmark-assets.py
```

`--attach-spec` only rewrites the manifests' `metrics` (spec accuracy or RMSE, rows evaluated,
argmax agreement and max difference against PyTorch); weight and sample checksums are unchanged.
Rebuilding a fixture drops its spec results, so re-run `spec_eval.py` after retraining.

Loaders download missing raw files over HTTPS into `data/raw/<dataset>/` (gitignored) and refuse
files whose SHA-256 differs from the pinned digest. Archives are read in place with `zipfile` /
`tarfile` streaming (only text, CSV, IDX, WAV and WFDB files are read; MIT-BIH's 48 records are
extracted to `data/raw/mitdb/extracted/` because `wfdb` reads from disk). Nothing from a download
is executed. Speech Commands MFCCs are computed once (~3 min) and cached in
`data/recurrent/speech-commands-mfcc-v1-<key>.npz` (gitignored; the key hashes the MFCC
parameters, split seed and archive digests).

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
4 torch threads. Re-running a config with the same thread count reproduces `weights.bin` bit for bit (checked for
`uci-har-gru`).

Tiny Shakespeare is trained with a per-step loss (the head on every step's hidden state, on
overlapping stride-32 windows); the exported model and its evaluation read only the last step.

## Results (seed 0, full test pools)

"PyTorch fp32" is the trained model in PyTorch; "Spec (Lean)" is the bit-exact Lean spec
(`spec_eval.py` → `realEval`) on the exported fixture, over every row of the pool. Every fixture
passes the spec gate (accuracy within 0.5 pp of PyTorch, RMSE within 1%, argmax agreement
≥ 99.5%): the spec and PyTorch pick the same class on every row, and their outputs differ by at
most 6e-5. Gate saturation is the share of gate pre-activations with `|a| > 8` (float64
reference, test pool).

| Workload | `T×F` | Model | Test pool | PyTorch fp32 | Spec (Lean) | Spec rows | Spec↔torch argmax | Spec↔torch max \|Δ\| | Trivial baseline | Gate sat. | Epochs (best) |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `recurrent-uci-har-lstm` | 64×9 | LSTM 32 | 2,947 | 89.51% | 89.51% | 2,947 | 100.00% | 1.7e-05 | 18.22% majority | 3.4e-06 | 36 (21) |
| `recurrent-uci-har-gru` | 64×9 | GRU 32 | 2,947 | 89.82% | 89.82% | 2,947 | 100.00% | 3.3e-06 | 18.22% majority | 1.5e-05 | 33 (18) |
| `recurrent-appliance-gru` | 24×8 | GRU 32 | 2,932 | RMSE 0.7317 | RMSE 0.7317 | 2,932 | — | 4.8e-07 | 0.863 last value | 0.0e+00 | 18 (3) |
| `recurrent-mnist-rows-lstm` | 28×28 | LSTM 32 | 10,000 | 98.60% | 98.60% | 10,000 | 100.00% | 5.7e-06 | 11.35% majority | 2.8e-08 | 49 (34) |
| `recurrent-spoken-arabic-digit-lstm` | 48×13 | LSTM 32 | 2,200 | 95.86% | 95.86% | 2,200 | 100.00% | 4.4e-05 | 10.00% majority | 0.0e+00 | 55 (40) |
| `recurrent-tiny-shakespeare-lstm` | 64×65 | LSTM 32 | 1,742 | 39.90% | 39.90% | 1,742 | 100.00% | 5.5e-06 | 14.98% majority | 0.0e+00 | 40 (33) |
| `recurrent-mitbih-gru` | 94×1 | GRU 32 | 21,829 | 96.47% | 96.47% | 21,829 | 100.00% | 5.9e-05 | 82.78% majority | 2.6e-04 | 60 (52) |
| `recurrent-speech-commands-gru` | 49×40 | GRU 32 | 4,890 | 88.92% | 88.92% | 4,890 | 100.00% | 1.2e-05 | 8.57% majority | 5.4e-03 | 35 (20) |
| `recurrent-speech-commands-lstm-gru` | 49×40 | LSTM 32 → GRU 32 | 4,890 | 89.16% | 89.16% | 4,890 | 100.00% | 1.2e-05 | 8.57% majority | 9.7e-04 | 40 (25) |

Appliance RMSE is in standardised `Appliances` units over all six horizons (zero forecast 0.849).
Not packaged: `uci-har-lstm-t128` (LSTM 32 at the full 128 steps) reached 86.73% test accuracy,
below the decimated T=64 model, so T=64 is kept. MIT-BIH ran all 60 epochs (best epoch 52, still
creeping up; published 5-class results at this split are ~98% with larger models). The previous
15-epoch MIT-BIH model reached 94.47%.

Training wall time (4 threads, shared machine): UCI-HAR 16–47 s, Appliance 19 s, MNIST rows
99 s, Spoken Arabic Digit 15 s, Tiny Shakespeare 107 s, MIT-BIH 23.5 min, Speech Commands GRU
12 min and LSTM→GRU 6 min (plus ~3 min once for the MFCC cache). Spec evaluation wall time with
`--jobs 6` (machine shared with other jobs): Appliance 1.4 min, UCI-HAR 4–6 min, Arabic Digit
3.3 min, Tiny Shakespeare 11.6 min, Speech Commands GRU 10.4 min and LSTM→GRU 24.5 min, MNIST
24 min, MIT-BIH 63 min.

Gate saturation stays far below the 50% limit for every model: these small, weight-decayed
models keep their gates in the non-saturated range, so activation fast paths for large `|a|`
rarely apply.

## Datasets

| Dataset | Licence | Source | Split |
|---|---|---|---|
| UCI-HAR | CC BY 4.0 | archive.ics.uci.edu dataset 240 | official test (subjects disjoint); val = 4 training subjects |
| Appliances Energy Prediction | CC BY 4.0 | archive.ics.uci.edu dataset 374 | chronological 70/15/15 |
| MNIST | CC BY-SA 3.0 (per Keras docs) | yann.lecun.com (404 now); Google `cvdf-datasets` mirror, MD5s match the published ones | official test; val = fixed 5,000 train images |
| Spoken Arabic Digit | CC BY 4.0 | archive.ics.uci.edu dataset 195 | official test (22 other speakers); val = 6 training speakers |
| Tiny Shakespeare | text public domain; char-rnn repo MIT | github.com/karpathy/char-rnn | last 10% test, 85–90% val |
| MIT-BIH Arrhythmia (PhysioNet `mitdb` 1.0.0) | ODC-By 1.0 | physionet.org | stratified random 80/20 beats (patients overlap), val 10% of train |
| Speech Commands v2 | CC BY 4.0 | download.tensorflow.org (`speech_commands_v0.02.tar.gz`, 2.3 GB, and `speech_commands_test_set_v0.02.tar.gz`) | official lists; separate 4,890-clip test archive |

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
- **Speech Commands v2**: clips padded/trimmed to 1 s; pinned numpy MFCC (16 kHz, 40 ms periodic
  Hann window, 20 ms hop, 1024-point FFT, 40 HTK mel bands 20–7600 Hz, log with 1e-6 floor,
  orthonormal DCT-II, 40 coefficients → 49×40), z-score. 12 classes: the 10 command words,
  `_unknown_` (a fixed random draw from the other 25 words) and `_silence_` (1 s background-noise
  slices with random gain), each extra class ~10% of the command clips in train and validation,
  as in the TensorFlow keyword-spotting recipe. Train/validation follow the official
  `validation_list.txt`/`testing_list.txt`; the test pool is the separate official test-set
  archive (4,890 clips, 12 class folders, as in TFDS). `speech-commands-lstm-gru` is the only
  stacked fixture (LSTM 32 → GRU 32).

## Publishing to Hugging Face

The asset bundle (`data/benchmark/assets/`) is the root of the private dataset repository that
`scripts/download-benchmark-assets.py` reads (`sagitarim/torchlean-benchmark-assets`; token in
`HF_TOKEN_TORCHLEAN_BENCHMARK`). Nothing here uploads automatically. To publish the recurrent
fixtures:

1. Start from the current published bundle so the catalog keeps the other fixtures:
   `python3 scripts/download-benchmark-assets.py --revision <current commit>`.
2. Train (above), then build the fixtures:
   `training/recurrent/.venv/bin/python -I scripts/build-recurrent-fixtures.py --overwrite`.
3. Score them with the spec and record the results:
   `training/recurrent/.venv/bin/python -I training/recurrent/spec_eval.py --jobs 6 > spec.jsonl`,
   then `... scripts/build-recurrent-fixtures.py --attach-spec spec.jsonl`. Every fixture must
   report `"accepted": true`.
4. Re-index and checksum-verify the whole bundle: `python3 scripts/index-benchmark-assets.py`
   (rewrites `manifest.json` at the bundle root).
5. Upload only the new and changed files, as one commit:
   `hf upload sagitarim/torchlean-benchmark-assets data/benchmark/assets . --repo-type dataset
   --include "models/recurrent/**" "samples/recurrent-v1/**" ATTRIBUTION.md manifest.json
   --commit-message "Add recurrent-v1 fixtures"` (with `HF_TOKEN` set to the token above).
6. Note the new commit hash on the Hub and pin it: download it on every benchmark worker with
   `scripts/download-benchmark-assets.py --revision <new commit>` (or
   `HF_TORCHLEAN_BENCHMARK_REVISION`), then regenerate the config with
   `scripts/exact-config.py --real-recurrent ...`, which records every fixture's weight and sample
   SHA-256. Never pin `main`.
