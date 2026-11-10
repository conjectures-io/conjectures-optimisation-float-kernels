"""Tensor scoring and process supervision; DB behavior is in test_tensor_workflow."""

from __future__ import annotations

import copy
import hashlib
import math
from pathlib import Path

import benchmark
import pytest
from comparisons import board_request, board_result, bundle_requests, validate_bundle_result
from config import (
    Config,
    MeasurementRequest,
    RealWorkloadSpec,
    digest,
    file_digest,
    measurement_hash,
)
from multiseed import expanded, workloads
from rewards import score
from scoring import assess
from timing import baseline_round_indices
from workload import baseline_sentinel_rows


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
                "run_seconds": 30,
                "build_seconds": 60,
                "benchmark_seconds": 120,
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
    cells = list(expanded(request))
    repetitions = dict.fromkeys(cells, 1)
    rounds = []
    warmup_policy = request.get("warmup_policy") == "independent-unscored-input-v1"
    for round_index in range(request["timing"]["rounds"]):
        input_plan = {"synthetic": {}, "real": {}}
        warmup_plan = {"synthetic": {}, "real": {}}
        for cell_index, cell in enumerate(cells):
            planned = (request.get("synthetic_input_plan") or {}).get(cell)
            if planned is None:
                seed = 1000 + round_index * len(cells) + cell_index
            else:
                seed = planned[round_index][0][0]
            input_plan["synthetic"][cell] = [seed]
            if warmup_policy:
                warmup = request["synthetic_warmup_plan"][cell]
                warmup_plan["synthetic"][cell] = [group[0] for group in warmup[round_index]]
        row = {
            "reference_first": round_index % 2 == 0,
            "input_plan": input_plan,
            "process_wall_ns": {
                "reference": [100_000] * request["timing"]["inner"],
                "candidate": [100_000] * request["timing"]["inner"],
            },
            "cells": {
                cell: {
                    "reference_ns": 1000,
                    "candidate_ns": int(1000 * ratio),
                    "reference_checksum": 123,
                    "candidate_checksum": 123,
                    "reference_inner_ns": [1000] * request["timing"]["inner"],
                    "candidate_inner_ns": [int(1000 * ratio)] * request["timing"]["inner"],
                }
                for cell in cells
            },
        }
        if warmup_policy:
            row["warmup_plan"] = warmup_plan
            row["warmup_ns"] = {
                side: {key: [100] * request["timing"]["inner"] for key in cells}
                for side in ("reference", "candidate")
            }
        rounds.append(row)

    has_baseline = request.get("baseline_hash") is not None
    baseline_count = (
        min(request["timing"].get("baseline_rounds", 3), request["timing"]["rounds"])
        if has_baseline
        else 0
    )
    baseline_round_indices_for_result = baseline_round_indices(
        request["timing"]["rounds"], baseline_count
    )
    sentinels = baseline_sentinel_rows(
        request["workloads"], request["cells"], request.get("real_workloads", ())
    )
    sentinel_synthetic = set(sentinels["synthetic"])
    baseline_rounds = [
        {
            "paired_round": round_index,
            "input_plan": {
                "synthetic": {
                    cell: [
                        rounds[0 if cell in sentinel_synthetic else round_index]["input_plan"][
                            "synthetic"
                        ][cell][0]
                    ]
                    for cell in cells
                },
                "real": {},
            },
            **(
                {
                    "warmup_plan": {
                        "synthetic": {
                            cell: [
                                rounds[0 if cell in sentinel_synthetic else round_index][
                                    "warmup_plan"
                                ]["synthetic"][cell][0]
                            ]
                            for cell in cells
                        },
                        "real": {},
                    },
                    "warmup_ns": {key: 100 for key in cells},
                }
                if warmup_policy
                else {}
            ),
            "process_wall_ns": [100_000],
            "cells": {
                cell: {
                    "baseline_ns": 1000,
                    "baseline_checksum": 123,
                    "requests": 1,
                    "baseline_inner_ns": [1000],
                    "baseline_ns_per_request": 1000.0,
                }
                for cell in cells
            },
        }
        for round_index in baseline_round_indices_for_result
    ]
    baseline_repetitions = dict.fromkeys(cells, 1)
    baseline_summary = {
        cell: {
            "baseline_median_ns": 1000,
            "baseline_repetitions": 1,
            "baseline_ns_per_request": 1000.0,
            "candidate_median_ns_per_request": int(1000 * ratio),
            "reference_median_ns_per_request": 1000.0,
            "candidate_to_baseline": ratio,
            "reference_to_baseline": 1.0,
        }
        for cell in cells
    }
    mode = "cold"
    build_stages = {}
    for stage in ("shared", "baseline", "reference", "candidate"):
        skipped = stage == "baseline" and not has_baseline
        build_stages[stage] = {
            "status": "skipped" if skipped else "complete",
            "seconds": 0.0,
            "cache_lookup_seconds": 0.0,
            "compile_seconds": 0.0,
            "other_seconds": 0.0,
            **(
                {}
                if skipped
                else {
                    "budget_seconds": request["timing"].get(
                        f"{stage}_build_seconds", request["timing"]["build_seconds"]
                    )
                }
            ),
        }
    timing = request["timing"]
    measurement_policy = {
        "observations_per_workload_per_process": 1,
        "fresh_process_per_pass": True,
        "weights_reloaded_each_pass": True,
        "inference_clock": "supervisor-monotonic-ready-done-v1",
        "baseline_sentinel_policy": "spaced-same-input-per-family-v1",
    }
    if request.get("warmup_policy") == "independent-unscored-input-v1":
        measurement_policy.update(
            unscored_warmup_per_row_per_process=True,
            warmup_inputs_independent_and_frozen=True,
        )
    return {
        "request_hash": measurement_hash(request),
        "measurement_policy": measurement_policy,
        "provenance": {"identity": request["identity"], "machine": "test-machine"},
        "workloads": request["workloads"],
        "repetitions": repetitions,
        "rounds": rounds,
        "baseline": (
            {
                "source_hash": request["baseline_hash"],
                "sentinel_rows": sentinels,
                "rounds": baseline_rounds,
                "repetitions": baseline_repetitions,
                "summary": baseline_summary,
            }
            if has_baseline
            else None
        ),
        "real": {"workloads": [], "rounds": [], "summary": {}, "families": {}},
        "stage_seconds": {
            "build": 0.0,
            "build_overhead": 0.0,
            "build_stages": build_stages,
            "baseline_process": baseline_count * 0.0001,
            "rounds": 0.001,
            "benchmark": 0.001,
        },
        "preparation": {
            "mode": mode,
            "budget_seconds": timing["build_seconds"],
            "stage_budgets_seconds": {
                stage: timing.get(f"{stage}_build_seconds", timing["build_seconds"])
                for stage in ("shared", "baseline", "reference", "candidate")
            },
        },
    }


