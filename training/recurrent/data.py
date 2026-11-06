"""Dataset loaders for the recurrent benchmark models.

Every loader returns a :class:`Dataset` with time-major float32 inputs ``[N, T, F]`` for the
train, validation and test splits, plus labels (class ids, or regression targets). Normalisation
statistics come from the training (fit) split only, validation is carved from the official
training data, and the official test split is never used for training or early stopping.

Raw downloads live in ``data/raw/<dataset>/`` (gitignored) and are treated as untrusted data:
they are only fetched over HTTPS, checked against pinned SHA-256 digests and read as text, CSV,
IDX or WFDB signal files. Nothing in them is executed.
"""

from __future__ import annotations

import csv
import gzip
import hashlib
import io
import json
import struct
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[2]
RAW_ROOT = REPO_ROOT / "data/raw"
SPLIT_SEED = 20261106  # data splits are fixed independently of the training seed


@dataclass(frozen=True)
class RawFile:
    url: str
    name: str
    sha256: str


@dataclass(frozen=True)
class Source:
    """A dataset's downloadable files plus the licence and attribution to ship with it."""

    directory: str
    files: tuple[RawFile, ...]
    license: str
    attribution: str
    source_url: str
    note: str = ""


SOURCES: dict[str, Source] = {
    "uci-har": Source(
        directory="uci-har",
        files=(
            RawFile(
                "https://archive.ics.uci.edu/static/public/240/"
                "human+activity+recognition+using+smartphones.zip",
                "human+activity+recognition+using+smartphones.zip",
                "c00b803081a5c797cd5e4b83700a9810b38d53d9d84e01917e090e1fdbc81031",
            ),
        ),
        license="CC BY 4.0",
        attribution=(
            "Reyes-Ortiz, J., Anguita, D., Ghio, A., Oneto, L. & Parra, X. (2013). Human Activity "
            "Recognition Using Smartphones [Dataset]. UCI Machine Learning Repository. "
            "https://doi.org/10.24432/C54S4K"
        ),
        source_url="https://archive.ics.uci.edu/dataset/240",
    ),
    "appliance-energy": Source(
        directory="appliances-energy",
        files=(
            RawFile(
                "https://archive.ics.uci.edu/static/public/374/appliances+energy+prediction.zip",
                "appliances+energy+prediction.zip",
                "2fccf354445d886e7917620b0195db1f3e3e34d5a067a93b844694a4c561255a",
            ),
        ),
        license="CC BY 4.0",
        attribution=(
            "Candanedo, L. (2017). Appliances Energy Prediction [Dataset]. UCI Machine Learning "
            "Repository. https://doi.org/10.24432/C5VC8G"
        ),
        source_url="https://archive.ics.uci.edu/dataset/374",
    ),
    "mnist-rows": Source(
        directory="mnist",
        files=tuple(
            RawFile(
                f"https://storage.googleapis.com/cvdf-datasets/mnist/{name}.gz",
                f"{name}.gz",
                digest,
            )
            for name, digest in (
                (
                    "train-images-idx3-ubyte",
                    "440fcabf73cc546fa21475e81ea370265605f56be210a4024d2ca8f203523609",
                ),
                (
                    "train-labels-idx1-ubyte",
                    "3552534a0a558bbed6aed32b30c495cca23d567ec52cac8be1a0730e8010255c",
                ),
                (
                    "t10k-images-idx3-ubyte",
                    "8d422c7b0a1c1c79245a5bcf07fe86e33eeafee792b84584aec276f5a2dbc4e6",
                ),
                (
                    "t10k-labels-idx1-ubyte",
                    "f7ae60f92e00ec6debd23a6088c31dbd2371eca3ffa0defaefb259924204aec6",
                ),
            )
        ),
        license="CC BY-SA 3.0 (as stated by the Keras/TensorFlow dataset documentation)",
        attribution=(
            "LeCun, Y., Cortes, C. & Burges, C. J. C. The MNIST database of handwritten digits. "
            "http://yann.lecun.com/exdb/mnist/"
        ),
        source_url="http://yann.lecun.com/exdb/mnist/",
        note=(
            "yann.lecun.com no longer serves the files (HTTP 404); the IDX files are the canonical "
            "copies mirrored by Google (cvdf-datasets) and match the published MD5 digests "
            "9fb629c4…, ec29112d…, f68b3c2d…, d53e105e…"
        ),
    ),
    "spoken-arabic-digit": Source(
        directory="spoken-arabic-digit",
        files=(
            RawFile(
                "https://archive.ics.uci.edu/static/public/195/spoken+arabic+digit.zip",
                "spoken+arabic+digit.zip",
                "3c8c83404289c49631605421830872be337d92d887e61798385aacbe9423010d",
            ),
        ),
        license="CC BY 4.0",
        attribution=(
            "Bedda, M. & Hammami, N. (2008). Spoken Arabic Digit [Dataset]. UCI Machine Learning "
            "Repository. https://doi.org/10.24432/C52C9Q"
        ),
        source_url="https://archive.ics.uci.edu/dataset/195",
    ),
    "tiny-shakespeare": Source(
        directory="tiny-shakespeare",
        files=(
            RawFile(
                "https://raw.githubusercontent.com/karpathy/char-rnn/master/data/"
                "tinyshakespeare/input.txt",
                "input.txt",
                "86c4e6aa9db7c042ec79f339dcb96d42b0075e16b8fc2e86bf0ca57e2dc565ed",
            ),
        ),
        license=(
            "Text: works of William Shakespeare, public domain. Compilation: char-rnn repository "
            "(MIT License, Copyright (c) 2015 Andrej Karpathy)"
        ),
        attribution=(
            "Karpathy, A. (2015). char-rnn, data/tinyshakespeare/input.txt. "
            "https://github.com/karpathy/char-rnn"
        ),
        source_url="https://github.com/karpathy/char-rnn/tree/master/data/tinyshakespeare",
    ),
    "mitbih": Source(
        directory="mitdb",
        files=(
            RawFile(
                "https://physionet.org/static/published-projects/mitdb/"
                "mit-bih-arrhythmia-database-1.0.0.zip",
                "mit-bih-arrhythmia-database-1.0.0.zip",
                "47b26926927c11bd9174154d367c811afc1b186f650f8a205931ca8c520f0a87",
            ),
        ),
        license="Open Data Commons Attribution License v1.0 (ODC-By 1.0)",
        attribution=(
            "Moody, G. B. & Mark, R. G. (2001). The impact of the MIT-BIH Arrhythmia Database. "
            "IEEE Eng in Med and Biol 20(3):45-50. Goldberger, A. et al. (2000). PhysioBank, "
            "PhysioToolkit, and PhysioNet. Circulation 101(23):e215-e220. Beat segmentation after "
            "Kachuee, M., Fazeli, S. & Sarrafzadeh, M. (2018), arXiv:1805.00794."
        ),
        source_url="https://physionet.org/content/mitdb/1.0.0/",
    ),
    "speech-commands": Source(
        directory="speech-commands",
        files=(
            RawFile(
                "https://storage.googleapis.com/download.tensorflow.org/data/"
                "speech_commands_v0.02.tar.gz",
                "speech_commands_v0.02.tar.gz",
                "af14739ee7dc311471de98f5f9d2c9191b18aedfe957f4a6ff791c709868ff58",
            ),
            RawFile(
                "https://storage.googleapis.com/download.tensorflow.org/data/"
                "speech_commands_test_set_v0.02.tar.gz",
                "speech_commands_test_set_v0.02.tar.gz",
                "cc2a00c1147c2254e9be3fa0f779d8c17421dc349b86366567a8edfa9acd51df",
            ),
        ),
        license="CC BY 4.0",
        attribution=(
            "Warden, P. (2018). Speech Commands: A Dataset for Limited-Vocabulary Speech "
            "Recognition. arXiv:1804.03209"
        ),
        source_url="https://www.tensorflow.org/datasets/catalog/speech_commands",
    ),
}


