from benchmark import input_seed_bank, input_seed_environment


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


def test_seed_environment_splits_values_without_breaking_tokens():
    values = list(range(30_000))
    env = input_seed_environment(values)
    count = int(env["BENCH_INPUT_SEED_CHUNKS"])

    assert count > 1
    assert all(len(env[f"BENCH_INPUT_SEEDS_{i}"]) <= 100_000 for i in range(count))
    assert ",".join(env[f"BENCH_INPUT_SEEDS_{i}"] for i in range(count)) == ",".join(
        map(str, values)
    )