def request_for(config, board="mlp"):
    plan = config.plan(board)
    return {k: v for k, v in plan["strata"][0].items() if k != "weight"} | {
        "cells": sorted(plan["cells"]),
        "workloads": workloads(plan["strata"][0]["seed"]),
        "timing": plan["timing"],
        "benchmark_identity": plan["benchmark_identity"],
        "candidate_hash": "d" * 64,
        "reference_hash": "e" * 64,
    }


def test_board_requests_bundle_and_project_without_changing_the_frozen_request(config):
    legacy_requests = {board: request_for(config, board) for board in ("mlp", "resnet")}
    legacy_requests["resnet"]["cells"] = [legacy_requests["mlp"]["cells"][0]]
    legacy_bundle = bundle_requests(legacy_requests)
    for board, request in legacy_requests.items():
        assert board_request(legacy_bundle, board) == request
    validate_bundle_result(
        legacy_bundle,
        {
            "bundle_hash": digest(legacy_bundle),
            "comparisons": {board: result(request) for board, request in legacy_requests.items()},
        },
    )

    requests = {board: request_for(config, board) for board in ("mlp", "resnet")}
    requests["resnet"]["cells"] = [requests["mlp"]["cells"][0]]
    seed = 7000
    shared_inputs = {}
    for request in requests.values():
        selected_inputs = {}
        for draw in request["workloads"]:
            for cell in request["cells"]:
                key = f"{draw['id']}|{cell}"
                if key not in shared_inputs:
                    shared_inputs[key] = [
                        [[seed + round_index]] for round_index in range(request["timing"]["rounds"])
                    ]
                    seed += request["timing"]["rounds"]
                selected_inputs[key] = shared_inputs[key]
        request.update(
            suites=["synthetic"],
            real_workloads=[],
            real_sample_plan=None,
            synthetic_input_plan=selected_inputs,
        )

    bundled = bundle_requests(requests)

    assert set(bundled["comparisons"]) == set(requests)
    assert set(bundled["cells"]) == {
        cell for request in requests.values() for cell in request["cells"]
    }
    assert bundled["timing"]["build_seconds"] == 2 * requests["mlp"]["timing"]["build_seconds"]
    assert (
        bundled["timing"]["benchmark_seconds"] == 2 * requests["mlp"]["timing"]["benchmark_seconds"]
    )
    for board, request in requests.items():
        assert board_request(bundled, board) == request
    bundled_result = {
        "bundle_hash": digest(bundled),
        "comparisons": {board: result(request) for board, request in requests.items()},
    }
    for board, request in requests.items():
        assert board_result(bundled, bundled_result, board) == result(request)


