"""The default local config represents only the scored small-device model families."""

import hashlib
import importlib.util
import json
import struct
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
from config import BASELINE_SOURCE, DEFAULT_SYNTHETIC_CELLS, Config, baseline_hash
from workload import RealWorkloadSpec

ROOT = Path(__file__).resolve().parents[1]
SCORED_FAMILIES = {"mlp", "resnet", "mobilenet", "normalized_mlp", "mixer", "recurrent"}


def generate(tmp_path, name, *options):
    output = tmp_path / name
    subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/exact-config.py"),
            "--output",
            str(output),
            "--environment",
            "test",
            *options,
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return output


def test_default_config_uses_six_families_and_seven_reward_boards(tmp_path):
    output = tmp_path / "competition.toml"
    subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/exact-config.py"),
            "--output",
            str(output),
            "--environment",
            "test",
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    config = tomllib.loads(output.read_text())

    # The starting incumbent and absolute telemetry must use the same kernel.
    incumbent = ROOT / "kernels/exact/incumbent/Kernel.lean"
    assert BASELINE_SOURCE.read_bytes() == incumbent.read_bytes()
    assert all(
        board["reference"]["sha256"] == baseline_hash() for board in config["boards"].values()
    )
    assert set(config["boards"]) == SCORED_FAMILIES | {"overall"}
    assert list(config["boards"]) == [
        "mlp",
        "resnet",
        "mobilenet",
        "normalized_mlp",
        "mixer",
        "recurrent",
        "overall",
    ]
    assert set(config["rewards"]["board_weights"]) == set(config["boards"])
    assert set(config["boards"]["overall"]["models"]) == SCORED_FAMILIES
    assert config["timing"]["rounds"] == 12
    assert config["timing"]["inner"] == 1
    assert config["timing"]["baseline_rounds"] == 3
    assert config["timing"]["run_seconds"] == 3600
    assert config["admission"]["min_rounds"] == 5
    assert config["benchmark"]["reuse_board_timings"] is False
    assert config["benchmark"]["reuse_completed_jobs"] is False
    assert all(
        config["boards"][board]["timing"]["benchmark_seconds"] == 3600 for board in config["boards"]
    )
    compact_cells = config["benchmark"]["synthetic_cells"]
    assert len(compact_cells) == 18
    assert all(":b1:" in cell for cell in compact_cells)
    assert not any(cell.startswith("transformer:") for cell in compact_cells)
    assert {f"recurrent:b1:{kind}" for kind in ("normal", "wide", "sparse")} <= set(compact_cells)

    full_output = tmp_path / "full-synthetic.toml"
    subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/exact-config.py"),
            "--output",
            str(full_output),
            "--environment",
            "test",
            "--synthetic-full",
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    full_config = tomllib.loads(full_output.read_text())
    full_cells = full_config["benchmark"]["synthetic_cells"]
    assert len(full_cells) == 36
    assert {f"recurrent:b{batch}:sparse" for batch in (1, 4)} <= set(full_cells)
    assert not any(cell.startswith("transformer:") for cell in full_cells)

    tuned_output = tmp_path / "tuned.toml"
    subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/exact-config.py"),
            "--output",
            str(tuned_output),
            "--environment",
            "test",
            "--rounds",
            "5",
            "--inner",
            "2",
            "--baseline-rounds",
            "4",
            "--min-rounds",
            "2",
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    tuned = tomllib.loads(tuned_output.read_text())
    assert {key: tuned["timing"][key] for key in ("rounds", "inner", "baseline_rounds")} == {
        "rounds": 5,
        "inner": 2,
        "baseline_rounds": 4,
    }
    assert tuned["admission"]["min_rounds"] == 2

    transformer_output = tmp_path / "transformer-research.toml"
    subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/exact-config.py"),
            "--output",
            str(transformer_output),
            "--environment",
            "test",
            "--board",
            "transformer",
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    research_config = tomllib.loads(transformer_output.read_text())
    assert set(research_config["boards"]) == {"transformer"}
    assert len(research_config["benchmark"]["synthetic_cells"]) == 3