@dataclass
class Split:
    x: np.ndarray  # float32 [N, T, F]
    y: np.ndarray  # int64 [N] (classification) or float32 [N, outputs] (regression)
    seq_y: np.ndarray | None = None  # optional per-step targets [N, T] for sequence training


@dataclass
class Dataset:
    name: str
    task: str  # "classification" or "regression"
    outputs: int
    train: Split
    val: Split
    test: Split
    evaluation_split: str
    info: dict = field(default_factory=dict)

    @property
    def steps(self) -> int:
        return int(self.test.x.shape[1])

    @property
    def features(self) -> int:
        return int(self.test.x.shape[2])


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fetch(dataset: str, raw_root: Path = RAW_ROOT, download: bool = True) -> dict[str, Path]:
    """Return verified local paths of a dataset's raw files, downloading missing ones."""
    source = SOURCES[dataset]
    directory = raw_root / source.directory
    paths = {}
    for item in source.files:
        path = directory / item.name
        if not path.is_file():
            if not download:
                raise FileNotFoundError(f"missing raw file {path}; run with downloads enabled")
            if not item.url.startswith("https://"):
                raise ValueError(f"refusing non-HTTPS download {item.url}")
            directory.mkdir(parents=True, exist_ok=True)
            partial = path.with_suffix(path.suffix + ".part")
            with (
                urllib.request.urlopen(item.url, timeout=120) as response,
                partial.open("wb") as out,
            ):
                for chunk in iter(lambda: response.read(1 << 20), b""):
                    out.write(chunk)
            partial.rename(path)
        actual = sha256_file(path)
        if item.sha256 and actual != item.sha256:
            raise ValueError(f"{path}: SHA-256 {actual} does not match pinned {item.sha256}")
        paths[item.name] = path
    return paths


def checksums(dataset: str) -> dict[str, str]:
    return {item.name: item.sha256 for item in SOURCES[dataset].files}


def source_info(dataset: str) -> dict:
    source = SOURCES[dataset]
    info = {
        "license": source.license,
        "attribution": source.attribution,
        "source_url": source.source_url,
        "download_urls": [item.url for item in source.files],
        "dataset_sha256": checksums(dataset),
    }
    if source.note:
        info["source_note"] = source.note
    return info


def standardise(train: np.ndarray, *others: np.ndarray):
    """Per-feature z-score using statistics of ``train`` only (float64 maths, float32 result)."""
    flat = train.reshape(-1, train.shape[-1]).astype(np.float64)
    mean = flat.mean(axis=0)
    std = flat.std(axis=0)
    std[std == 0] = 1.0
    out = [((a.astype(np.float64) - mean) / std).astype(np.float32) for a in (train, *others)]
    return out, {"mean": mean.tolist(), "std": std.tolist()}


# --------------------------------------------------------------------------------------------
# UCI-HAR
# --------------------------------------------------------------------------------------------