def test_board_bundle_rejects_different_hidden_inputs(config):
    requests = {board: request_for(config, board) for board in ("mlp", "resnet")}
    requests["resnet"]["cells"] = list(requests["mlp"]["cells"])
    for board, request in requests.items():
        seed = 9001 + (1000 if board == "resnet" else 0)
        selected_inputs = {}
        for draw in request["workloads"]:
            for cell in request["cells"]:
                selected_inputs[f"{draw['id']}|{cell}"] = [
                    [[seed + round_index]] for round_index in range(request["timing"]["rounds"])
                ]
                seed += request["timing"]["rounds"]
        request["synthetic_input_plan"] = selected_inputs
    with pytest.raises(ValueError, match="synthetic input schedule"):
        bundle_requests(requests)


def test_bundle_runner_executes_candidate_once_for_multiple_references(config, monkeypatch):
    requests = {board: request_for(config, board) for board in ("mlp", "resnet")}
    # The first request represents Overall's superset; the next board scores one shared row.
    requests["resnet"]["cells"] = [requests["mlp"]["cells"][0]]
    requests["resnet"]["timing"]["benchmark_seconds"] //= 2
    candidate_source = b"candidate"
    candidate_hash = hashlib.sha256(candidate_source).hexdigest()
    reference_sources = {}
    for board, request in requests.items():
        request["candidate_hash"] = candidate_hash
        reference_source = f"ref-{board}".encode()
        reference_hash = hashlib.sha256(reference_source).hexdigest()
        request["reference_hash"] = reference_hash
        reference_sources[reference_hash] = reference_source
    bundle = bundle_requests(requests)
    calls = []
    baseline_inputs_seen = []
    baseline_pass_ids = []
    sentinel_keys = set(
        baseline_sentinel_rows(
            requests["mlp"]["workloads"],
            requests["mlp"]["cells"],
            requests["mlp"].get("real_workloads", ()),
        )["synthetic"]
    )

    def fake_run(binary, request, reps, inner, side, deadline, input_sequences):
        calls.append(side)
        if side == "baseline":
            baseline_pass_ids.append(input_sequences.get("__cache_pass__"))
            baseline_inputs_seen.append(
                {key: value for key, value in input_sequences.items() if key != "__cache_pass__"}
            )
        keys = set(benchmark.row_names(request).values())
        return {
            **{key: (100, 42) for key in keys},
            "__raw_ns__": {key: [100] for key in keys},
            "__real_preparation_ns__": {},
            "__process_wall_ns__": [10],
            "__peak_memory_bytes__": [None],
        }

    def fake_measure(request, candidate, reference, mode=None, progress_callback=None, runner=None):
        keys = set(benchmark.row_names(request).values())
        baseline_indices = baseline_round_indices(
            request["timing"]["rounds"], request["timing"].get("baseline_rounds", 3)
        )
        selected_sentinels = set(
            baseline_sentinel_rows(
                request["workloads"], request["cells"], request.get("real_workloads", ())
            )["synthetic"]
        )
        sentinel_inputs = None
        for round_index in range(request["timing"]["rounds"]):
            inputs = {key: [[f"{key}:{round_index}"]] for key in keys}
            if round_index == 0:
                sentinel_inputs = {key: inputs[key] for key in selected_sentinels}
            for side, binary in (("reference", reference), ("candidate", candidate_source)):
                runner(binary, request, {key: 1 for key in keys}, 1, side, object(), inputs)
            if round_index in baseline_indices:
                baseline_inputs = dict(inputs)
                baseline_inputs.update(sentinel_inputs)
                baseline_inputs["__cache_pass__"] = baseline_indices.index(round_index)
                runner(
                    b"baseline",
                    request,
                    {key: 1 for key in keys},
                    1,
                    "baseline",
                    object(),
                    baseline_inputs,
                )
        return {"request_hash": measurement_hash(request)}

    monkeypatch.setattr(benchmark, "measure", fake_measure)
    result = benchmark.measure_bundle(bundle, candidate_source, reference_sources, runner=fake_run)

    assert set(result["comparisons"]) == set(requests)
    assert calls.count("candidate") == requests["mlp"]["timing"]["rounds"]
    assert calls.count("baseline") == requests["mlp"]["timing"]["baseline_rounds"]
    assert calls.count("reference") == 2 * requests["mlp"]["timing"]["rounds"]
    assert result["shared_candidate_process_wall_ns"] == [10] * requests["mlp"]["timing"]["rounds"]
    assert (
        result["shared_baseline_process_wall_ns"]
        == [10] * requests["mlp"]["timing"]["baseline_rounds"]
    )
    assert baseline_pass_ids == [0, 1, 2]
    assert all(
        all(row[key] == baseline_inputs_seen[0][key] for key in sentinel_keys)
        for row in baseline_inputs_seen[1:]
    )


