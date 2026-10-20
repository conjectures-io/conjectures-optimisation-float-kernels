"""Timing analysis must classify known controls and reject incompatible raw pairs."""

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "exact_timing_plots", ROOT / "scripts/plot-exact-timing.py"
)
timing = importlib.util.module_from_spec(spec)
spec.loader.exec_module(timing)


def collection(tmp_path, ratio):
    raw = tmp_path / "raw"
    raw.mkdir()
    manifest = {
        "sources": {
            name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
            for name in ("validator/scoring.py", "validator/config.py")
        }
    }
    (raw / "manifest.json").write_text(json.dumps(manifest))
    session = dict(
        name="test",
        reference="incumbent",
        candidate="candidate",
        seed=42,
        rounds=5,
        target_ms=100,
        inner=3,
        complete=True,
        seconds=10,
    )
    (raw / "sessions.json").write_text(json.dumps([session]))
    cells = [
        f"{model}:b{batch}:{kind}"
        for model in ("mlp", "cnn", "mnet", "nmlp", "mixer")
        for batch in (1, 8)
        for kind in ("normal", "wide", "sparse")
    ]
    events = []
    for r in range(5):
        for role, binary, factor, position in (
            ("reference", "incumbent", 1, r % 2),
            ("candidate", "candidate", ratio, 1 - r % 2),
        ):
            events.append(
                dict(
                    session="test",
                    phase=role,
                    binary=binary,
                    seed=42,
                    round=r,
                    position=position,
                    inner=3,
                    seconds=1,
                    reps={c: 1 for c in cells},
                    cells={c: {"ns": round(100_000_000 * factor), "checksum": 12} for c in cells},
                )
            )
    path = raw / "processes.jsonl"
    path.write_text("".join(json.dumps(e) + "\n" for e in events))
    return path, events


@pytest.mark.parametrize(
    "ratio,promotes,guard", [(1, False, True), (0.9, True, True), (1.2, False, False)]
)
def test_known_controls(tmp_path, ratio, promotes, guard):
    collection(tmp_path, ratio)
    _, _, sessions, cells, _ = timing.analyse(tmp_path)
    result = sessions[0]
    assert result["score"] == pytest.approx(ratio)
    assert result["ci95"] == pytest.approx([ratio, ratio])
    assert result["would_promote"] is promotes
    assert result["guard"]["ok"] is guard
    assert len(cells) == 150


@pytest.mark.parametrize("damage", ["repetitions", "checksum", "missing-cell"])
def test_bad_pairs_are_refused(tmp_path, damage):
    path, events = collection(tmp_path, 1)
    candidate = events[1]
    cell = next(iter(candidate["cells"]))
    if damage == "repetitions":
        candidate["reps"][cell] = 2
    elif damage == "checksum":
        candidate["cells"][cell]["checksum"] = 13
    else:
        del candidate["cells"][cell]
    path.write_text("".join(json.dumps(e) + "\n" for e in events))
    with pytest.raises(RuntimeError):
        timing.analyse(tmp_path)