UCI_SIGNALS = (
    "body_acc_x",
    "body_acc_y",
    "body_acc_z",
    "body_gyro_x",
    "body_gyro_y",
    "body_gyro_z",
    "total_acc_x",
    "total_acc_y",
    "total_acc_z",
)
UCI_ACTIVITIES = (
    "WALKING",
    "WALKING_UPSTAIRS",
    "WALKING_DOWNSTAIRS",
    "SITTING",
    "STANDING",
    "LAYING",
)


def _uci_inner_zip(raw: dict[str, Path]) -> zipfile.ZipFile:
    with zipfile.ZipFile(raw["human+activity+recognition+using+smartphones.zip"]) as outer:
        inner = outer.read("UCI HAR Dataset.zip")
    return zipfile.ZipFile(io.BytesIO(inner))


def _uci_split(archive: zipfile.ZipFile, part: str):
    signals = []
    for name in UCI_SIGNALS:
        text = archive.read(f"UCI HAR Dataset/{part}/Inertial Signals/{name}_{part}.txt")
        signals.append(np.loadtxt(io.BytesIO(text), dtype=np.float64))
    x = np.stack(signals, axis=-1)  # [N, 128, 9]
    y = np.loadtxt(io.BytesIO(archive.read(f"UCI HAR Dataset/{part}/y_{part}.txt")), dtype=np.int64)
    subjects = np.loadtxt(
        io.BytesIO(archive.read(f"UCI HAR Dataset/{part}/subject_{part}.txt")), dtype=np.int64
    )
    return x, y - 1, subjects


def load_uci_har(steps: int = 64, raw_root: Path = RAW_ROOT) -> Dataset:
    """UCI-HAR inertial signals, 9 channels; T=128 as is or T=64 by averaging adjacent pairs.

    Validation holds out 4 of the 21 training subjects, so it stays subject-disjoint like the
    official test split.
    """
    if steps not in (64, 128):
        raise ValueError("UCI-HAR supports T=128 (raw) or T=64 (2x decimated)")
    raw = fetch("uci-har", raw_root)
    archive = _uci_inner_zip(raw)
    x_train, y_train, subjects = _uci_split(archive, "train")
    x_test, y_test, _ = _uci_split(archive, "test")
    if steps == 64:
        x_train = 0.5 * (x_train[:, 0::2] + x_train[:, 1::2])
        x_test = 0.5 * (x_test[:, 0::2] + x_test[:, 1::2])
    unique_subjects = np.unique(subjects)
    rng = np.random.default_rng(SPLIT_SEED)
    val_subjects = np.sort(rng.choice(unique_subjects, size=4, replace=False))
    val_mask = np.isin(subjects, val_subjects)
    (fit, val, test), stats = standardise(x_train[~val_mask], x_train[val_mask], x_test)
    info = source_info("uci-har")
    info.update(
        {
            "dataset_name": "UCI-HAR (Human Activity Recognition Using Smartphones)",
            "preprocessing": (
                "9 inertial signal channels (body_acc, body_gyro, total_acc x/y/z) of the official "
                "split; "
                + (
                    "2x decimation by averaging adjacent time steps (128 -> 64); "
                    if steps == 64
                    else ""
                )
                + "per-channel z-score with statistics of the training subjects minus validation"
            ),
            "validation": f"training subjects {val_subjects.tolist()} held out",
            "classes": list(UCI_ACTIVITIES),
            "normalisation": stats,
        }
    )
    return Dataset(
        name="uci-har",
        task="classification",
        outputs=6,
        train=Split(fit, y_train[~val_mask]),
        val=Split(val, y_train[val_mask]),
        test=Split(test, y_test),
        evaluation_split="UCI-HAR official subject-disjoint 2,947-window test split"
        + (", 2x decimated to 64 steps" if steps == 64 else ""),
        info=info,
    )


# --------------------------------------------------------------------------------------------
# Appliance Energy Prediction (mirrors scripts/split-real-fixtures.py::appliance_evaluation)
# --------------------------------------------------------------------------------------------

APPLIANCE_FEATURES = ("Appliances", "lights", "T1", "RH_1", "T2", "RH_2", "T6", "RH_6")
APPLIANCE_TOKENS = 24
APPLIANCE_HORIZON = 6


def appliance_csv(raw_root: Path = RAW_ROOT) -> Path:
    """Extract ``energydata_complete.csv`` next to the archive (plain CSV, read-only use)."""
    raw = fetch("appliance-energy", raw_root)
    archive_path = raw["appliances+energy+prediction.zip"]
    target = archive_path.parent / "extracted" / "energydata_complete.csv"
    if not target.is_file():
        target.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(archive_path) as archive:
            target.write_bytes(archive.read("energydata_complete.csv"))
    return target