def test_bundle_runner_forwards_frozen_warmup_sequences(config, monkeypatch):
    request = request_for(config, "mlp")
    candidate_source = b"candidate"
    candidate_hash = hashlib.sha256(candidate_source).hexdigest()
    reference_source = b"reference"
    reference_hash = hashlib.sha256(reference_source).hexdigest()
    request["candidate_hash"] = candidate_hash
    request["reference_hash"] = reference_hash
    bundle = bundle_requests({"mlp": request})
    keys = set(benchmark.row_names(request).values())
    warmups = {key: [[f"warmup:{key}"]] for key in keys}
    observed = []

    def fake_run(
        binary, request, reps, inner, side, deadline, input_sequences, *, warmup_sequences=None
    ):
        observed.append((side, warmup_sequences))
        return {
            **{key: (100, 42) for key in benchmark.row_names(request).values()},
            "__raw_ns__": {},
            "__real_preparation_ns__": {},
            "__process_wall_ns__": [10],
            "__peak_memory_bytes__": [None],
        }

    def fake_measure(request, candidate, reference, mode=None, progress_callback=None, runner=None):
        inputs = {key: [[f"input:{key}"]] for key in keys}
        runner(
            candidate,
            request,
            {key: 1 for key in keys},
            1,
            "candidate",
            object(),
            inputs,
            warmup_sequences=warmups,
        )
        return {"request_hash": measurement_hash(request)}

    monkeypatch.setattr(benchmark, "measure", fake_measure)
    benchmark.measure_bundle(
        bundle, candidate_source, {reference_hash: reference_source}, runner=fake_run
    )

    assert observed == [("candidate", warmups)]


