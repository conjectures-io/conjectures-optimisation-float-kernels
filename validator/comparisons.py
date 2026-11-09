"""Pure request bundling for one candidate measured against several board champions.

The worker executes a bundle once. Each board keeps its own reference, workload subset and
measurement hash; projecting the bundle back to that board preserves the existing scoring API.
"""

from __future__ import annotations

from config import digest, measurement_hash, sha

_BOARD_LOCAL_FIELDS = {
    "reference_hash",
    "cells",
    "timing",
    "real_workloads",
    "real_sample_plan",
    "synthetic_input_plan",
    "real_warmup_plan",
    "synthetic_warmup_plan",
}


def bundle_requests(requests: dict[str, dict]) -> dict:
    """Combine compatible board requests while preserving a canonical request for each board."""
    if not requests:
        raise ValueError("a comparison bundle needs at least one board")
    names = list(requests)
    first = requests[names[0]]
    if not isinstance(first, dict):
        raise ValueError("board measurement requests must be objects")

    common = {key: value for key, value in first.items() if key not in _BOARD_LOCAL_FIELDS}
    timings = [request.get("timing") for request in requests.values()]
    if any(not isinstance(timing, dict) for timing in timings):
        raise ValueError("board comparison requires a timing profile")
    timing = timings[0]
    for item in timings[1:]:
        if any(item.get(key) != timing.get(key) for key in ("rounds", "inner", "run_seconds")):
            raise ValueError("board requests require matching paired-run timing profiles")
        if any(
            len(
                {
                    profile.get(f"{stage}_build_seconds", profile["build_seconds"])
                    for profile in (timing, item)
                }
            )
            != 1
            for stage in ("shared", "baseline", "reference", "candidate")
        ):
            raise ValueError("board requests require matching per-stage build budgets")
    cells = set()
    real_by_id = {}
    synthetic_inputs = {}
    real_samples = {}
    synthetic_warmups = {}
    real_warmups = {}
    comparisons = {}

    for board, request in requests.items():
        if not isinstance(board, str) or not board or not isinstance(request, dict):
            raise ValueError("invalid board comparison")
        if {
            key: value for key, value in request.items() if key not in _BOARD_LOCAL_FIELDS
        } != common:
            raise ValueError("board requests do not share one execution plan")
        if board in comparisons:
            raise ValueError("duplicate board comparison")
        reference_hash = request.get("reference_hash")
        if not isinstance(reference_hash, str):
            raise ValueError("board comparison requires a reference source hash")
        sha(reference_hash)
        board_cells = request.get("cells")
        if (
            not isinstance(board_cells, list)
            or not board_cells
            or any(not isinstance(cell, str) for cell in board_cells)
            or len(set(board_cells)) != len(board_cells)
        ):
            raise ValueError("board comparison requires unique scored cells")
        cells.update(board_cells)

        board_real = request.get("real_workloads", [])
        if not isinstance(board_real, list):
            raise ValueError("board real workloads must be a list")
        for workload in board_real:
            workload_id = workload.get("id") if isinstance(workload, dict) else None
            if not isinstance(workload_id, str) or not workload_id:
                raise ValueError("real workload requires an ID")
            previous = real_by_id.setdefault(workload_id, workload)
            if previous != workload:
                raise ValueError("boards disagree about a shared real workload")

        comparisons[board] = {
            "reference_hash": reference_hash,
            "cells": board_cells,
            "timing": request["timing"],
            "real_workload_ids": [item["id"] for item in board_real],
            "local_fields": sorted(_BOARD_LOCAL_FIELDS & set(request)),
            "request_hash": measurement_hash(request),
        }

        selected_samples = request.get("real_sample_plan") or {}
        for workload_id, schedule in selected_samples.items():
            previous = real_samples.setdefault(workload_id, schedule)
            if previous != schedule:
                raise ValueError("boards disagree about a shared real sample schedule")
        for workload_id, schedule in (request.get("real_warmup_plan") or {}).items():
            previous = real_warmups.setdefault(workload_id, schedule)
            if previous != schedule:
                raise ValueError("boards disagree about a shared real warm-up schedule")

        selected_inputs = request.get("synthetic_input_plan") or {}
        for workload_cell, schedule in selected_inputs.items():
            previous = synthetic_inputs.setdefault(workload_cell, schedule)
            if previous != schedule:
                raise ValueError("boards disagree about a shared synthetic input schedule")
        for workload_cell, schedule in (request.get("synthetic_warmup_plan") or {}).items():
            previous = synthetic_warmups.setdefault(workload_cell, schedule)
            if previous != schedule:
                raise ValueError("boards disagree about a shared synthetic warm-up schedule")

    real_families = {key: value["family"] for key, value in real_by_id.items()}
    row_sets = {
        board: {
            f"{workload['id']}|{cell}"
            for workload in common.get("workloads", ())
            for cell in comparison["cells"]
        }
        | {
            f"{workload_id}|{real_families[workload_id]}"
            for workload_id in comparison["real_workload_ids"]
        }
        for board, comparison in comparisons.items()
    }
    all_rows = set().union(*row_sets.values())
    if not any(rows == all_rows for rows in row_sets.values()):
        raise ValueError("a board must cover the union workload for shared candidate execution")

    bundled = dict(common)
    bundled["reference_hash"] = requests[names[0]]["reference_hash"]
    # The worker executes all board comparisons under one supervisor. Preserve the per-board
    # timing unchanged in each comparison, and reserve the sum of the old board caps for the
    # combined job-level watchdog.
    bundled["timing"] = dict(timing)
    for key in ("build_seconds", "cached_build_seconds", "benchmark_seconds"):
        values = [item[key] for item in timings if key in item]
        if values:
            bundled["timing"][key] = sum(values)
    if any("baseline_rounds" in item for item in timings):
        bundled["timing"]["baseline_rounds"] = max(
            item.get("baseline_rounds", timing.get("baseline_rounds", 3)) for item in timings
        )
    bundled["cells"] = sorted(cells)
    bundled["real_workloads"] = [real_by_id[key] for key in sorted(real_by_id)]
    bundled["real_sample_plan"] = real_samples or None
    bundled["synthetic_input_plan"] = synthetic_inputs or None
    bundled["real_warmup_plan"] = real_warmups or None
    bundled["synthetic_warmup_plan"] = synthetic_warmups or None
    bundled["comparisons"] = comparisons
    return bundled


