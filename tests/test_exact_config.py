"""The default local config represents only the initially scored model families."""

import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCORED_FAMILIES = {"mlp", "resnet", "mobilenet", "normalized_mlp", "mixer"}


def test_default_config_uses_five_families_and_six_reward_boards(tmp_path):
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

    assert set(config["boards"]) == SCORED_FAMILIES | {"overall"}
    assert set(config["rewards"]["board_weights"]) == set(config["boards"])
    assert set(config["boards"]["overall"]["models"]) == SCORED_FAMILIES
    assert config["timing"]["rounds"] == 12
    assert config["timing"]["inner"] == 1
    assert config["timing"]["baseline_rounds"] == 3
    assert config["admission"]["min_rounds"] == 5
    compact_cells = config["benchmark"]["synthetic_cells"]
    assert len(compact_cells) == 15
    assert all(":b1:" in cell for cell in compact_cells)

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
    assert len(full_config["benchmark"]["synthetic_cells"]) == 30

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