def test_baseline_sentinels_are_evenly_spaced():
    assert baseline_round_indices(12, 3) == (0, 6, 11)
    assert baseline_round_indices(12, 1) == (6,)
    assert baseline_round_indices(5, 8) == (0, 1, 2, 3, 4)


def test_baseline_sentinel_rows_select_one_stable_input_per_family():
    selected = baseline_sentinel_rows(
        [{"id": "s0"}],
        ["mlp:b1:wide", "mlp:b1:normal", "resnet:b1:sparse"],
        [
            {"id": "resnet-z", "family": "resnet"},
            {"id": "mlp-z", "family": "mlp"},
            {"id": "mlp-a", "family": "mlp"},
        ],
    )
    assert selected == {
        "synthetic": ["s0|mlp:b1:normal", "s0|resnet:b1:sparse"],
        "real": ["mlp-a", "resnet-z"],
    }


def editable_config(config):
    raw = copy.deepcopy(config.__dict__)
    raw["benchmark"] = {
        "suites": list(raw.pop("suites")),
        "synthetic_cells": list(raw.pop("synthetic_cells")),
        "real_workloads": [workload.json() for workload in raw.pop("real_workloads")],
        "suite_weights": raw.pop("suite_weights"),
        "reuse_board_timings": raw.pop("reuse_board_timings", True),
        "reuse_completed_jobs": raw.pop("reuse_completed_jobs", True),
    }
    return raw


@pytest.mark.parametrize(
    "ratio,outcome", [(1, "above-bar"), (0.9, "qualifies"), (1.2, "guard-failed")]
)
def test_scoring(config, ratio, outcome):
    request = request_for(config)
    decision = assess(config.plan("mlp"), [(request, result(request, ratio))])
    assert decision["score"] == pytest.approx(ratio)
    assert decision["outcome"] == outcome
    assert decision == assess(config.plan("mlp"), [(request, result(request, ratio))])


@pytest.mark.parametrize("real_ratio", [0.8, 1.2])
def test_mixed_suite_score_includes_real_model_evidence(config, monkeypatch, real_ratio):
    import scoring

    plan = config.plan("mlp")
    plan["suites"] = ["synthetic", "real"]
    plan["suite_weights"] = {"synthetic": 0.5, "real": 0.5}
    request = request_for(config)
    measured = result(request, 0.9)
    real_workload = {
        "id": "mlp-real-a",
        "family": "mlp",
        "model": "toy",
        "dataset": "toy",
    }
    plan["real_workloads"] = [real_workload]
    request["suites"] = ["synthetic", "real"]
    request["real_workloads"] = [real_workload]
    measured["real"] = {
        "rounds": [
            {
                "workloads": {
                    "mlp-real-a": {
                        "candidate_ns": int(1000 * real_ratio),
                        "reference_ns": 1000,
                    }
                }
            }
            for _ in range(request["timing"]["rounds"])
        ]
    }
    monkeypatch.setattr(scoring, "validate_result", lambda *_args, **_kwargs: None)
    decision = assess(plan, [(request, measured)])
    expected = math.sqrt(0.9 * real_ratio)
    assert decision["score"] == pytest.approx(expected)
    assert decision["strata"][0]["suite_scores"] == pytest.approx(
        {"synthetic": 0.9, "real": real_ratio}
    )
    assert decision["suite_scores"] == pytest.approx({"synthetic": 0.9, "real": real_ratio})
    assert decision["outcome"] == ("qualifies" if real_ratio == 0.8 else "guard-failed")