def test_generated_default_config_parses_and_overall_plan_includes_recurrent(tmp_path):
    configured = Config.load(generate(tmp_path, "default.toml"))
    assert set(configured.boards) == SCORED_FAMILIES | {"overall"}
    assert list(configured.boards)[-2:] == ["recurrent", "overall"]
    assert configured.reuse_board_timings is False
    assert configured.reuse_completed_jobs is False
    overall = configured.plan("overall")
    assert {cell.split(":")[0] for cell in overall["cells"]} == SCORED_FAMILIES
    assert sum(overall["cells"].values()) == pytest.approx(1)
    assert configured.plan("recurrent")["cells"] == pytest.approx(
        {f"recurrent:b1:{kind}": 1 / 3 for kind in ("normal", "wide", "sparse")}
    )


def test_board_reuse_and_timeout_profiles_are_configurable(tmp_path):
    config = tomllib.loads(
        generate(
            tmp_path,
            "custom-timeouts.toml",
            "--reuse-board-timings",
            "--reuse-completed-jobs",
            "--mlp-benchmark-seconds",
            "1800",
            "--family-benchmark-seconds",
            "900",
            "--overall-benchmark-seconds",
            "2400",
            "--run-seconds",
            "2700",
        ).read_text()
    )
    assert config["benchmark"]["reuse_board_timings"] is True
    assert config["benchmark"]["reuse_completed_jobs"] is True
    assert config["boards"]["mlp"]["timing"]["benchmark_seconds"] == 1800
    assert config["boards"]["resnet"]["timing"]["benchmark_seconds"] == 900
    assert config["boards"]["recurrent"]["timing"]["benchmark_seconds"] == 900
    assert config["boards"]["overall"]["timing"]["benchmark_seconds"] == 2400
    assert config["timing"]["run_seconds"] == 2700


def test_board_round_profiles_are_configurable_and_validated(tmp_path):
    output = generate(
        tmp_path,
        "board-rounds.toml",
        "--rounds",
        "12",
        "--min-rounds",
        "4",
        "--board-rounds-json",
        '{"resnet":4,"recurrent":4,"overall":4}',
        "--board-baseline-rounds-json",
        '{"resnet":1,"recurrent":1,"overall":1}',
    )
    configured = Config.load(output)
    assert configured.plan("resnet")["timing"]["rounds"] == 4
    assert configured.plan("resnet")["timing"]["baseline_rounds"] == 1
    assert configured.plan("mlp")["timing"]["rounds"] == 12
    assert configured.plan("overall")["timing"]["rounds"] == 4


def test_recurrent_board_alone_and_with_full_synthetic_cells(tmp_path):
    single = tomllib.loads(generate(tmp_path, "recurrent.toml", "--board", "recurrent").read_text())
    assert set(single["boards"]) == {"recurrent"}
    assert single["boards"]["recurrent"]["models"] == {"recurrent": 1.0}
    assert single["rewards"]["board_weights"] == {"recurrent": 1.0}
    assert single["benchmark"]["synthetic_cells"] == [
        f"recurrent:b1:{kind}" for kind in ("normal", "wide", "sparse")
    ]
    full = tomllib.loads(
        generate(
            tmp_path, "recurrent-full.toml", "--board", "recurrent", "--synthetic-full"
        ).read_text()
    )
    assert full["benchmark"]["synthetic_cells"] == [
        f"recurrent:b{batch}:{kind}" for batch in (1, 4) for kind in ("normal", "wide", "sparse")
    ]


def test_overall_board_alone_uses_every_scored_family(tmp_path):
    config = tomllib.loads(generate(tmp_path, "overall.toml", "--board", "overall").read_text())
    assert set(config["boards"]) == {"overall"}
    assert set(config["boards"]["overall"]["models"]) == SCORED_FAMILIES
    assert {cell.split(":")[0] for cell in config["benchmark"]["synthetic_cells"]} == (
        SCORED_FAMILIES
    )


def test_default_synthetic_cells_exclude_transformer_but_keep_it_selectable():
    assert len(DEFAULT_SYNTHETIC_CELLS) == 18
    assert not any(cell.startswith("transformer:") for cell in DEFAULT_SYNTHETIC_CELLS)
    assert "recurrent:b1:normal" in DEFAULT_SYNTHETIC_CELLS


def write_tensors(path, tensors):
    data = b"".join(struct.pack(f"<I{len(words)}I", len(words), *words) for words in tensors)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return hashlib.sha256(data).hexdigest()


