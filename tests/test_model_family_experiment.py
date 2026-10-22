"""Check that experiment replay projects paired evidence onto the correct boards."""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "family_experiment", ROOT / "scripts/compare-model-families.py"
)
experiment = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(experiment)


def test_native_mac_command_does_not_use_taskset(monkeypatch):
    monkeypatch.setattr(experiment.platform, "system", lambda: "Darwin")
    assert experiment.benchmark_command(Path("/tmp/bench"), 3) == ["/tmp/bench"]


def test_linux_command_keeps_cpu_affinity(monkeypatch):
    monkeypatch.setattr(experiment.platform, "system", lambda: "Linux")
    assert experiment.benchmark_command(Path("/tmp/bench"), 3) == [
        "taskset",
        "-c",
        "3",
        "/tmp/bench",
    ]


def test_mac_identity_uses_native_metadata(monkeypatch):
    monkeypatch.setattr(experiment.platform, "system", lambda: "Darwin")
    calls = []

    def output(command, **kwargs):
        calls.append(command)
        return "Apple test CPU\n"

    monkeypatch.setattr(experiment.subprocess, "check_output", output)
    assert len(experiment.machine_identity()) == 64
    assert ["sysctl", "-n", "machdep.cpu.brand_string"] in calls
    assert ["cc", "--version"] in calls


def test_replay_keeps_family_gain_separate_from_other_families(tmp_path):
    pytest.importorskip("matplotlib")
    manifest = {
        "stage": "pilot",
        "settings": experiment.STAGES["pilot"],
        "machine": "a" * 64,
        "host": {"node": "test"},
        "benchmark": "b" * 64,
        "sources": {"candidate": "c" * 64, "reference": "d" * 64},
        "scoring": experiment.sha(experiment.SCORING_PATH.read_bytes()),
        "config": experiment.sha(experiment.CONFIG_PATH.read_bytes()),
    }
    experiment.save(tmp_path / "raw/manifest.json", manifest)
    rounds = [
        {
            "reference_first": i % 2 == 0,
            "cells": {
                cell: {
                    "reference_ns": 1000,
                    "candidate_ns": 500 if cell.startswith("mobilenet:") else 1000,
                    "reference_checksum": 123,
                    "candidate_checksum": 123,
                }
                for cell in experiment.CELLS
            },
        }
        for i in range(5)
    ]
    experiment.save(
        tmp_path / "raw/sessions/control/result.json",
        dict(
            candidate="candidate",
            reference="reference",
            seed=42,
            repeat=0,
            repetitions=dict.fromkeys(experiment.CELLS, 1),
            rounds=rounds,
        ),
    )
    events = []
    for name, role in (
        ("calibration", "reference"),
        ("0-reference", "reference"),
        ("0-candidate", "candidate"),
    ):
        events.append(
            dict(
                index=name,
                seconds=0.01,
                rows={
                    cell: dict(ns=values[f"{role}_ns"], checksum=123)
                    for cell, values in rounds[0]["cells"].items()
                },
            )
        )
    log = tmp_path / "raw/sessions/control/attempt-1/processes.jsonl"
    log.parent.mkdir()
    log.write_text("\n".join(json.dumps(event) for event in events))
    experiment.summarize(tmp_path)
    decisions = {
        row["board"]: row for row in json.loads((tmp_path / "summaries/decisions.json").read_text())
    }
    assert decisions["mobilenet"]["score"] == pytest.approx(0.5)
    assert decisions["overall"]["score"] == pytest.approx(0.5**0.2)
    assert decisions["mobilenet"]["outcome"] == "qualifies"
    assert decisions["overall"]["outcome"] == "qualifies"
    for board in ("mlp", "resnet", "normalized_mlp", "mixer"):
        assert decisions[board]["score"] == pytest.approx(1)
        assert decisions[board]["outcome"] == "above-bar"
    assert (tmp_path / "visualizations/family-comparison.svg").exists()
    estimates = json.loads((tmp_path / "summaries/runtime-estimates.json").read_text())
    assert len(estimates) == 3
    assert estimates[-1]["session_minutes"] > estimates[0]["session_minutes"]


def test_replay_refuses_changed_scoring_code(tmp_path):
    experiment.save(tmp_path / "raw/manifest.json", {"scoring": "wrong"})
    with pytest.raises(ValueError, match="scoring/config changed"):
        experiment.summarize(tmp_path)


@pytest.mark.parametrize(
    "options",
    [
        ["--repeats", "0"],
        ["--seeds", "42", "42"],
        ["--seeds", "-1"],
        ["--rounds", "0"],
        ["--inner", "-1"],
        ["--cell-ms", "nan"],
    ],
)
def test_invalid_schedule_override_does_not_start_collection(tmp_path, options):
    output = tmp_path / "experiment"
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/compare-model-families.py"),
            "--output",
            str(output),
            *options,
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert not output.exists()