def test_configurable_synthetic_cells_are_normalized_per_family(config):
    raw = editable_config(config)
    raw["benchmark"]["synthetic_cells"] = [
        "mlp:b1:normal",
        "mlp:b8:normal",
        "resnet:b1:sparse",
    ]
    compact = Config.parse(raw)

    mlp_cells = compact.plan("mlp")["cells"]
    resnet_cells = compact.plan("resnet")["cells"]
    assert mlp_cells == {"mlp:b1:normal": 0.5, "mlp:b8:normal": 0.5}
    assert resnet_cells == {"resnet:b1:sparse": 1.0}

    raw["benchmark"]["synthetic_cells"] = ["mlp:b1:normal"]
    with pytest.raises(ValueError, match="no cell for model family resnet"):
        Config.parse(raw).plan("resnet")


def test_configured_suite_weights_are_frozen_and_scoped_to_available_families(config, monkeypatch):
    monkeypatch.setattr(RealWorkloadSpec, "validate_assets", lambda *_args: None)
    raw = editable_config(config)
    raw["benchmark"]["suites"] = ["synthetic", "real"]
    raw["benchmark"]["suite_weights"] = {"synthetic": 0.7, "real": 0.3}
    raw["benchmark"]["real_workloads"] = [
        {
            "id": "local-test-mlp",
            "family": "mlp",
            "model": "fixture",
            "dataset": "fixture",
            "adapter": "image-zoo-v1",
            "weights_path": "validator/config.py",
            "weights_sha256": file_digest(Path("validator/config.py")),
            "samples_path": "validator/scoring.py",
            "samples_sha256": file_digest(Path("validator/scoring.py")),
            "asset_revision": "c" * 64,
            "sample_count": 4,
            "sample_words": 1,
            "samples_per_run": 1,
        }
    ]
    configured = Config.parse(raw)
    assert configured.plan("mlp")["suite_weights"] == {"synthetic": 0.7, "real": 0.3}
    assert configured.plan("resnet")["suite_weights"] == {"synthetic": 1.0}
    assert configured.identity("mlp") != config.identity("mlp")


def test_resolved_synthetic_input_plan_is_required_and_validated(config):
    request = request_for(config)
    request["suites"] = ["synthetic"]
    with pytest.raises(ValueError, match="synthetic input plan is required"):
        MeasurementRequest.parse(request)

    request["synthetic_input_plan"] = {
        f"{draw['id']}|{cell}": [
            [[1000 + index * request["timing"]["rounds"] + round_index]]
            for round_index in range(request["timing"]["rounds"])
        ]
        for index, (draw, cell) in enumerate(
            (draw, cell) for draw in request["workloads"] for cell in request["cells"]
        )
    }
    parsed = MeasurementRequest.parse(request)
    assert parsed.synthetic_input_plan == request["synthetic_input_plan"]

    first_key, second_key = list(request["synthetic_input_plan"])[:2]
    request["synthetic_input_plan"][second_key] = request["synthetic_input_plan"][first_key]
    with pytest.raises(ValueError, match="distinct across observations"):
        MeasurementRequest.parse(request)


@pytest.mark.parametrize("enabled", [True, False])
def test_stratified_hardware_guard(config, enabled):
    plan = config.plan("mlp")
    plan["admission"]["guard_enabled"] = enabled
    plan["strata"] = [
        plan["strata"][0] | {"weight": 0.5},
        plan["strata"][0] | {"environment": "arm", "weight": 0.5},
    ]
    a = request_for(config)
    b = a | {"environment": "arm"}
    decision = assess(plan, [(a, result(a, 0.5)), (b, result(b, 1.2))])
    assert decision["score"] < 0.97
    assert decision["outcome"] == ("guard-failed" if enabled else "qualifies")
    assert decision["guard_enabled"] is enabled
    assert decision["strata"][0]["guards"]
    assert any(v > 1.1 for row in decision["strata"] for v in row["guards"].values())
    with pytest.raises(ValueError, match="incomplete"):
        assess(plan, [(a, result(a))])


