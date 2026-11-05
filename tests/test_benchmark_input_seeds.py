from pathlib import Path

import benchmark
import pytest
import sandbox
from benchmark import input_seed_bank
from multiseed import workloads


def test_paired_input_seed_bank_is_reproducible_and_changes_with_key():
    request = {
        "cells": ["mixer:b4:sparse", "mlp:b1:normal"],
        "workloads": [{"id": "s0"}, {"id": "s1"}],
    }
    reps = {
        "s0|mlp:b1:normal": 2,
        "s0|mixer:b4:sparse": 1,
        "s1|mlp:b1:normal": 1,
        "s1|mixer:b4:sparse": 3,
    }
    key = bytes(range(32))

    first = input_seed_bank(request, reps, inner=3, key=key)
    replay = input_seed_bank(request, reps, inner=3, key=key)
    other = input_seed_bank(request, reps, inner=3, key=bytes(reversed(range(32))))

    assert first == replay
    assert len(first) == 3 * sum(reps.values())
    assert len(set(first)) == len(first)
    assert first != other


@pytest.mark.parametrize("outcome", ["success", "timeout", "exception"])
def test_overall_seed_bank_exceeds_old_env_limit_and_is_removed(tmp_path, monkeypatch, outcome):
    monkeypatch.setattr(sandbox, "LIB", tmp_path)
    request = {
        "seed": 1103123939,
        "cells": list(benchmark.INPUT_CELL_ORDER),
        "workloads": workloads(1103123939),
        "timing": {"run_seconds": 300},
    }
    reps = {name: 200 for name in benchmark.row_names(request).values()}
    seed_paths, banks = [], []

    def run(command, timeout, **kwargs):
        env = kwargs["env_extra"]
        path = Path(env["BENCH_INPUT_SEEDS_FILE"])
        seed_paths.append(path)
        banks.append(path.read_text())
        assert path.parent == tmp_path
        assert len(banks[-1]) > 800_000
        assert len(banks[-1].split(",")) == 3 * sum(reps.values())
        assert sum(len(k) + len(v) + 2 for k, v in env.items()) < 20_000
        if outcome == "exception":
            raise OSError("sandbox could not start")
        if outcome == "timeout":
            return 124, ""
        return 0, "\n".join(f"{row} true 100 42" for row in benchmark.row_names(request))

    monkeypatch.setattr(sandbox, "run", run)
    for side in ("reference", "candidate"):

        def invoke():
            return benchmark.run_binary(
                Path(side),
                request,
                reps,
                3,
                side,
                benchmark.Deadline("benchmark", 300),
                input_seed_key=bytes(range(32)),
            )

        if outcome == "success":
            assert len(invoke()) == 180
        elif outcome == "timeout":
            with pytest.raises(benchmark.MeasurementFailure, match="benchmark-timeout"):
                invoke()
        else:
            with pytest.raises(OSError, match="sandbox could not start"):
                invoke()
        assert not seed_paths[-1].exists()
    assert banks[0] == banks[1]
    assert not list(tmp_path.iterdir())