def load_appliance(raw_root: Path = RAW_ROOT) -> Dataset:
    """Chronological 70/15/15 split; inputs 24x8, target = next 6 standardised Appliances values.

    The float32 arithmetic below is the same as ``appliance_evaluation`` in
    ``scripts/split-real-fixtures.py``, so the test inputs are byte-identical to the existing
    Appliance Energy sample pool (the fixture builder checks this).
    """
    csv_path = appliance_csv(raw_root)
    rows: list[list[float]] = []
    with csv_path.open(newline="", encoding="utf-8") as stream:
        for row in csv.DictReader(stream):
            rows.append([float(row[name]) for name in APPLIANCE_FEATURES])
    values = np.asarray(rows, dtype=np.float32)
    train_end = int(len(values) * 0.70)
    validation_end = int(len(values) * 0.85)
    means = values[:train_end].mean(axis=0, dtype=np.float64).astype(np.float32)
    scales = values[:train_end].std(axis=0, dtype=np.float64).astype(np.float32)
    scales[scales == 0] = 1.0
    normalized = (values - means) / scales
    span = APPLIANCE_TOKENS + APPLIANCE_HORIZON

    def windows(starts: np.ndarray) -> Split:
        x = np.stack([normalized[i : i + APPLIANCE_TOKENS] for i in starts]).astype(np.float32)
        y = np.stack([normalized[i + APPLIANCE_TOKENS : i + span, 0] for i in starts]).astype(
            np.float32
        )
        return Split(x, y)

    final_start_exclusive = max(validation_end, len(values) - span + 1)
    train = windows(np.arange(0, train_end - span + 1))
    val = windows(np.arange(train_end, validation_end - span + 1))
    test = windows(np.arange(validation_end, final_start_exclusive))
    info = source_info("appliance-energy")
    info.update(
        {
            "dataset_name": "Appliances Energy Prediction",
            "preprocessing": (
                "features " + ", ".join(APPLIANCE_FEATURES) + "; chronological 70/15/15 split; "
                "per-feature z-score with float32 training-period statistics; 24-step input "
                "windows; target = next 6 standardised Appliances values"
            ),
            "validation": "chronological 70-85% period (windows fully inside it)",
            "normalisation": {"mean": means.tolist(), "std": scales.tolist()},
        }
    )
    return Dataset(
        name="appliance-energy",
        task="regression",
        outputs=APPLIANCE_HORIZON,
        train=train,
        val=val,
        test=test,
        evaluation_split="Appliance Energy chronological test-period windows",
        info=info,
    )


# --------------------------------------------------------------------------------------------
# Sequential MNIST by rows
# --------------------------------------------------------------------------------------------


def _idx(path: Path, magic_expected: int) -> np.ndarray:
    raw = gzip.decompress(path.read_bytes())
    magic = struct.unpack_from(">I", raw)[0]
    if magic != magic_expected:
        raise ValueError(f"{path}: bad IDX magic {magic}")
    if magic == 2051:
        _, count, rows, cols = struct.unpack_from(">IIII", raw)
        if len(raw) != 16 + count * rows * cols:
            raise ValueError(f"{path}: truncated IDX image file")
        return np.frombuffer(raw, dtype=np.uint8, offset=16).reshape(count, rows, cols)
    _, count = struct.unpack_from(">II", raw)
    if len(raw) != 8 + count:
        raise ValueError(f"{path}: truncated IDX label file")
    return np.frombuffer(raw, dtype=np.uint8, offset=8).astype(np.int64)


def load_mnist_rows(raw_root: Path = RAW_ROOT) -> Dataset:
    """MNIST read row by row (T=28, F=28), pixels / 255 with no standardisation.

    Test rows equal ``images.reshape(N, 784).astype(float32) / 255`` byte for byte, i.e. the
    existing image-zoo MNIST sample pool. Validation = a fixed random 5,000 of the 60,000
    training images.
    """
    raw = fetch("mnist-rows", raw_root)
    train_images = _idx(raw["train-images-idx3-ubyte.gz"], 2051)
    train_labels = _idx(raw["train-labels-idx1-ubyte.gz"], 2049)
    test_images = _idx(raw["t10k-images-idx3-ubyte.gz"], 2051)
    test_labels = _idx(raw["t10k-labels-idx1-ubyte.gz"], 2049)

    def scale(images: np.ndarray) -> np.ndarray:
        return images.astype(np.float32)[:, None, :, :].reshape(len(images), 28, 28) / np.float32(
            255.0
        )

    order = np.random.default_rng(SPLIT_SEED).permutation(len(train_images))
    val_idx, fit_idx = np.sort(order[:5000]), np.sort(order[5000:])
    x_train = scale(train_images)
    info = source_info("mnist-rows")
    info.update(
        {
            "dataset_name": "MNIST (sequential, one 28-pixel row per step)",
            "preprocessing": "pixels / 255 as float32, no standardisation; row t is step t",
            "validation": "fixed random 5,000 of the 60,000 training images",
        }
    )
    return Dataset(
        name="mnist-rows",
        task="classification",
        outputs=10,
        train=Split(x_train[fit_idx], train_labels[fit_idx]),
        val=Split(x_train[val_idx], train_labels[val_idx]),
        test=Split(scale(test_images), test_labels),
        evaluation_split="MNIST official 10,000-image test split, read as 28 rows of 28 pixels",
        info=info,
    )


# --------------------------------------------------------------------------------------------
# Spoken Arabic Digit
# --------------------------------------------------------------------------------------------


def _arabic_blocks(text: str) -> list[np.ndarray]:
    blocks, current = [], []
    for line in text.splitlines():
        if line.strip():
            current.append([float(v) for v in line.split()])
        elif current:
            blocks.append(np.asarray(current, dtype=np.float64))
            current = []
    if current:
        blocks.append(np.asarray(current, dtype=np.float64))
    if any(block.shape[1] != 13 for block in blocks):
        raise ValueError("Spoken Arabic Digit: expected 13 MFCCs per frame")
    return blocks


def resample_linear(sequence: np.ndarray, steps: int) -> np.ndarray:
    """Linear interpolation of a [L, F] sequence to [steps, F] (endpoints preserved)."""
    length = len(sequence)
    if length == 1:
        return np.repeat(sequence, steps, axis=0)
    positions = np.linspace(0.0, length - 1, steps)
    source = np.arange(length)
    return np.stack(
        [np.interp(positions, source, sequence[:, j]) for j in range(sequence.shape[1])], axis=1
    )