@pytest.mark.parametrize("ratio,outcome", [(0.9, "qualifies"), (1.2, "above-bar")])
def test_disabled_guard_preserves_aggregate_admission(config, ratio, outcome):
    raw = editable_config(config)
    raw["admission"]["guard_enabled"] = False
    configured = Config.parse(raw)
    request = request_for(configured)
    decision = assess(configured.plan("mlp"), [(request, result(request, ratio))])
    assert decision["outcome"] == outcome


@pytest.mark.parametrize("value", [0, 1, "false", None])
def test_guard_enabled_requires_boolean(config, value):
    raw = editable_config(config)
    raw["admission"]["guard_enabled"] = value
    with pytest.raises(ValueError, match="guard_enabled must be true or false"):
        Config.parse(raw)


def test_peak_memory_is_telemetry_and_does_not_change_admission(config):
    request = request_for(config)
    plan = config.plan("mlp")
    measured = result(request, 0.9)
    for row in measured["rounds"]:
        row["process_peak_memory_bytes"] = {
            "candidate": [None] * request["timing"]["inner"],
            "reference": [1024 * 1024] * request["timing"]["inner"],
        }
    decision = assess(plan, [(request, measured)])
    assert decision["outcome"] == "qualifies"

    measured["rounds"][0]["process_peak_memory_bytes"]["candidate"][0] = -1
    with pytest.raises(ValueError, match="peak-memory"):
        assess(plan, [(request, measured)])


def test_baseline_speed_is_telemetry_and_does_not_change_admission(config):
    request = request_for(config)
    plan = config.plan("mlp")
    request["baseline_hash"] = plan["baseline_hash"]
    measured = result(request, 0.9)
    decision = assess(plan, [(request, measured)])

    baseline_ns = 1_000_000
    for row in measured["baseline"]["rounds"]:
        for cell, observation in row["cells"].items():
            observation["baseline_ns"] = baseline_ns
            observation["baseline_inner_ns"] = [baseline_ns]
            observation["baseline_ns_per_request"] = float(baseline_ns)
    for cell, summary in measured["baseline"]["summary"].items():
        summary["baseline_median_ns"] = baseline_ns
        summary["baseline_ns_per_request"] = float(baseline_ns)
        summary["candidate_to_baseline"] = summary["candidate_median_ns_per_request"] / baseline_ns
        summary["reference_to_baseline"] = summary["reference_median_ns_per_request"] / baseline_ns

    assert assess(plan, [(request, measured)]) == decision


@pytest.mark.parametrize("change", ["nan", "checksum", "missing", "order", "provenance"])
def test_bad_evidence(config, change):
    request = request_for(config)
    data = result(request)
    cell = next(iter(data["repetitions"]))
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
    raw = editable_config(config)
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


def test_default_budgets_vary_by_family_and_architecture(config):
    raw = editable_config(config)
    raw.pop("timing")
    raw["environments"] = {
        "dev": dict(weight=0.5, identity="a" * 64, architecture="amd64"),
        "arm": dict(weight=0.5, identity="b" * 64, architecture="arm64"),
    }
    for board in raw["boards"].values():
        board.pop("timing", None)
    raw["boards"]["overall"] = dict(
        models={"mlp": 0.5, "resnet": 0.5}, reference=raw["boards"]["mlp"]["reference"].copy()
    )
    raw["rewards"]["board_weights"] = {"mlp": 0.25, "resnet": 0.25, "overall": 0.5}
    raw["admission"]["min_rounds"] = 3
    configured = Config.parse(raw)
    assert configured.verification_seconds == 1800
    assert configured.plan("mlp")["timing"]["benchmark_seconds"] == 360
    assert configured.plan("overall")["timing"]["benchmark_seconds"] == 1200
    plan = configured.plan("mlp")
    caps = {st["environment"]: st.get("timing", plan["timing"]) for st in plan["strata"]}
    assert (caps["dev"]["build_seconds"], caps["dev"]["cached_build_seconds"]) == (300, 120)
    assert (caps["arm"]["build_seconds"], caps["arm"]["cached_build_seconds"]) == (960, 180)
    assert Config.parse(editable_config(configured)).public() == configured.public()


