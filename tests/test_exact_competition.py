"""Tensor scoring and process supervision; DB behavior is in test_tensor_workflow."""

from __future__ import annotations

import copy
import hashlib

import pytest
from config import Config, digest, measurement_hash
from rewards import score
from scoring import assess


@pytest.fixture
def config(tmp_path):
    source = b"initial reference"
    path = tmp_path / "Kernel.lean"
    path.write_bytes(source)
    boards = {
        name: {
            "models": {model: 1.0},
            "reference": {"path": str(path), "sha256": hashlib.sha256(source).hexdigest()},
        }
        for name, model in (("mlp", "mlp"), ("resnet", "resnet"))
    }
    return Config.parse(
        {
            "boards": boards,
            "environments": {"local": {"weight": 1, "identity": "a" * 64}},
            "seeds": [42],
            "timing": {
                "rounds": 5,
                "inner": 1,
                "cell_ms": 1,
                "run_seconds": 30,
                "job_seconds": 120,
            },
            "admission": {
                "bar": 0.97,
                "guard": 1.1,
                "draws": 100,
                "quantile": 0.05,
                "min_rounds": 5,
            },
            "rewards": {
                "board_weights": {"mlp": 0.5, "resnet": 0.5},
                "rank_shares": [0.8, 0.2],
                "paused_policy": "preserve",
            },
            "proof_identity": "b" * 64,
            "benchmark_identity": "c" * 64,
            "retry_seconds": 0.1,
        }
    )


def result(request, ratio=0.9):
    return {
        "request_hash": measurement_hash(request),
        "provenance": {"identity": request["identity"], "machine": "test-machine"},
        "repetitions": {c: 1 for c in request["cells"]},
        "rounds": [
            {
                "reference_first": i % 2 == 0,
                "cells": {
                    c: {
                        "reference_ns": 1000,
                        "candidate_ns": 1000 * ratio,
                        "reference_checksum": 123,
                        "candidate_checksum": 123,
                    }
                    for c in request["cells"]
                },
            }
            for i in range(request["timing"]["rounds"])
        ],
    }


def request_for(config, board="mlp"):
    plan = config.plan(board)
    return {k: v for k, v in plan["strata"][0].items() if k != "weight"} | {
        "cells": sorted(plan["cells"]),
        "timing": plan["timing"],
        "benchmark_identity": plan["benchmark_identity"],
        "candidate_hash": "d" * 64,
        "reference_hash": "e" * 64,
    }


@pytest.mark.parametrize(
    "ratio,outcome", [(1, "above-bar"), (0.9, "qualifies"), (1.2, "guard-failed")]
)
def test_scoring(config, ratio, outcome):
    request = request_for(config)
    decision = assess(config.plan("mlp"), [(request, result(request, ratio))])
    assert decision["score"] == pytest.approx(ratio)
    assert decision["outcome"] == outcome
    assert decision == assess(config.plan("mlp"), [(request, result(request, ratio))])


def test_stratified_hardware_guard(config):
    plan = config.plan("mlp")
    plan["strata"] = [
        plan["strata"][0] | {"weight": 0.5},
        plan["strata"][0] | {"environment": "arm", "weight": 0.5},
    ]
    a = request_for(config)
    b = a | {"environment": "arm"}
    decision = assess(plan, [(a, result(a, 0.5)), (b, result(b, 1.2))])
    assert decision["score"] < 0.97
    assert decision["outcome"] == "guard-failed"
    with pytest.raises(ValueError, match="incomplete"):
        assess(plan, [(a, result(a))])


@pytest.mark.parametrize("change", ["nan", "checksum", "missing", "order", "provenance"])
def test_bad_evidence(config, change):
    request = request_for(config)
    data = result(request)
    cell = next(iter(request["cells"]))
    if change == "nan":
        data["rounds"][0]["cells"][cell]["candidate_ns"] = float("nan")
    elif change == "checksum":
        data["rounds"][0]["cells"][cell]["candidate_checksum"] = 4
    elif change == "missing":
        data["rounds"].pop()
    elif change == "order":
        data["rounds"][0]["reference_first"] = False
    else:
        data["provenance"]["identity"] = "wrong"
    with pytest.raises(ValueError):
        assess(config.plan("mlp"), [(request, data)])


def test_reward_only_identity_and_paths(config):
    raw = copy.deepcopy(config.__dict__)
    raw["rewards"]["rank_shares"] = [0.9, 0.1]
    changed = Config.parse(raw)
    assert config.identity("mlp") == changed.identity("mlp")
    assert digest(config.public()) != digest(changed.public())
    assert "path" not in config.public()["boards"]["mlp"]["reference"]


@pytest.mark.parametrize("n", [0, 1, 2])
def test_two_rank_payout(config, n):
    publication = {
        "version": "tensor-top2-v1",
        "rewards": config.rewards,
        "boards": {
            board: {
                "paused": False,
                "kings": [
                    {"promotion_id": i + 1, "submission_id": i + 1, "hotkey": "alice"}
                    for i in range(n)
                ],
            }
            for board in config.boards
        },
    }
    assert sum(score(publication).weights.values()) == pytest.approx([0, 0.8, 1][n])
    publication["rewards"] = publication["rewards"] | {"paused_policy": "treasury"}
    for board in publication["boards"].values():
        board["paused"] = True
    assert score(publication).weights == {}


def test_supervisor_lost_lease_stops_child(tmp_path):
    import sys

    from process import supervised

    with pytest.raises(RuntimeError, match="lease lost"):
        supervised(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            lambda: False,
            5,
            0.01,
            tmp_path / "process.log",
        )


def test_supervisor_cancellation_is_prompt_even_with_long_heartbeat(tmp_path):
    import sys
    import time

    from process import supervised

    t0 = time.monotonic()
    with pytest.raises(RuntimeError, match="stopping"):
        supervised(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            lambda: True,
            60,
            40,
            tmp_path / "process.log",
            cancelled=lambda: True,
        )
    assert time.monotonic() - t0 < 5


def test_parent_death_kills_worker_subprocess(tmp_path):
    import os
    import signal
    import subprocess
    import sys
    import time
    from pathlib import Path

    from process import environment

    child_pid = tmp_path / "child.pid"
    child_code = (
        f"import os,time; open({str(child_pid)!r}, 'w').write(str(os.getpid())); time.sleep(60)"
    )
    command = [sys.executable, "-c", child_code]
    logfile = str(tmp_path / "parent.log")
    parent_code = (
        f"from process import supervised; supervised({command!r}, "
        f"lambda: True, 90, 30, {logfile!r})"
    )
    parent = subprocess.Popen([sys.executable, "-c", parent_code], env=environment())
    child = None
    try:
        until = time.monotonic() + 5
        while not child_pid.exists() and time.monotonic() < until:
            time.sleep(0.02)
        assert child_pid.exists()
        child = int(child_pid.read_text())
        os.kill(parent.pid, signal.SIGKILL)
        parent.wait(timeout=5)
        until = time.monotonic() + 5
        while time.monotonic() < until:
            status = Path(f"/proc/{child}/status")
            try:
                state = status.read_text()
            except (FileNotFoundError, ProcessLookupError):
                break  # Exiting between lookup and read is successful cleanup.
            if "State:\tZ" in state:
                break
            time.sleep(0.02)
        else:
            pytest.fail("benchmark subprocess survived its dead supervisor")
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait()
        if child:
            try:
                os.kill(child, signal.SIGKILL)
            except ProcessLookupError:
                pass