def load_spoken_arabic_digit(steps: int = 48, raw_root: Path = RAW_ROOT) -> Dataset:
    """13 MFCCs per frame, each utterance resampled to ``steps`` frames, z-scored.

    The train file holds 660 blocks per digit (330 male then 330 female), the test file 220 per
    digit, with test speakers disjoint from training speakers. We assume each speaker's 10
    repetitions are consecutive and hold out 6 training speakers (3 male, 3 female) as
    validation.
    """
    raw = fetch("spoken-arabic-digit", raw_root)
    with zipfile.ZipFile(raw["spoken+arabic+digit.zip"]) as archive:
        train_blocks = _arabic_blocks(archive.read("Train_Arabic_Digit.txt").decode("ascii"))
        test_blocks = _arabic_blocks(archive.read("Test_Arabic_Digit.txt").decode("ascii"))
    if len(train_blocks) != 6600 or len(test_blocks) != 2200:
        raise ValueError(
            f"Spoken Arabic Digit: expected 6600/2200 blocks, got "
            f"{len(train_blocks)}/{len(test_blocks)}"
        )
    x_train = np.stack([resample_linear(b, steps) for b in train_blocks])
    x_test = np.stack([resample_linear(b, steps) for b in test_blocks])
    y_train = np.repeat(np.arange(10), 660)
    y_test = np.repeat(np.arange(10), 220)
    speaker = (np.arange(6600) % 660) // 10  # 0..65, 0..32 male, 33..65 female
    val_speakers = np.array([3, 15, 27, 36, 48, 60])
    val_mask = np.isin(speaker, val_speakers)
    (fit, val, test), stats = standardise(x_train[~val_mask], x_train[val_mask], x_test)
    info = source_info("spoken-arabic-digit")
    info.update(
        {
            "dataset_name": "Spoken Arabic Digit",
            "preprocessing": (
                f"13 MFCCs per frame; each utterance (4-93 frames) linearly resampled to {steps} "
                "frames (endpoints kept, so the last step is the utterance end); per-coefficient "
                "z-score with training statistics"
            ),
            "validation": (
                f"training speakers {val_speakers.tolist()} held out (speaker = 10 consecutive "
                "blocks within each digit)"
            ),
            "normalisation": stats,
        }
    )
    return Dataset(
        name="spoken-arabic-digit",
        task="classification",
        outputs=10,
        train=Split(fit, y_train[~val_mask]),
        val=Split(val, y_train[val_mask]),
        test=Split(test, y_test),
        evaluation_split="Spoken Arabic Digit official 2,200-utterance test file (22 speakers)",
        info=info,
    )


# --------------------------------------------------------------------------------------------
# Tiny Shakespeare (char-level next-character prediction)
# --------------------------------------------------------------------------------------------


def load_tiny_shakespeare(
    steps: int = 64, train_stride: int = 32, raw_root: Path = RAW_ROOT
) -> Dataset:
    """One-hot characters (vocabulary of the whole file, sorted), next-character labels.

    Text split by position: [0, 85%) train, [85%, 90%) validation, [90%, 100%) test. Test windows
    are non-overlapping ``steps``-character inputs (stride ``steps``) labelled with the following
    character. Training windows overlap (stride ``train_stride``) and carry per-step targets so
    the model can be trained on every position; evaluation reads only the last step.
    """
    raw = fetch("tiny-shakespeare", raw_root)
    text = raw["input.txt"].read_text(encoding="utf-8")
    vocab = sorted(set(text))
    index = {ch: i for i, ch in enumerate(vocab)}
    codes = np.fromiter((index[ch] for ch in text), dtype=np.int64, count=len(text))
    n = len(codes)
    val_start, test_start = int(n * 0.85), int(n * 0.90)
    eye = np.eye(len(vocab), dtype=np.float32)

    def windows(lo: int, hi: int, stride: int, with_seq: bool) -> Split:
        starts = np.arange(lo, hi - steps, stride)
        ids = np.stack([codes[s : s + steps] for s in starts])
        y = codes[starts + steps]
        seq = np.stack([codes[s + 1 : s + steps + 1] for s in starts]) if with_seq else None
        return Split(eye[ids], y, seq)

    info = source_info("tiny-shakespeare")
    info.update(
        {
            "dataset_name": "Tiny Shakespeare (char-rnn)",
            "preprocessing": (
                f"characters one-hot over the {len(vocab)}-symbol vocabulary of the whole file "
                "(sorted by code point; no standardisation); label = next character"
            ),
            "validation": "characters [85%, 90%) of the file",
            "vocabulary": "".join(vocab),
        }
    )
    return Dataset(
        name="tiny-shakespeare",
        task="classification",
        outputs=len(vocab),
        train=windows(0, val_start, train_stride, True),
        val=windows(val_start, test_start, steps, False),
        test=windows(test_start, n, steps, False),
        evaluation_split=(
            f"last 10% of the text, non-overlapping {steps}-character windows, label = next "
            "character"
        ),
        info=info,
    )


# --------------------------------------------------------------------------------------------
# MIT-BIH heartbeats (Kachuee et al. 2018 segmentation, rebuilt from PhysioNet)
# --------------------------------------------------------------------------------------------