def test_environment_limits_are_frozen_and_checked_by_scoring(config):
    raw = editable_config(config)
    raw["environments"]["local"]["timing"] = dict(build_seconds=50, cached_build_seconds=20)
    configured = Config.parse(raw)
    plan = configured.plan("mlp")
    st = plan["strata"][0]
    request = {k: st[k] for k in ("environment", "identity", "seed")} | {
        "cells": sorted(plan["cells"]),
        "timing": st["timing"],
        "workloads": workloads(st["seed"]),
        "benchmark_identity": plan["benchmark_identity"],
        "candidate_hash": "d" * 64,
        "reference_hash": "e" * 64,
    }
    assert assess(plan, [(request, result(request))])["outcome"] == "qualifies"
    wrong = request | {"timing": plan["timing"]}
    with pytest.raises(ValueError, match="incompatible measurement policy"):
        assess(plan, [(wrong, result(wrong))])


def test_build_phase_budgets_can_be_overridden_per_environment(config):
    raw = editable_config(config)
    raw["environments"]["local"]["timing"] = {
        "shared_build_seconds": 40,
        "baseline_build_seconds": 25,
        "reference_build_seconds": 35,
        "candidate_build_seconds": 50,
    }
    configured = Config.parse(raw)
    timing = configured.plan("mlp")["strata"][0]["timing"]
    assert timing["build_seconds"] == 60  # still the overall build cap
    assert timing["shared_build_seconds"] == 40
    assert timing["baseline_build_seconds"] == 25
    assert timing["reference_build_seconds"] == 35
    assert timing["candidate_build_seconds"] == 50


@pytest.mark.parametrize(
    "scope,override",
    [
        ("environments", {"rounds": 10}),
        ("environments", {"build_seconds": 10, "cached_build_seconds": 20}),
        ("boards", {"build_seconds": 300}),
        ("boards", {"benchmark_seconds": -1}),
    ],
)
def test_invalid_budget_overrides_fail_before_planning(config, scope, override):
    raw = editable_config(config)
    key = "local" if scope == "environments" else "mlp"
    raw[scope][key]["timing"] = override
    with pytest.raises(ValueError):
        Config.parse(raw)


def test_winner_only_rewards_keep_seven_independent_pools(config):
    family_weight = 0.5 / 6
    boards = {
        "overall": 0.5,
        "mlp": family_weight,
        "resnet": family_weight,
        "mobilenet": family_weight,
        "normalized_mlp": family_weight,
        "mixer": family_weight,
        "recurrent": family_weight,
    }
    publication = {
        "version": "tensor-top2-v1",
        "rewards": config.rewards | {"board_weights": boards, "rank_shares": [1.0, 0.0]},
        "boards": {
            board: {
                "paused": False,
                "kings": [
                    {"promotion_id": 2, "submission_id": 2, "hotkey": "current-" + board},
                    {"promotion_id": 1, "submission_id": 1, "hotkey": "previous-" + board},
                ],
            }
            for board in boards
        },
    }
    assert score(publication).weights == pytest.approx(
        {"current-" + board: weight for board, weight in boards.items()}
    )
    # A board without a champion leaves its allocation unassigned to competitors.
    publication["boards"]["overall"]["kings"] = []
    assert sum(score(publication).weights.values()) == pytest.approx(0.5)


def test_winner_only_policy_does_not_change_benchmark_identity(config):
    raw = editable_config(config)
    raw["rewards"]["rank_shares"] = [1.0, 0.0]
    updated = Config.parse(raw)
    for board in config.boards:
        assert updated.identity(board) == config.identity(board)
        assert updated.plan(board) == config.plan(board)
    assert updated.rewards["rank_shares"] == [1.0, 0.0]
