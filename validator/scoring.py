"""Pure paired scoring; no database connection or machine-specific timing comparisons."""

from __future__ import annotations

import math
import random
from statistics import median

from config import digest, measurement_hash


def geomean(values: list[tuple[float, float]]) -> float:
    return math.exp(sum(math.log(v) * w for v, w in values) / sum(w for _, w in values))


def quantile(values: list[float], q: float) -> float:
    xs = sorted(values)
    pos = (len(xs) - 1) * q
    lo = int(pos)
    return xs[lo] + (xs[min(lo + 1, len(xs) - 1)] - xs[lo]) * (pos - lo)


def validate_result(request: dict, result: dict, cell_weights: dict | None = None) -> list[float]:
    if result.get("request_hash") != measurement_hash(request):
        raise ValueError("measurement request mismatch")
    provenance = result.get("provenance", {})
    if provenance.get("identity") != request["identity"] or not provenance.get("machine"):
        raise ValueError("environment provenance mismatch")
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
    baseline_indices = set(range(baseline_round_count))
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
        seen_real_samples: set | None = None,
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
            flattened = [index for group in samples for index in group]
            if len(flattened) != len(set(flattened)):
                raise ValueError("real sample indices must be distinct within a round")
            sample_keys = {(spec["id"], index) for index in flattened}
            if seen_real_samples is not None and seen_real_samples.intersection(sample_keys):
                raise ValueError("real sample indices must not repeat across timed rounds")
            if seen_real_samples is not None:
                seen_real_samples.update(sample_keys)

    rounds = result.get("rounds", [])
    if not isinstance(rounds, list) or len(rounds) != request["timing"]["rounds"]:
        raise ValueError("missing paired rounds")
    seen_input_seeds = set()
    seen_real_samples = set()
    for index, row in enumerate(rounds):
        if "suites" in request:
            check_input_plan(
                row.get("input_plan"),
                seen_seeds=seen_input_seeds,
                seen_real_samples=seen_real_samples,
            )
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
        expected_real_samples = row.get("input_plan", {}).get("real", {})
        for item in real_workloads:
            observation = real_rounds[index]["workloads"][item["id"]]
            if (
                "suites" in request
                and observation.get("sample_indices") != expected_real_samples[item["id"]]
            ):
                raise ValueError("real input plan disagrees with measured sample ids")
            expected_baseline = (
                expected_real_samples[item["id"]][0] if index in baseline_indices else None
            )
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
            if type(row.get(key)) is not int or row[key] <= 0:
                raise ValueError("invalid real workload summary duration")
        baseline_ns = row.get("baseline_median_ns")
        baseline_ratio = row.get("candidate_to_baseline")
        if request.get("baseline_hash") is not None:
            if (
                type(baseline_ns) is not int
                or baseline_ns <= 0
                or type(row.get("baseline_model_load_median_ns")) is not int
                or type(row.get("baseline_input_preparation_median_ns")) is not int
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
                paired = rounds[index]["input_plan"]
                expected_synthetic = {
                    key: [values[0]] for key, values in paired["synthetic"].items()
                }
                expected_real = {key: [values[0]] for key, values in paired["real"].items()}
                if row["input_plan"] != {"synthetic": expected_synthetic, "real": expected_real}:
                    raise ValueError("baseline did not use the paired input selection")
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
                        and key in {"baseline_median_ns", "baseline_repetitions"}
                    ):
                        valid = type(actual) is int and actual == expected
                    else:
                        valid = isinstance(actual, (int, float)) and math.isclose(
                            actual, expected, rel_tol=1e-12
                        )
                    if not valid:
                        raise ValueError("invalid baseline summary")
    return scores


def assess(plan, measurements):
    from multiseed import PROFILES

    measurements = sorted(measurements, key=lambda pair: (pair[0]["environment"], pair[0]["seed"]))

    expected = {(s["environment"], s["seed"]): s for s in plan["strata"]}
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
        score = geomean([(median(xs), w["weight"]) for w, xs in groups])
        diagnostics.append(dict(environment=key[0], seed=key[1], score=score, guards=guards))
        environments.append((groups, st["weight"]))
    if seen != set(expected):
        raise ValueError("incomplete required environments/seeds")
    point = geomean(
        [(d["score"], expected[(d["environment"], d["seed"])]["weight"]) for d in diagnostics]
    )
    rng = random.Random(int(digest({"plan": plan, "measurements": measurements})[:16], 16))
    draws = []
    for _ in range(plan["admission"]["draws"]):
        envscores = []
        for groups, envweight in environments:
            n = len(groups[0][1])
            indices = rng.choices(range(n), k=n)
            terms = []
            for p, weight in PROFILES.items():
                items = [(w, xs) for w, xs in groups if w["profile"] == p]
                for w, xs in rng.choices(items, k=len(items)):
                    terms.append((median(xs[i] for i in indices), weight / len(items)))
            envscores.append((geomean(terms), envweight))
        draws.append(1 - geomean(envscores))
    lower = quantile(draws, plan["admission"]["quantile"])
    guard = all(v <= plan["admission"]["guard"] for d in diagnostics for v in d["guards"].values())
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
        score=point, gain_lower=lower, guard_passed=guard, outcome=outcome, strata=diagnostics
    )