MITBIH_RECORDS = (
    100, 101, 102, 103, 104, 105, 106, 107, 108, 109, 111, 112, 113, 114, 115, 116, 117, 118,
    119, 121, 122, 123, 124, 200, 201, 202, 203, 205, 207, 208, 209, 210, 212, 213, 214, 215,
    217, 219, 220, 221, 222, 223, 228, 230, 231, 232, 233, 234,
)  # fmt: skip
AAMI = {
    **dict.fromkeys("NLRej", 0),  # N: normal, bundle branch blocks, escapes
    **dict.fromkeys("aAJS", 1),  # S: supraventricular ectopic
    **dict.fromkeys("VE", 2),  # V: ventricular ectopic
    "F": 3,  # F: fusion
    **dict.fromkeys("/fQ", 4),  # Q: paced, fusion of paced, unclassifiable
}
MITBIH_CLASSES = ("N", "S", "V", "F", "Q")


def _mitdb_dir(raw_root: Path) -> Path:
    raw = fetch("mitbih", raw_root)
    archive_path = raw["mit-bih-arrhythmia-database-1.0.0.zip"]
    target = archive_path.parent / "extracted"
    marker = target / ".complete"
    if not marker.is_file():
        target.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(archive_path) as archive:
            for record in MITBIH_RECORDS:
                for suffix in ("hea", "dat", "atr"):
                    name = f"mit-bih-arrhythmia-database-1.0.0/{record}.{suffix}"
                    (target / f"{record}.{suffix}").write_bytes(archive.read(name))
        marker.write_text("ok\n")
    return target


def mitbih_beats(raw_root: Path = RAW_ROOT, length: int = 187):
    """Beats as in Kachuee et al.: 125 Hz, 10 s windows normalised to [0, 1], each annotated
    beat from its R-peak for 1.2 x the window's median RR interval, zero-padded to 187.

    Uses lead 0 of each record (MLII in 46 of 48 records) and the reference beat annotations.
    """
    import wfdb
    from scipy.signal import resample_poly

    directory = _mitdb_dir(raw_root)
    beats, labels, records = [], [], []
    for record in MITBIH_RECORDS:
        signal = wfdb.rdrecord(str(directory / str(record)), channels=[0]).p_signal[:, 0]
        annotation = wfdb.rdann(str(directory / str(record)), "atr")
        signal = resample_poly(signal, 25, 72)  # 360 Hz -> 125 Hz
        peaks = np.round(np.asarray(annotation.sample) * 125.0 / 360.0).astype(np.int64)
        symbols = annotation.symbol
        window = 1250
        for start in range(0, len(signal) - window + 1, window):
            segment = signal[start : start + window]
            low, high = segment.min(), segment.max()
            if high <= low:
                continue
            segment = (segment - low) / (high - low)
            inside = [
                (int(p - start), s)
                for p, s in zip(peaks, symbols, strict=True)
                if start <= p < start + window
            ]
            beat_peaks = [p for p, s in inside if s in AAMI]
            if len(beat_peaks) < 2:
                continue
            period = float(np.median(np.diff(beat_peaks)))
            span = min(length, int(round(1.2 * period)))
            for p, s in inside:
                if s not in AAMI:
                    continue
                beat = np.zeros(length, dtype=np.float64)
                piece = segment[p : p + span]
                beat[: len(piece)] = piece
                beats.append(beat)
                labels.append(AAMI[s])
                records.append(record)
    return np.stack(beats), np.asarray(labels, dtype=np.int64), np.asarray(records)


def load_mitbih(steps: int = 94, raw_root: Path = RAW_ROOT) -> Dataset:
    """Kachuee-style beats (187 samples in [0, 1]) decimated 2x (every other sample) to 94.

    Split like the widely used Kaggle copy: a stratified random 80/20 beat-level split (patients
    are shared between train and test, so accuracy is optimistic), with 10% of train held out
    for validation. Values stay in [0, 1] (no z-score), as in the reference preprocessing.
    """
    if steps != 94:
        raise ValueError("MIT-BIH fixture uses 94 steps (187 decimated 2x)")
    beats, labels, _ = mitbih_beats(raw_root)
    x = beats[:, ::2, None].astype(np.float32)  # 187 -> 94
    rng = np.random.default_rng(SPLIT_SEED)
    test_mask = np.zeros(len(x), dtype=bool)
    val_mask = np.zeros(len(x), dtype=bool)
    for cls in range(len(MITBIH_CLASSES)):
        idx = rng.permutation(np.flatnonzero(labels == cls))
        n_test = int(round(0.2 * len(idx)))
        n_val = int(round(0.1 * (len(idx) - n_test)))
        test_mask[idx[:n_test]] = True
        val_mask[idx[n_test : n_test + n_val]] = True
    fit_mask = ~(test_mask | val_mask)
    info = source_info("mitbih")
    info.update(
        {
            "dataset_name": "MIT-BIH Arrhythmia Database heartbeats (AAMI 5-class)",
            "preprocessing": (
                "lead 0, resampled 360->125 Hz (polyphase), 10 s windows min-max normalised to "
                "[0, 1], each annotated beat cut from its R-peak for 1.2x the window's median RR "
                "interval and zero-padded to 187 samples (Kachuee et al. 2018), then every other "
                "sample kept (94 steps); AAMI mapping N/S/V/F/Q"
            ),
            "validation": "stratified random 10% of the training beats",
            "classes": list(MITBIH_CLASSES),
            "caveat": "beat-level random split: patients overlap between train and test",
        }
    )
    return Dataset(
        name="mitbih",
        task="classification",
        outputs=5,
        train=Split(x[fit_mask], labels[fit_mask]),
        val=Split(x[val_mask], labels[val_mask]),
        test=Split(x[test_mask], labels[test_mask]),
        evaluation_split="MIT-BIH beats, stratified random 20% test split (Kachuee segmentation)",
        info=info,
    )


