"""Pure paired scoring; no database connection or machine-specific timing comparisons."""

from __future__ import annotations

import math
import random
from statistics import median

from config import digest, measurement_hash
from timing import baseline_round_indices, build_stage_seconds, preparation_seconds
from workload import baseline_sentinel_rows


def geomean(values: list[tuple[float, float]]) -> float:
    values = list(values)
    return math.exp(sum(math.log(v) * w for v, w in values) / sum(w for _, w in values))


def quantile(values: list[float], q: float) -> float:
    xs = sorted(values)
    pos = (len(xs) - 1) * q
    lo = int(pos)
    return xs[lo] + (xs[min(lo + 1, len(xs) - 1)] - xs[lo]) * (pos - lo)


def validate_result(request: dict, result: dict, cell_weights: dict | None = None) -> list[float]:
    if result.get("request_hash") != measurement_hash(request):
        raise ValueError("measurement request mismatch")
    expected_policy = {
        "observations_per_workload_per_process": 1,
        "fresh_process_per_pass": True,
        "weights_reloaded_each_pass": True,
        "inference_clock": "supervisor-monotonic-ready-done-v1",
        "baseline_sentinel_policy": "spaced-same-input-per-family-v1",
    }
    if request.get("warmup_policy") == "independent-unscored-input-v1":
        expected_policy.update(
            unscored_warmup_per_row_per_process=True,
            warmup_inputs_independent_and_frozen=True,
        )
    if result.get("measurement_policy") != expected_policy:
        raise ValueError("measurement timing/isolation policy mismatch")
    provenance = result.get("provenance", {})
    if provenance.get("identity") != request["identity"] or not provenance.get("machine"):
        raise ValueError("environment provenance mismatch")
    if "suites" in request:
        stage_seconds = result.get("stage_seconds")
        preparation = result.get("preparation")
        stages = stage_seconds.get("build_stages") if isinstance(stage_seconds, dict) else None
        expected_stages = {"shared", "baseline", "reference", "candidate"}
        if not isinstance(stages, dict) or set(stages) != expected_stages:
            raise ValueError("missing per-role build stage evidence")
        mode = preparation.get("mode") if isinstance(preparation, dict) else None
        if mode not in ("cold", "cached"):
            raise ValueError("missing build preparation mode")
        expected_budgets = {
            name: build_stage_seconds(request["timing"], name)
            for name in ("shared", "baseline", "reference", "candidate")
        }
        if preparation.get("stage_budgets_seconds") != expected_budgets:
            raise ValueError("build phase caps disagree with the measurement plan")
        durations = []
        for name, row in stages.items():
            if (
                not isinstance(row, dict)
                or row.get("status") not in ("complete", "cached", "skipped")
                or isinstance(row.get("seconds"), bool)
                or not isinstance(row.get("seconds"), (int, float))
                or not math.isfinite(row["seconds"])
                or row["seconds"] < 0
                or any(
                    isinstance(row.get(field), bool)
                    or not isinstance(row.get(field), (int, float))
                    or not math.isfinite(row[field])
                    or row[field] < 0
                    for field in ("cache_lookup_seconds", "compile_seconds", "other_seconds")
                )
                or abs(
                    row["seconds"]
                    - row["cache_lookup_seconds"]
                    - row["compile_seconds"]
                    - row["other_seconds"]
                )
                > 0.02
            ):
                raise ValueError("invalid build stage evidence")
            durations.append(float(row["seconds"]))
            if name == "shared":
                expected_status = "cached" if mode == "cached" else "complete"
            elif name == "baseline" and request.get("baseline_hash") is None:
                expected_status = "skipped"
            elif mode == "cached":
                expected_status = "cached"
            else:
                expected_status = row["status"]
            if row["status"] != expected_status or (
                mode == "cold"
                and expected_status != "skipped"
                and name != "shared"
                and row["status"] not in ("complete", "cached")
            ):
                raise ValueError("build stage status disagrees with the measurement plan")
            if expected_status != "skipped" and row.get("budget_seconds") != build_stage_seconds(
                request["timing"], name
            ):
                raise ValueError("build stage budget disagrees with the measurement plan")
        total_build = stage_seconds.get("build")
        build_overhead = stage_seconds.get("build_overhead")
        if (
            isinstance(total_build, bool)
            or not isinstance(total_build, (int, float))
            or not math.isfinite(total_build)
            or total_build < 0
            or isinstance(build_overhead, bool)
            or not isinstance(build_overhead, (int, float))
            or not math.isfinite(build_overhead)
            or build_overhead < 0
            or abs(sum(durations) + build_overhead - total_build) > 0.02
            or total_build > preparation_seconds(request["timing"], mode) + 0.02
            or preparation.get("budget_seconds") != preparation_seconds(request["timing"], mode)
        ):
            raise ValueError("invalid overall build timing evidence")
    reps = result.get("repetitions", {})
    from multiseed import expanded

    if result.get("workloads") != request["workloads"]:
        raise ValueError("workload plan mismatch")
    real_workloads = request.get("real_workloads", [])
    real = result.get("real")
    if real is None and not real_workloads:
        real = {"workloads": [], "rounds": [], "summary": {}, "families": {}}
    if not isinstance(real, dict) or real.get("workloads") != real_workloads:
        raise ValueError("real workload evidence mismatch")
    real_rounds = real.get("rounds")
    if not isinstance(real_rounds, list) or len(real_rounds) != (
        request["timing"]["rounds"] if real_workloads else 0
    ):
        raise ValueError("missing real workload diagnostic rounds")
    baseline_round_count = (
        min(request["timing"].get("baseline_rounds", 3), request["timing"]["rounds"])
        if request.get("baseline_hash") is not None
        else 0
    )
    baseline_indices = set(
        baseline_round_indices(request["timing"]["rounds"], baseline_round_count)
    )
    sentinels = baseline_sentinel_rows(request["workloads"], request["cells"], real_workloads)
    sentinel_synthetic = set(sentinels["synthetic"])
    sentinel_real = set(sentinels["real"])

    def check_peak_memory(samples, expected_count):
        if (
            not isinstance(samples, list)
            or len(samples) != expected_count
            or any(
                value is not None and (type(value) is not int or value <= 0) for value in samples
            )
        ):
            raise ValueError("invalid peak-memory telemetry")

    for i, row in enumerate(real_rounds):
        if not isinstance(row, dict):
            raise ValueError("invalid real workload round")
        observations = row.get("workloads")
        if (
            row.get("paired_round") != i
            or not isinstance(observations, dict)
            or set(observations) != {item["id"] for item in real_workloads}
        ):
            raise ValueError("invalid real workload round")
        for spec in real_workloads:
            obs = observations[spec["id"]]
            required = {
                "reference_ns",
                "candidate_ns",
                "reference_checksum",
                "candidate_checksum",
                "reference_model_load_ns",
                "candidate_model_load_ns",
                "reference_input_preparation_ns",
                "candidate_input_preparation_ns",
                "samples",
                "sample_indices",
                "baseline_sample_indices",
                "reference_inner_ns",
                "candidate_inner_ns",
            }
            if i in baseline_indices:
                required |= {
                    "baseline_ns",
                    "baseline_checksum",
                    "baseline_model_load_ns",
                    "baseline_input_preparation_ns",
                    "baseline_inner_ns",
                }
            if request.get("warmup_policy") == "independent-unscored-input-v1":
                required |= {"reference_warmup_ns", "candidate_warmup_ns"}
                if i in baseline_indices:
                    required.add("baseline_warmup_ns")
            if set(obs) != required or obs["samples"] != spec["samples_per_run"]:
                raise ValueError("invalid real workload observation fields")
            if (
                not isinstance(obs["sample_indices"], list)
                or len(obs["sample_indices"]) != request["timing"]["inner"]
                or any(
                    not isinstance(group, list)
                    or len(group) != spec["samples_per_run"]
                    or len(set(group)) != len(group)
                    or any(
                        type(index) is not int or not 0 <= index < spec["sample_count"]
                        for index in group
                    )
                    for group in obs["sample_indices"]
                )
            ):
                raise ValueError("invalid real workload sample selection")
            for timing_key in required & {
                "reference_ns",
                "candidate_ns",
                "reference_model_load_ns",
                "candidate_model_load_ns",
                "reference_input_preparation_ns",
                "candidate_input_preparation_ns",
                "baseline_ns",
                "baseline_model_load_ns",
                "baseline_input_preparation_ns",
                "reference_warmup_ns",
                "candidate_warmup_ns",
                "baseline_warmup_ns",
            }:
                value = obs[timing_key]
                if type(value) is not int or value < (1 if timing_key.endswith("_ns") else 0):
                    raise ValueError("invalid real workload duration")
            checksums = [obs["reference_checksum"], obs["candidate_checksum"]]
            if any(type(value) is not int or not 0 <= value < 2**32 for value in checksums):
                raise ValueError("invalid real workload checksum")
            if checksums[0] != checksums[1]:
                raise ValueError("real workload output mismatch")
            for side, duration in (
                ("reference", obs["reference_ns"]),
                ("candidate", obs["candidate_ns"]),
            ):
                raw = obs[f"{side}_inner_ns"]
                if (
                    not isinstance(raw, list)
                    or len(raw) != request["timing"]["inner"]
                    or any(type(value) is not int or value <= 0 for value in raw)
                    or int(median(raw)) != duration
                ):
                    raise ValueError("invalid raw real workload timing samples")
            if i in baseline_indices and (
                type(obs["baseline_checksum"]) is not int
                or not 0 <= obs["baseline_checksum"] < 2**32
                or not isinstance(obs["baseline_inner_ns"], list)
                or len(obs["baseline_inner_ns"]) != 1
                or any(type(value) is not int or value <= 0 for value in obs["baseline_inner_ns"])
                or median(obs["baseline_inner_ns"]) != obs["baseline_ns"]
            ):
                raise ValueError("invalid real baseline evidence")

    def check_input_plan(
        plan,
        *,
        baseline: bool = False,
        seen_seeds: set | None = None,
    ):
        if not isinstance(plan, dict) or set(plan) != {"synthetic", "real"}:
            raise ValueError("invalid paired input plan")
        if not isinstance(plan["synthetic"], dict) or set(plan["synthetic"]) != set(
            expanded(request)
        ):
            raise ValueError("synthetic input plan does not match scored cells")
        expected_real_ids = {spec["id"] for spec in real_workloads}
        if not isinstance(plan["real"], dict) or set(plan["real"]) != expected_real_ids:
            raise ValueError("real input plan does not match workloads")
        groups = 1 if baseline else request["timing"]["inner"]
        all_seeds = []
        for seeds in plan["synthetic"].values():
            if (
                not isinstance(seeds, list)
                or len(seeds) != groups
                or any(type(seed) is not int or not 0 <= seed < 2**32 for seed in seeds)
            ):
                raise ValueError("invalid synthetic input seeds")
            all_seeds.extend(seeds)
        if len(all_seeds) != len(set(all_seeds)):
            raise ValueError("synthetic input seeds must be distinct")
        if seen_seeds is not None and seen_seeds.intersection(all_seeds):
            raise ValueError("synthetic input seeds must not repeat across timed rounds")
        if seen_seeds is not None:
            seen_seeds.update(all_seeds)
        for spec in real_workloads:
            samples = plan["real"][spec["id"]]
            if (
                not isinstance(samples, list)
                or len(samples) != groups
                or any(
                    not isinstance(group, list)
                    or len(group) != spec["samples_per_run"]
                    or any(
                        type(index) is not int or not 0 <= index < spec["sample_count"]
                        for index in group
                    )
                    or len(set(group)) != len(group)
                    for group in samples
                )
            ):
                raise ValueError("invalid real input sample indices")

    rounds = result.get("rounds", [])
    if not isinstance(rounds, list) or len(rounds) != request["timing"]["rounds"]:
        raise ValueError("missing paired rounds")
    seen_input_seeds = set()
    for index, row in enumerate(rounds):
        if "suites" in request:
            check_input_plan(row.get("input_plan"), seen_seeds=seen_input_seeds)
            planned_synthetic = request.get("synthetic_input_plan")
            if planned_synthetic is not None:
                expected = {
                    key: [group[0] for group in values[index]]
                    for key, values in planned_synthetic.items()
                }
                if row["input_plan"]["synthetic"] != expected:
                    raise ValueError("worker synthetic input seeds differ from coordinator plan")
            if request.get("warmup_policy") == "independent-unscored-input-v1":
                expected_warmup_synthetic = {
                    key: [group[0] for group in values[index]]
                    for key, values in request["synthetic_warmup_plan"].items()
                }
                expected_warmup_real = {
                    item_id: schedule[index]
                    for item_id, schedule in (request.get("real_warmup_plan") or {}).items()
                }
                if row.get("warmup_plan") != {
                    "synthetic": expected_warmup_synthetic,
                    "real": expected_warmup_real,
                }:
                    raise ValueError("worker warm-up inputs differ from coordinator plan")
                warmup_ns = row.get("warmup_ns")
                expected_rows = {
                    f"{draw['id']}|{cell}"
                    for draw in request["workloads"]
                    for cell in request["cells"]
                } | {f"{item['id']}|{item['family']}" for item in real_workloads}
                if not isinstance(warmup_ns, dict) or set(warmup_ns) != {
                    "reference",
                    "candidate",
                }:
                    raise ValueError("missing warm-up timing telemetry")
                for side in ("reference", "candidate"):
                    samples = warmup_ns[side]
                    if (
                        not isinstance(samples, dict)
                        or set(samples) != expected_rows
                        or any(
                            not isinstance(values, list)
                            or len(values) != request["timing"]["inner"]
                            or any(type(value) is not int or value <= 0 for value in values)
                            for values in samples.values()
                        )
                    ):
                        raise ValueError("invalid warm-up timing telemetry")
            process_wall = row.get("process_wall_ns")
            if (
                not isinstance(process_wall, dict)
                or set(process_wall) != {"reference", "candidate"}
                or any(
                    not isinstance(samples, list)
                    or len(samples) != request["timing"]["inner"]
                    or any(type(value) is not int or value <= 0 for value in samples)
                    for samples in process_wall.values()
                )
            ):
                raise ValueError("missing raw process wall timings")
        process_peaks = row.get("process_peak_memory_bytes")
        if process_peaks is not None:
            if not isinstance(process_peaks, dict) or set(process_peaks) != {
                "reference",
                "candidate",
            }:
                raise ValueError("invalid process peak-memory telemetry")
            for samples in process_peaks.values():
                check_peak_memory(samples, request["timing"]["inner"])
        expected_real_samples = row.get("input_plan", {}).get("real", {})
        for item in real_workloads:
            observation = real_rounds[index]["workloads"][item["id"]]
            if (
                request.get("real_sample_plan") is not None
                and expected_real_samples[item["id"]]
                != request["real_sample_plan"][item["id"]][index]
            ):
                raise ValueError("worker real sample IDs differ from coordinator plan")
            if (
                "suites" in request
                and observation.get("sample_indices") != expected_real_samples[item["id"]]
            ):
                raise ValueError("real input plan disagrees with measured sample ids")
            if index in baseline_indices:
                if item["id"] in sentinel_real:
                    expected_baseline = request["real_sample_plan"][item["id"]][0][0]
                else:
                    expected_baseline = expected_real_samples[item["id"]][0]
            else:
                expected_baseline = None
            if observation.get("baseline_sample_indices") != expected_baseline:
                raise ValueError("invalid baseline sample selection")
    summary = real.get("summary")
    if not isinstance(summary, dict) or set(summary) != {item["id"] for item in real_workloads}:
        raise ValueError("missing real workload summaries")
    for spec in real_workloads:
        row = summary[spec["id"]]
        if (
            not isinstance(row, dict)
            or row.get("family") != spec["family"]
            or row.get("model") != spec["model"]
            or row.get("dataset") != spec["dataset"]
            or row.get("samples_per_run") != spec["samples_per_run"]
        ):
            raise ValueError("real workload summary identity mismatch")
        for key in (
            "candidate_median_ns",
            "reference_median_ns",
            "candidate_model_load_median_ns",
            "reference_model_load_median_ns",
            "candidate_input_preparation_median_ns",
            "reference_input_preparation_median_ns",
        ):
            value = row.get(key)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value <= 0
            ):
                raise ValueError("invalid real workload summary duration")
        baseline_ns = row.get("baseline_median_ns")
        baseline_ratio = row.get("candidate_to_baseline")
        if request.get("baseline_hash") is not None:
            if (
                isinstance(baseline_ns, bool)
                or not isinstance(baseline_ns, (int, float))
                or not math.isfinite(baseline_ns)
                or baseline_ns <= 0
                or any(
                    isinstance(row.get(key), bool)
                    or not isinstance(row.get(key), (int, float))
                    or not math.isfinite(row[key])
                    or row[key] <= 0
                    for key in (
                        "baseline_model_load_median_ns",
                        "baseline_input_preparation_median_ns",
                    )
                )
            ):
                raise ValueError("missing real baseline summary")
        elif baseline_ns is not None or baseline_ratio is not None:
            raise ValueError("unexpected real baseline summary")
        for key, expected in (
            ("candidate_to_reference", row["candidate_median_ns"] / row["reference_median_ns"]),
            (
                "candidate_to_baseline",
                row["candidate_median_ns"] / baseline_ns if baseline_ns else None,
            ),
        ):
            actual = row.get(key)
            if expected is None:
                if actual is not None:
                    raise ValueError("unexpected real workload ratio")
            elif not isinstance(actual, (int, float)) or not math.isclose(
                actual, expected, rel_tol=1e-12
            ):
                raise ValueError("invalid real workload ratio")
    families = real.get("families")
    expected_families = sorted({spec["family"] for spec in real_workloads})
    if not isinstance(families, dict) or set(families) != set(expected_families):
        raise ValueError("missing real workload family summaries")
    for family in expected_families:
        group = families[family]
        family_ids = {spec["id"] for spec in real_workloads if spec["family"] == family}
        if not isinstance(group, dict) or set(group) != {
            "workloads",
            "equal_weight_candidate_to_reference_geomean",
            "equal_weight_candidate_to_baseline_geomean",
        }:
            raise ValueError("invalid real workload family summary")
        if not isinstance(group["workloads"], dict) or set(group["workloads"]) != family_ids:
            raise ValueError("invalid real workload family membership")
        for workload_id in family_ids:
            if group["workloads"][workload_id] != summary[workload_id]:
                raise ValueError("real family summary disagrees with workload summary")
        reference_geomean = math.exp(
            sum(
                math.log(summary[workload_id]["candidate_to_reference"])
                for workload_id in family_ids
            )
            / len(family_ids)
        )
        baseline_values = [
            summary[workload_id]["candidate_to_baseline"]
            for workload_id in family_ids
            if summary[workload_id]["candidate_to_baseline"] is not None
        ]
        baseline_geomean = (
            math.exp(sum(math.log(value) for value in baseline_values) / len(baseline_values))
            if baseline_values
            else None
        )
        for key, expected in (
            ("equal_weight_candidate_to_reference_geomean", reference_geomean),
            ("equal_weight_candidate_to_baseline_geomean", baseline_geomean),
        ):
            actual = group[key]
            if expected is None:
                if actual is not None:
                    raise ValueError("unexpected real workload family ratio")
            elif (
                not isinstance(actual, (int, float))
                or not math.isfinite(actual)
                or not math.isclose(actual, expected, rel_tol=1e-12)
            ):
                raise ValueError("invalid real workload family ratio")
    cells = list(expanded(request))
    if cell_weights:
        cell_weights = {
            f"{w['id']}|{c}": w["weight"] * weight
            for w in request["workloads"]
            for c, weight in cell_weights.items()
        }
    if set(reps) != set(cells) or any(
        type(n) is not int or not 1 <= n <= 1_000_000 for n in reps.values()
    ):
        raise ValueError("invalid repetition counts")
    scores = []
    for i, row in enumerate(rounds):
        if row.get("reference_first") is not (i % 2 == 0):
            raise ValueError("invalid alternating order")
        if set(row.get("cells", {})) != set(cells):
            raise ValueError("missing or extra cells")
        ratios = []
        for cell, weight in (cell_weights or dict.fromkeys(cells, 1.0)).items():
            obs = row["cells"][cell]
            a, b = obs["reference_ns"], obs["candidate_ns"]
            if any(
                isinstance(x, bool)
                or not isinstance(x, (int, float))
                or not math.isfinite(x)
                or x <= 0
                for x in (a, b)
            ):
                raise ValueError("invalid paired timing")
            checksums = (obs.get("reference_checksum"), obs.get("candidate_checksum"))
            if (
                any(type(x) is not int or not 0 <= x < 2**32 for x in checksums)
                or checksums[0] != checksums[1]
            ):
                raise ValueError("checksum discrepancy")
            for side, duration in (("reference", a), ("candidate", b)):
                raw = obs.get(f"{side}_inner_ns")
                if "suites" in request and (
                    not isinstance(raw, list)
                    or len(raw) != request["timing"]["inner"]
                    or any(type(value) is not int or value <= 0 for value in raw)
                    or int(median(raw)) != duration
                ):
                    raise ValueError("invalid raw paired timing samples")
            ratios.append((b / a, weight))
        scores.append(geomean(ratios))
    if request.get("baseline_hash") is not None:
        evidence = result.get("baseline")
        expected_rounds = baseline_round_count
        baseline_reps = evidence.get("repetitions") if isinstance(evidence, dict) else None
        if (
            not isinstance(evidence, dict)
            or evidence.get("source_hash") != request["baseline_hash"]
            or evidence.get("sentinel_rows") != sentinels
            or not isinstance(baseline_reps, dict)
            or set(baseline_reps) != set(cells)
            or any(type(n) is not int or n < 1 for n in baseline_reps.values())
            or not isinstance(evidence.get("rounds"), list)
            or len(evidence["rounds"]) != expected_rounds
        ):
            raise ValueError("invalid baseline diagnostic evidence")
        seen_baseline_rounds = set()
        for row in evidence["rounds"]:
            index = row.get("paired_round")
            observations = row.get("cells")
            if (
                type(index) is not int
                or not 0 <= index < request["timing"]["rounds"]
                or index in seen_baseline_rounds
                or not isinstance(observations, dict)
                or set(observations) != set(cells)
            ):
                raise ValueError("invalid baseline diagnostic round")
            seen_baseline_rounds.add(index)
            if "suites" in request:
                check_input_plan(row.get("input_plan"), baseline=True)
                process_wall = row.get("process_wall_ns")
                if (
                    not isinstance(process_wall, list)
                    or len(process_wall) != 1
                    or type(process_wall[0]) is not int
                    or process_wall[0] <= 0
                ):
                    raise ValueError("missing baseline process wall timing")
                process_peaks = row.get("process_peak_memory_bytes")
                if process_peaks is not None:
                    check_peak_memory(process_peaks, 1)
                paired = rounds[index]["input_plan"]
                sentinel = rounds[0]["input_plan"]
                expected_synthetic = {
                    key: [(sentinel if key in sentinel_synthetic else paired)["synthetic"][key][0]]
                    for key in paired["synthetic"]
                }
                expected_real = {
                    key: [(sentinel if key in sentinel_real else paired)["real"][key][0]]
                    for key in paired["real"]
                }
                if row["input_plan"] != {"synthetic": expected_synthetic, "real": expected_real}:
                    raise ValueError("baseline telemetry used an unexpected input selection")
                if request.get("warmup_policy") == "independent-unscored-input-v1":
                    paired_warmup = rounds[index].get("warmup_plan")
                    first_warmup = rounds[0].get("warmup_plan")
                    expected_warmup = {
                        "synthetic": {
                            key: [
                                (first_warmup if key in sentinel_synthetic else paired_warmup)[
                                    "synthetic"
                                ][key][0]
                            ]
                            for key in paired_warmup["synthetic"]
                        },
                        "real": {
                            item["id"]: [
                                (first_warmup if item["id"] in sentinel_real else paired_warmup)[
                                    "real"
                                ][item["id"]][0]
                            ]
                            for item in real_workloads
                        },
                    }
                    if row.get("warmup_plan") != expected_warmup:
                        raise ValueError("baseline telemetry used an unexpected warm-up selection")
                    if (
                        not isinstance(row.get("warmup_ns"), dict)
                        or set(row["warmup_ns"]) != expected_rows
                    ):
                        raise ValueError("missing baseline warm-up timing telemetry")
                    if any(
                        type(value) is not int or value <= 0 for value in row["warmup_ns"].values()
                    ):
                        raise ValueError("invalid baseline warm-up timing telemetry")
            for cell, observation in observations.items():
                duration = observation.get("baseline_ns")
                checksum = observation.get("baseline_checksum")
                count = observation.get("requests")
                if (
                    type(duration) is not int
                    or duration <= 0
                    or type(checksum) is not int
                    or not 0 <= checksum < 2**32
                    or type(count) is not int
                    or count != baseline_reps[cell]
                ):
                    raise ValueError("invalid baseline timing or output")
                if "suites" in request:
                    raw = observation.get("baseline_inner_ns")
                    if (
                        not isinstance(raw, list)
                        or len(raw) != 1
                        or type(raw[0]) is not int
                        or raw[0] != duration
                    ):
                        raise ValueError("invalid raw baseline timing sample")
        if seen_baseline_rounds != baseline_indices:
            raise ValueError("baseline sentinel rounds are not correctly spaced")
        summary = evidence.get("summary")
        if "suites" in request and (not isinstance(summary, dict) or set(summary) != set(cells)):
            raise ValueError("missing baseline summary")
        if "suites" in request:
            for cell in cells:
                row = summary[cell]
                if not isinstance(row, dict):
                    raise ValueError("invalid baseline summary")
                baseline_times = [item["cells"][cell]["baseline_ns"] for item in evidence["rounds"]]
                candidate_per_request = median(
                    item["cells"][cell]["candidate_ns"] / reps[cell] for item in rounds
                )
                reference_per_request = median(
                    item["cells"][cell]["reference_ns"] / reps[cell] for item in rounds
                )
                baseline_per_request = median(baseline_times) / baseline_reps[cell]
                expected_values = {
                    "baseline_median_ns": median(baseline_times),
                    "baseline_repetitions": baseline_reps[cell],
                    "baseline_ns_per_request": baseline_per_request,
                    "candidate_median_ns_per_request": candidate_per_request,
                    "reference_median_ns_per_request": reference_per_request,
                    "candidate_to_baseline": candidate_per_request / baseline_per_request,
                    "reference_to_baseline": reference_per_request / baseline_per_request,
                }
                for key, expected in expected_values.items():
                    actual = row.get(key)
                    if (
                        isinstance(expected, int)
                        and not isinstance(expected, bool)
                        and key == "baseline_repetitions"
                    ):
                        valid = type(actual) is int and actual == expected
                    else:
                        valid = (
                            not isinstance(actual, bool)
                            and isinstance(actual, (int, float))
                            and math.isfinite(actual)
                            and math.isclose(actual, expected, rel_tol=1e-12)
                        )
                    if not valid:
                        raise ValueError("invalid baseline summary")
    return scores