def recurrent_fixture(assets, name, steps=3, features=2, outputs=4, hidden=5):
    """A one-layer GRU `recurrent-v1` fixture with a manifest, as split-real-fixtures writes."""
    rows = 3 * hidden
    tensors = [
        [1, steps, features, outputs, 1, 1, hidden],
        [0] * (rows * features),
        [0] * (rows * hidden),
        [0] * rows,
        [0] * rows,
        [0] * (outputs * hidden),
        [0] * outputs,
    ]
    weights = f"weights/recurrent-v1/{name}/weights.bin"
    samples = f"samples/recurrent-v1/{name}/samples.bin"
    weights_sha = write_tensors(assets / weights, tensors)
    sample_data = struct.pack(f"<{2 * steps * features}I", *range(2 * steps * features))
    (assets / samples).parent.mkdir(parents=True, exist_ok=True)
    (assets / samples).write_bytes(sample_data)
    manifest = {
        "format": "sample-addressable-f32-v1",
        "workload_id": name,
        "adapter": "recurrent-v1",
        "family": "recurrent",
        "model": "gru-h5",
        "dataset": "toy-sequences",
        "sample_count": 2,
        "sample_words": steps * features,
        "weights_path": weights,
        "weights_sha256": weights_sha,
        "samples_path": samples,
        "samples_sha256": hashlib.sha256(sample_data).hexdigest(),
    }
    target = assets / "models/recurrent/toy" / name
    target.mkdir(parents=True)
    (target / "manifest.json").write_text(json.dumps(manifest))
    return manifest


@pytest.fixture
def exact_config(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location("exact_config", ROOT / "scripts/exact-config.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # The assets must sit inside the generator's root; use a scratch root for the bundle.
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(module, "machine_identity", lambda: "a" * 64)
    return module


def run_generator(module, monkeypatch, *options):
    monkeypatch.setattr(sys, "argv", ["exact-config.py", *options])
    module.main()


def test_real_recurrent_switch_selects_every_recurrent_manifest(
    exact_config, monkeypatch, tmp_path
):
    assets = tmp_path / "assets"
    manifests = [recurrent_fixture(assets, name) for name in ("seq-b", "seq-a")]
    # A manifest elsewhere in the bundle is not a recurrent fixture and is not selected.
    other = assets / "models/uci-har/mlp/manifest.json"
    other.parent.mkdir(parents=True)
    other.write_text(json.dumps({"adapter": "uci-har-v1", "workload_id": "har-mlp"}))
    output = tmp_path / "real.toml"
    run_generator(
        exact_config,
        monkeypatch,
        "--output",
        str(output),
        "--assets-dir",
        str(assets),
        "--reference",
        str(ROOT / "kernels/exact/incumbent/Kernel.lean"),
        "--real-recurrent",
    )
    config = tomllib.loads(output.read_text())
    assert config["benchmark"]["suites"] == ["synthetic", "real"]
    assert config["benchmark"]["suite_weights"] == {"synthetic": 0.5, "real": 0.5}
    real = config["benchmark"]["real_workloads"]
    assert [item["id"] for item in real] == ["seq-a", "seq-b"]
    for item in real:
        spec = RealWorkloadSpec.parse(item)
        assert (spec.adapter, spec.family, spec.samples_per_run) == ("recurrent-v1", "recurrent", 1)
        spec.validate_assets(tmp_path / spec.weights_path, tmp_path / spec.samples_path)
    assert {item["weights_sha256"] for item in real} == {m["weights_sha256"] for m in manifests}


def test_real_recurrent_switch_requires_fixtures_and_rejects_other_adapters(
    exact_config, monkeypatch, tmp_path, capsys
):
    assets = tmp_path / "assets"
    (assets / "models").mkdir(parents=True)
    options = [
        "--assets-dir",
        str(assets),
        "--reference",
        str(ROOT / "kernels/exact/incumbent/Kernel.lean"),
        "--real-recurrent",
    ]
    with pytest.raises(SystemExit):
        run_generator(exact_config, monkeypatch, "--output", str(tmp_path / "a.toml"), *options)
    assert "requires fixtures under" in capsys.readouterr().err
    stray = assets / "models/recurrent/odd/manifest.json"
    stray.parent.mkdir(parents=True)
    stray.write_text(json.dumps({"adapter": "image-zoo-v1", "workload_id": "odd"}))
    with pytest.raises(SystemExit):
        run_generator(exact_config, monkeypatch, "--output", str(tmp_path / "b.toml"), *options)
    assert "uses adapter 'image-zoo-v1'" in capsys.readouterr().err