# --------------------------------------------------------------------------------------------
# Google Speech Commands v2, 12 classes
# --------------------------------------------------------------------------------------------

SPEECH_WORDS = ("yes", "no", "up", "down", "left", "right", "on", "off", "stop", "go")
SPEECH_CLASSES = ("_silence_", "_unknown_", *SPEECH_WORDS)
MFCC_PARAMS = {
    "sample_rate": 16000,
    "clip_samples": 16000,
    "window_samples": 640,  # 40 ms
    "hop_samples": 320,  # 20 ms -> 49 frames
    "n_fft": 1024,
    "window": "periodic Hann",
    "mel_bands": 40,
    "mel_scale": "HTK (2595 log10(1 + f / 700))",
    "f_min": 20.0,
    "f_max": 7600.0,
    "log_floor": 1e-6,
    "dct": "type II, orthonormal",
    "coefficients": 40,
}


_FILTERBANKS: dict[str, np.ndarray] = {}


def _mel_filterbank(params: dict = MFCC_PARAMS) -> np.ndarray:
    key = json.dumps(params, sort_keys=True)
    if key in _FILTERBANKS:
        return _FILTERBANKS[key]

    def hz_to_mel(f):
        return 2595.0 * np.log10(1.0 + np.asarray(f, dtype=np.float64) / 700.0)

    def mel_to_hz(m):
        return 700.0 * (10.0 ** (np.asarray(m, dtype=np.float64) / 2595.0) - 1.0)

    n_fft, bands = params["n_fft"], params["mel_bands"]
    bins = np.arange(n_fft // 2 + 1) * params["sample_rate"] / n_fft
    edges = mel_to_hz(
        np.linspace(hz_to_mel(params["f_min"]), hz_to_mel(params["f_max"]), bands + 2)
    )
    bank = np.zeros((bands, len(bins)))
    for b in range(bands):
        lo, mid, hi = edges[b], edges[b + 1], edges[b + 2]
        rising = (bins - lo) / (mid - lo)
        falling = (hi - bins) / (hi - mid)
        bank[b] = np.clip(np.minimum(rising, falling), 0.0, None)
    _FILTERBANKS[key] = bank
    return bank


def mfcc(audio: np.ndarray, params: dict = MFCC_PARAMS) -> np.ndarray:
    """Pinned numpy MFCC: pad/trim to 1 s, 49 frames x 40 coefficients (float64)."""
    from scipy.fft import dct

    clip = np.zeros(params["clip_samples"], dtype=np.float64)
    audio = np.asarray(audio, dtype=np.float64)[: params["clip_samples"]]
    clip[: len(audio)] = audio
    win, hop = params["window_samples"], params["hop_samples"]
    frames = 1 + (len(clip) - win) // hop
    window = 0.5 - 0.5 * np.cos(2.0 * np.pi * np.arange(win) / win)  # periodic Hann
    index = np.arange(win)[None, :] + hop * np.arange(frames)[:, None]
    spectrum = np.abs(np.fft.rfft(clip[index] * window, n=params["n_fft"])) ** 2
    mel = spectrum @ _mel_filterbank(params).T
    return dct(np.log(mel + params["log_floor"]), type=2, norm="ortho", axis=1)[
        :, : params["coefficients"]
    ]


def _decode_wav(raw: bytes, name: str) -> np.ndarray:
    from scipy.io import wavfile

    rate, data = wavfile.read(io.BytesIO(raw))
    if rate != 16000 or data.dtype != np.int16 or data.ndim != 1:
        raise ValueError(f"{name}: expected 16 kHz mono int16 PCM")
    return data.astype(np.float64) / 32768.0


def _tar_members(path: Path):
    """Stream (normalised name, member, tar) over a .tar.gz without extracting anything."""
    import tarfile

    with tarfile.open(path, "r|gz") as tar:
        for member in tar:
            if member.isfile():
                yield member.name.removeprefix("./"), member, tar


SPEECH_CACHE_VERSION = "speech-commands-mfcc-v1"


def _speech_features(raw: dict[str, Path], cache_dir: Path) -> dict[str, np.ndarray]:
    """MFCCs of the selected clips, cached (gitignored) under ``data/recurrent/``."""
    key = hashlib.sha256(
        json.dumps(
            [SPEECH_CACHE_VERSION, MFCC_PARAMS, SPLIT_SEED, checksums("speech-commands")],
            sort_keys=True,
        ).encode()
    ).hexdigest()[:12]
    cache = cache_dir / f"{SPEECH_CACHE_VERSION}-{key}.npz"
    if cache.is_file():
        with np.load(cache) as stored:
            return {name: stored[name] for name in stored.files}

    train_archive = raw["speech_commands_v0.02.tar.gz"]
    test_archive = raw["speech_commands_test_set_v0.02.tar.gz"]
    # Pass 1: names and the official split lists.
    names, lists = [], {}
    for name, member, tar in _tar_members(train_archive):
        if name.endswith(".wav"):
            names.append(name)
        elif name in ("validation_list.txt", "testing_list.txt"):
            lists[name] = set(tar.extractfile(member).read().decode("ascii").split())
    val_list, test_list = lists["validation_list.txt"], lists["testing_list.txt"]
    rng = np.random.default_rng(SPLIT_SEED)
    label_of = {name: i for i, name in enumerate(SPEECH_CLASSES)}
    selected: dict[str, tuple[str, int]] = {}  # clip -> (split, label)
    for split in ("fit", "val"):
        in_split = [
            n
            for n in sorted(names)
            if not n.startswith("_background_noise_/")
            and n not in test_list
            and (n in val_list) == (split == "val")
        ]
        words = [n for n in in_split if n.split("/")[0] in SPEECH_WORDS]
        unknown = [n for n in in_split if n.split("/")[0] not in SPEECH_WORDS]
        for n in words:
            selected[n] = (split, label_of[n.split("/")[0]])
        # Like the TensorFlow keyword-spotting recipe: unknown and silence each ~10% of words.
        for i in rng.choice(len(unknown), size=len(words) // 10, replace=False):
            selected[unknown[i]] = (split, label_of["_unknown_"])
    # Pass 2: features of the selected clips plus the background-noise recordings.
    features: dict[str, list] = {"fit": [], "val": [], "test": []}
    labels: dict[str, list] = {"fit": [], "val": [], "test": []}
    order: dict[str, list] = {"fit": [], "val": []}
    noises: dict[str, np.ndarray] = {}
    for name, member, tar in _tar_members(train_archive):
        if name in selected:
            split, label = selected[name]
            order[split].append(
                (name, mfcc(_decode_wav(tar.extractfile(member).read(), name)), label)
            )
        elif name.startswith("_background_noise_/") and name.endswith(".wav"):
            noises[name] = _decode_wav(tar.extractfile(member).read(), name)
    for split in ("fit", "val"):
        for _, feature, label in sorted(order[split], key=lambda item: item[0]):
            features[split].append(feature)
            labels[split].append(label)
    noise_list = [noises[name] for name in sorted(noises)]
    for split in ("fit", "val"):
        words = sum(1 for label in labels[split] if label >= 2)
        for _ in range(words // 10):
            noise = noise_list[rng.integers(len(noise_list))]
            offset = rng.integers(0, len(noise) - 16000)
            features[split].append(mfcc(noise[offset : offset + 16000] * rng.uniform(0.0, 1.0)))
            labels[split].append(label_of["_silence_"])
    test_items = []
    for name, member, tar in _tar_members(test_archive):
        if name.endswith(".wav"):
            folder = name.split("/")[0]
            audio = _decode_wav(tar.extractfile(member).read(), name)
            test_items.append((name, mfcc(audio), label_of[folder]))
    for _, feature, label in sorted(test_items, key=lambda item: item[0]):
        features["test"].append(feature)
        labels["test"].append(label)
    result = {}
    for split in ("fit", "val", "test"):
        result[f"x_{split}"] = np.stack(features[split])
        result[f"y_{split}"] = np.asarray(labels[split], dtype=np.int64)
    cache_dir.mkdir(parents=True, exist_ok=True)
    np.savez(cache, **result)
    return result


def load_speech_commands(raw_root: Path = RAW_ROOT, cache_dir: Path | None = None) -> Dataset:
    """12-class Speech Commands v2 MFCC features (49 x 40).

    Read straight from the two official archives (nothing is extracted). Training and
    validation clips follow ``validation_list.txt``/``testing_list.txt`` of the training
    archive: all clips of the 10 command words, plus a fixed random draw of the other 25 words
    as ``_unknown_`` and 1 s slices of the ``_background_noise_`` recordings (random gain) as
    ``_silence_``, each about 10% of the command clips. The test split is the separate official
    test-set archive (its 12 class folders, 4,890 clips). Features are z-scored per coefficient
    with training statistics.
    """
    raw = fetch("speech-commands", raw_root)
    stored = _speech_features(raw, cache_dir or REPO_ROOT / "data/recurrent")
    (x_fit, x_val, x_test), stats = standardise(stored["x_fit"], stored["x_val"], stored["x_test"])
    info = source_info("speech-commands")
    info.update(
        {
            "dataset_name": "Google Speech Commands v2 (12-class)",
            "preprocessing": (
                "clips padded/trimmed to 1 s; pinned numpy MFCC (see mfcc_params): 49 frames x "
                "40 coefficients; per-coefficient z-score with training statistics"
            ),
            "validation": (
                "official validation_list.txt; unknown and silence each ~10% of command clips"
            ),
            "mfcc_params": MFCC_PARAMS,
            "classes": list(SPEECH_CLASSES),
            "normalisation": stats,
        }
    )
    return Dataset(
        name="speech-commands",
        task="classification",
        outputs=12,
        train=Split(x_fit, stored["y_fit"]),
        val=Split(x_val, stored["y_val"]),
        test=Split(x_test, stored["y_test"]),
        evaluation_split=(
            f"Speech Commands v2 official 12-class test-set archive ({len(x_test):,} clips)"
        ),
        info=info,
    )


LOADERS = {
    "uci-har": load_uci_har,
    "appliance-energy": load_appliance,
    "mnist-rows": load_mnist_rows,
    "spoken-arabic-digit": load_spoken_arabic_digit,
    "tiny-shakespeare": load_tiny_shakespeare,
    "mitbih": load_mitbih,
    "speech-commands": load_speech_commands,
}


def load(dataset: str, **kwargs) -> Dataset:
    return LOADERS[dataset](**kwargs)