def assess(plan, measurements):
    from multiseed import PROFILES

    measurements = sorted(measurements, key=lambda pair: (pair[0]["environment"], pair[0]["seed"]))

    expected = {(s["environment"], s["seed"]): s for s in plan["strata"]}
    family_weights = {}
    for cell, weight in plan["cells"].items():
        family = cell.split(":", 1)[0]
        family_weights[family] = family_weights.get(family, 0.0) + weight
    seen = set()
    environments = []
    diagnostics = []
    for request, result in measurements:
        key = request["environment"], request["seed"]
        if key not in expected or key in seen:
            raise ValueError("unexpected or duplicate stratum")
        seen.add(key)
        st = expected[key]
        if request["identity"] != st["identity"] or set(request["cells"]) != set(plan["cells"]):
            raise ValueError("incompatible stratum")
        if (
            request["timing"] != st.get("timing", plan["timing"])
            or request["benchmark_identity"] != plan["benchmark_identity"]
            or request.get("suites", ["synthetic"]) != plan.get("suites", ["synthetic"])
            or request.get("real_workloads", []) != plan.get("real_workloads", [])
        ):
            raise ValueError("incompatible measurement policy")
        validate_result(request, result, plan["cells"])
        groups = []
        guards = {}
        for w in request["workloads"]:
            scores = []
            for r in result["rounds"]:
                scores.append(
                    geomean(
                        [
                            (
                                r["cells"][f"{w['id']}|{c}"]["candidate_ns"]
                                / r["cells"][f"{w['id']}|{c}"]["reference_ns"],
                                weight,
                            )
                            for c, weight in plan["cells"].items()
                        ]
                    )
                )
            if len(scores) < plan["admission"]["min_rounds"]:
                raise ValueError("insufficient rounds")
            groups.append((w, scores))
            for c in plan["cells"]:
                k = f"{w['id']}|{c}"
                guards[k] = median(
                    r["cells"][k]["candidate_ns"] / r["cells"][k]["reference_ns"]
                    for r in result["rounds"]
                )
        synthetic_score = geomean([(median(xs), w["weight"]) for w, xs in groups])

        real_specs = request.get("real_workloads", [])
        real_rows = result.get("real", {}).get("rounds", []) if real_specs else []
        by_family = {}
        real_ratios = {}
        for spec in real_specs:
            ratios = [
                row["workloads"][spec["id"]]["candidate_ns"]
                / row["workloads"][spec["id"]]["reference_ns"]
                for row in real_rows
            ]
            real_ratios[spec["id"]] = ratios
            workload_ratio = median(ratios)
            guards[f"real:{spec['id']}"] = workload_ratio
            by_family.setdefault(spec["family"], []).append((spec["id"], workload_ratio))

        real_family_scores = {
            family: geomean([(ratio, 1.0) for _, ratio in rows])
            for family, rows in by_family.items()
        }
        real_score = (
            geomean(
                [(value, family_weights[family]) for family, value in real_family_scores.items()]
            )
            if real_family_scores
            else None
        )
        suite_scores = {"synthetic": synthetic_score}
        if real_score is not None:
            suite_scores["real"] = real_score
        suite_weights = plan.get("suite_weights", {"synthetic": 1.0})
        if set(suite_scores) != set(suite_weights):
            raise ValueError("scored suite set disagrees with the frozen plan")
        score = geomean([(suite_scores[name], weight) for name, weight in suite_weights.items()])
        diagnostics.append(
            dict(
                environment=key[0],
                seed=key[1],
                score=score,
                suite_scores=suite_scores,
                real_family_scores=real_family_scores,
                guards=guards,
            )
        )
        environments.append((groups, real_ratios, by_family, st["weight"]))
    if seen != set(expected):
        raise ValueError("incomplete required environments/seeds")
    point = geomean(
        [(d["score"], expected[(d["environment"], d["seed"])]["weight"]) for d in diagnostics]
    )
    suite_names = list(plan.get("suite_weights", {"synthetic": 1.0}))
    aggregate_suite_scores = {
        name: geomean(
            (
                diagnostic["suite_scores"][name],
                expected[(diagnostic["environment"], diagnostic["seed"])]["weight"],
            )
            for diagnostic in diagnostics
        )
        for name in suite_names
    }
    rng = random.Random(int(digest({"plan": plan, "measurements": measurements})[:16], 16))
    draws = []
    for _ in range(plan["admission"]["draws"]):
        envscores = []
        for groups, real_ratio_rows, real_groups, envweight in environments:
            n = len(groups[0][1])
            indices = rng.choices(range(n), k=n)
            terms = []
            for p, weight in PROFILES.items():
                items = [(w, xs) for w, xs in groups if w["profile"] == p]
                for w, xs in rng.choices(items, k=len(items)):
                    terms.append((median(xs[i] for i in indices), weight / len(items)))
            suite_scores = {"synthetic": geomean(terms)}
            if real_groups:
                sampled_families = []
                for family, rows in real_groups.items():
                    sampled = rng.choices(rows, k=len(rows))
                    family_score = geomean(
                        (
                            median(real_ratio_rows[workload_id][i] for i in indices),
                            1.0,
                        )
                        for workload_id, _ in sampled
                    )
                    sampled_families.append((family_score, family_weights[family]))
                suite_scores["real"] = geomean(sampled_families)
            combined = geomean(
                (suite_scores[name], weight)
                for name, weight in plan.get("suite_weights", {"synthetic": 1.0}).items()
            )
            envscores.append((combined, envweight))
        draws.append(1 - geomean(envscores))
    lower = quantile(draws, plan["admission"]["quantile"])
    guard_enabled = plan["admission"].get("guard_enabled", True)
    guard = not guard_enabled or all(
        v <= plan["admission"]["guard"] for d in diagnostics for v in d["guards"].values()
    )
    outcome = (
        "guard-failed"
        if not guard
        else "above-bar"
        if point > plan["admission"]["bar"]
        else "not-significant"
        if lower <= 0
        else "qualifies"
    )
    return dict(
        score=point,
        suite_scores=aggregate_suite_scores,
        gain_lower=lower,
        guard_enabled=guard_enabled,
        guard_passed=guard,
        outcome=outcome,
        strata=diagnostics,
    )