def board_request(bundle: dict, board: str) -> dict:
    """Project a bundled request to the exact legacy-shaped request frozen for one board."""
    comparisons = bundle.get("comparisons")
    if not isinstance(comparisons, dict) or board not in comparisons:
        raise ValueError("unknown board in comparison bundle")
    comparison = comparisons[board]
    real_ids = comparison["real_workload_ids"]
    real_workloads = {item["id"]: item for item in bundle.get("real_workloads", [])}
    if set(real_ids) - set(real_workloads):
        raise ValueError("comparison bundle omits a board workload")
    result = {key: value for key, value in bundle.items() if key != "comparisons"}
    result["reference_hash"] = comparison["reference_hash"]
    result["cells"] = comparison["cells"]
    local_fields = set(comparison.get("local_fields", ()))
    for field in _BOARD_LOCAL_FIELDS - {"reference_hash", "cells"}:
        result.pop(field, None)
    result["timing"] = comparison["timing"]
    sample_plan = bundle.get("real_sample_plan") or {}
    warmup_sample_plan = bundle.get("real_warmup_plan") or {}
    if "real_workloads" in local_fields:
        result["real_workloads"] = [real_workloads[key] for key in real_ids]
    else:
        result.pop("real_workloads", None)
    if "real_sample_plan" in local_fields:
        result["real_sample_plan"] = {key: sample_plan[key] for key in real_ids} or None
    if "real_warmup_plan" in local_fields:
        result["real_warmup_plan"] = {key: warmup_sample_plan[key] for key in real_ids} or None
    input_plan = bundle.get("synthetic_input_plan") or {}
    selected_inputs = {
        f"{draw['id']}|{cell}": input_plan[f"{draw['id']}|{cell}"]
        for draw in result["workloads"]
        for cell in result["cells"]
        if f"{draw['id']}|{cell}" in input_plan
    } or None
    if "synthetic_input_plan" in local_fields:
        result["synthetic_input_plan"] = selected_inputs
    warmup_plan = bundle.get("synthetic_warmup_plan") or {}
    selected_warmups = {
        f"{draw['id']}|{cell}": warmup_plan[f"{draw['id']}|{cell}"]
        for draw in result["workloads"]
        for cell in result["cells"]
        if f"{draw['id']}|{cell}" in warmup_plan
    } or None
    if "synthetic_warmup_plan" in local_fields:
        result["synthetic_warmup_plan"] = selected_warmups
    if measurement_hash(result) != comparison["request_hash"]:
        raise ValueError("projected board request does not match its frozen hash")
    return result


def board_result(bundle: dict, result: dict, board: str) -> dict:
    """Select one legacy-shaped result and bind it to the bundled and board request hashes."""
    comparisons = bundle.get("comparisons")
    rows = result.get("comparisons") if isinstance(result, dict) else None
    if not isinstance(comparisons, dict) or board not in comparisons:
        raise ValueError("unknown board in comparison bundle")
    if result.get("bundle_hash") != digest(bundle):
        raise ValueError("comparison bundle result hash mismatch")
    if not isinstance(rows, dict) or set(rows) != set(comparisons):
        raise ValueError("comparison bundle result is incomplete")
    selected = rows[board]
    if not isinstance(selected, dict) or selected.get("request_hash") != comparisons[board].get(
        "request_hash"
    ):
        raise ValueError("board result does not match its frozen request")
    return selected


def validate_bundle_result(bundle: dict, result: dict) -> None:
    """Validate every projected board result before a shared job is accepted into the DB."""
    from scoring import validate_result

    comparisons = bundle.get("comparisons")
    if not isinstance(comparisons, dict) or not comparisons:
        raise ValueError("invalid comparison bundle")
    for board in comparisons:
        request = board_request(bundle, board)
        validate_result(request, board_result(bundle, result, board))
