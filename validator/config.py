"""Coordinator-owned instructions. Only resolved public evidence is persisted."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path

from multiseed import WorkloadRecord, workload_records, workloads
from timing import DEFAULT_TIMING, build_defaults, validate_timing
from workload import RealWorkloadSpec

MODELS = ("mlp", "resnet", "mobilenet", "normalized_mlp", "mixer", "transformer")
BATCHES = {m: (1, 8) if m in ("mlp", "normalized_mlp") else (1, 4) for m in MODELS}
KINDS = ("normal", "wide", "sparse")
VERSION = "exact-boards-v6-sample-addressable-real-workloads"
BASELINE_SOURCE = Path(__file__).resolve().parents[1] / "kernels/exact/baseline/Kernel.lean"


def baseline_hash() -> str:
    return hashlib.sha256(BASELINE_SOURCE.read_bytes()).hexdigest()


def digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def file_digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(block)
    return hasher.hexdigest()


@dataclass(frozen=True)
class MeasurementRequest:
    environment: str
    identity: str
    seed: int
    candidate_hash: str
    reference_hash: str
    cells: tuple[str, ...]
    timing: dict
    benchmark_identity: str
    workloads: list[dict]
    baseline_hash: str | None = None
    suites: tuple[str, ...] = ("synthetic",)
    real_workloads: tuple[dict, ...] = ()

    @classmethod
    def parse(cls, request):
        fields = set(cls.__dataclass_fields__)
        required = fields - {"baseline_hash", "suites", "real_workloads"}
        if not required <= set(request) or set(request) - fields:
            raise ValueError("invalid measurement request fields")
        cells = request["cells"]
        if not isinstance(cells, (list, tuple)) or not cells or len(set(cells)) != len(cells):
            raise ValueError("measurement requires unique cell names")
        valid_cells = {f"{m}:b{b}:{kind}" for m in MODELS for b in BATCHES[m] for kind in KINDS}
        if set(cells) - valid_cells:
            raise ValueError("unknown measurement cell")
        for key in ("identity", "candidate_hash", "reference_hash", "benchmark_identity"):
            sha(request[key])
        if request.get("baseline_hash") is not None:
            sha(request["baseline_hash"])
        if type(request["seed"]) is not int or not 0 <= request["seed"] < 2**32:
            raise ValueError("invalid measurement seed")
        suites = tuple(request.get("suites", ("synthetic",)))
        if (
            not suites
            or "synthetic" not in suites
            or len(set(suites)) != len(suites)
            or set(suites) - {"synthetic", "real"}
        ):
            raise ValueError("suites must include synthetic and may include real")
        try:
            records = tuple(WorkloadRecord.parse(item) for item in request["workloads"])
            real_records = tuple(
                RealWorkloadSpec.parse(item) for item in request.get("real_workloads", ())
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid workload plan: {exc}") from exc
        if ("synthetic" in suites and records != workload_records(request["seed"])) or (
            "synthetic" not in suites and records
        ):
            raise ValueError("synthetic workload plan mismatch")
        if ("real" in suites and not real_records) or ("real" not in suites and real_records):
            raise ValueError("real suite selection and workload list disagree")
        if len({item.id for item in real_records}) != len(real_records):
            raise ValueError("real workload ids must be unique")
        validate_timing(request["timing"])
        if any(
            item.samples_per_run * request["timing"]["inner"] * request["timing"]["rounds"]
            > item.sample_count
            for item in real_records
        ):
            raise ValueError("real workload needs enough unique samples for every timed process")
        return cls(
            **(
                request
                | {
                    "cells": tuple(sorted(cells)),
                    "suites": suites,
                    "real_workloads": tuple(item.json() for item in real_records),
                }
            )
        )

    def identity_hash(self):
        return digest(asdict(self))


def measurement_hash(request: dict) -> str:
    return MeasurementRequest.parse(request).identity_hash()


def sha(value: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError("expected a sha256 hex digest")
    return value


def weights(values: dict, name: str) -> dict[str, float]:
    if not values or any(not isinstance(k, str) or not k for k in values):
        raise ValueError(f"{name}: nonempty named weights required")
    result = {k: float(v) for k, v in values.items()}
    if any(not math.isfinite(v) or v < 0 for v in result.values()) or not math.isclose(
        sum(result.values()), 1, abs_tol=1e-9
    ):
        raise ValueError(f"{name}: finite nonnegative weights must sum to one")
    return {k: v for k, v in result.items() if v > 0}


@dataclass(frozen=True)
class Config:
    boards: dict[str, dict]
    environments: dict[str, dict]
    timing: dict
    admission: dict
    rewards: dict
    seeds: tuple[int, ...] = ()
    seed_count: int = 1
    retry_seconds: float = 300
    lease_seconds: float = 120
    alert_seconds: float = 900
    verification_seconds: float = 1800
    proof_identity: str = ""
    benchmark_identity: str = ""
    seed_mode: str = "fixed"
    # Plan uploads the API tagged `dev` (a development deployment); production plans only subnet.
    dev_intake: bool = False
    suites: tuple[str, ...] = ("synthetic",)
    real_workloads: tuple[RealWorkloadSpec, ...] = ()

    @classmethod
    def load(cls, path: str | Path) -> Config:
        with open(path, "rb") as f:
            data = tomllib.load(f)
        for board in data.get("boards", {}).values():
            ref = board.get("reference", {})
            if "path" in ref:
                ref["path"] = str((Path(path).resolve().parent / ref["path"]).resolve())
        return cls.parse(data)

    @classmethod
    def parse(cls, raw: dict) -> Config:
        allowed = {
            "boards",
            "environments",
            "seeds",
            "timing",
            "admission",
            "rewards",
            "retry_seconds",
            "lease_seconds",
            "alert_seconds",
            "verification_seconds",
            "proof_identity",
            "benchmark_identity",
            "seed_mode",
            "seed_count",
            "dev_intake",
            "benchmark",
        }
        if set(raw) - allowed:
            raise ValueError(f"unknown configuration keys: {set(raw) - allowed}")
        data = copy.deepcopy(raw)
        benchmark = data.pop("benchmark", {})
        if not isinstance(benchmark, dict) or set(benchmark) - {"suites", "real_workloads"}:
            raise ValueError("benchmark accepts only suites and real_workloads")
        suites = tuple(benchmark.get("suites", ("synthetic",)))
        if (
            not suites
            or "synthetic" not in suites
            or len(set(suites)) != len(suites)
            or set(suites) - {"synthetic", "real"}
        ):
            raise ValueError("benchmark.suites must include synthetic and may include real")
        real_workloads = tuple(
            RealWorkloadSpec.parse(item) for item in benchmark.get("real_workloads", ())
        )
        if len({item.id for item in real_workloads}) != len(real_workloads):
            raise ValueError("real workload ids must be unique")
        if ("real" in suites and not real_workloads) or ("real" not in suites and real_workloads):
            raise ValueError("benchmark real suite and real_workloads must be enabled together")
        if "real" in suites:
            for workload in real_workloads:
                root = Path(__file__).resolve().parents[1]
                paths = []
                for relative, expected_hash in (
                    (workload.weights_path, workload.weights_sha256),
                    (workload.samples_path, workload.samples_sha256),
                ):
                    source = root / relative
                    asset = source.resolve()
                    try:
                        asset.relative_to(root)
                    except ValueError as exc:
                        raise ValueError(
                            f"real workload asset escapes repository: {workload.id}"
                        ) from exc
                    if (
                        not asset.is_file()
                        or source.is_symlink()
                        or asset.stat().st_size > 512 * 2**20
                        or file_digest(asset) != expected_hash
                    ):
                        raise ValueError(
                            f"real workload asset missing or hash mismatch: {workload.id}"
                        )
                    paths.append(asset)
                workload.validate_assets(*paths)
        timing_overrides = data.get("timing", {})
        data["timing"] = DEFAULT_TIMING | timing_overrides
        if "cached_build_seconds" not in timing_overrides:
            data["timing"]["cached_build_seconds"] = min(180, data["timing"]["build_seconds"])
        validate_timing(data["timing"])
        if "real" in suites and any(
            item.samples_per_run * data["timing"]["inner"] * data["timing"]["rounds"]
            > item.sample_count
            for item in real_workloads
        ):
            raise ValueError("real workload needs enough unique samples for every timed process")
        data["proof_identity"] = sha(data["proof_identity"])
        data["benchmark_identity"] = sha(data["benchmark_identity"])
        seeds = data.get("seeds", ())
        if (
            (data.get("seed_mode", "fixed") == "fixed" and not seeds)
            or len(set(seeds)) != len(seeds)
            or any(type(s) is not int or not 0 <= s < 2**32 for s in seeds)
        ):
            raise ValueError("seeds must be unique UInt32 values")
        data["seeds"] = tuple(seeds)
        data["suites"] = suites
        data["real_workloads"] = real_workloads
        if data.get("seed_mode") == "fresh" and seeds:
            raise ValueError("fresh mode uses seed_count, not seeds")
        if type(data.get("seed_count", 1)) is not int or not 1 <= data.get("seed_count", 1) <= 100:
            raise ValueError("seed_count must be between 1 and 100")
        if data.get("seed_mode", "fixed") not in ("fixed", "fresh"):
            raise ValueError("seed_mode must be fixed or fresh")
        if type(data.get("dev_intake", False)) is not bool:
            raise ValueError("dev_intake must be true or false")
        envs = data["environments"]
        weights({k: v["weight"] for k, v in envs.items()}, "environments")
        for name, env in envs.items():
            if not {"weight", "identity"} <= env.keys() or set(env) - {
                "weight",
                "identity",
                "architecture",
                "timing",
            }:
                raise ValueError(f"{name}: invalid environment settings")
            sha(env["identity"])
            overrides = {
                k: timing_overrides[k]
                for k in ("build_seconds", "cached_build_seconds")
                if k in timing_overrides
            }
            local = env.get("timing", {})
            if set(local) - {"build_seconds", "cached_build_seconds"}:
                raise ValueError("environment timing overrides may only change build limits")
            limits = build_defaults(env.get("architecture", "arm64")) | overrides | local
            if "cached_build_seconds" not in overrides and "cached_build_seconds" not in local:
                limits["cached_build_seconds"] = min(
                    limits["cached_build_seconds"], limits["build_seconds"]
                )
            validate_timing(data["timing"] | limits)
            env["timing"] = limits
        boards = data["boards"]
        if not boards:
            raise ValueError("at least one board required")
        for name, board in boards.items():
            if not re.fullmatch(r"[a-z][a-z0-9_]*", name):
                raise ValueError("invalid board key")
            if set(board) - {"models", "reference", "environments", "timing"} or not {
                "models",
                "reference",
            } <= set(board):
                raise ValueError("board requires models and reference")
            model_weights = weights(board["models"], "models")
            if set(model_weights) - set(MODELS):
                raise ValueError("unknown workload model")
            local = board.get("timing", {})
            if set(local) - {
                "benchmark_seconds",
                "run_seconds",
                "baseline_rounds",
            }:
                raise ValueError("board timing overrides may only change measurement limits")
            board["timing"] = (
                {
                    "benchmark_seconds": 360 if len(model_weights) == 1 else 1200,
                    "run_seconds": data["timing"]["run_seconds"],
                }
                | {
                    k: timing_overrides[k]
                    for k in ("benchmark_seconds", "run_seconds")
                    if k in timing_overrides
                }
                | local
            )
            validate_timing(data["timing"] | board["timing"])
            if "environments" in board:
                override = weights(board["environments"], "board environments")
                if set(override) - envs.keys():
                    raise ValueError("unknown environment override")
            ref = board["reference"]
            if set(ref) != {"sha256", "path"}:
                raise ValueError("initial reference requires path and sha256")
            sha(ref["sha256"])
        timing = data["timing"]
        validate_timing(timing)
        admission = data["admission"]
        if set(admission) != {"bar", "guard", "draws", "quantile", "min_rounds"}:
            raise ValueError("incomplete admission settings")
        if any(not math.isfinite(v) for v in admission.values()):
            raise ValueError("admission settings must be finite")
        if (
            not 0 < admission["bar"] < 1
            or admission["guard"] < 1
            or not 0 < admission["quantile"] < 0.5
        ):
            raise ValueError("invalid admission thresholds")
        if any(type(admission[k]) is not int or admission[k] < 1 for k in ("draws", "min_rounds")):
            raise ValueError("draws and min_rounds must be positive integers")
        if timing["rounds"] < admission["min_rounds"]:
            raise ValueError("not enough configured rounds")
        rewards = data["rewards"]
        if set(rewards) != {"board_weights", "rank_shares", "paused_policy"}:
            raise ValueError("incomplete reward settings")
        weights(rewards["board_weights"], "board rewards")
        if set(rewards["board_weights"]) != set(boards):
            raise ValueError("reward boards must match active boards")
        shares = rewards["rank_shares"]
        if len(shares) != 2:
            raise ValueError("exactly two paid ranks required")
        weights({str(i): v for i, v in enumerate(shares)}, "rank shares")
        if rewards["paused_policy"] not in ("preserve", "treasury"):
            raise ValueError("unknown paused policy")
        for key in ("retry_seconds", "lease_seconds", "alert_seconds", "verification_seconds"):
            if key in data and (not math.isfinite(data[key]) or data[key] <= 0):
                raise ValueError(f"{key} must be finite and positive")
        if data.get("lease_seconds", 120) > 86400 or data.get("retry_seconds", 300) > 86400:
            raise ValueError("lease/retry periods must not exceed one day")
        return cls(**data)

    def public(self) -> dict:
        result = asdict(self)
        for board in result["boards"].values():
            board["reference"].pop("path")
        return result

    def plan(self, board: str) -> dict:
        models = weights(self.boards[board]["models"], "models")
        timing = self.timing | self.boards[board]["timing"]
        cells = {
            f"{m}:b{b}:{kind}": weight / 6
            for m, weight in models.items()
            for b in BATCHES[m]
            for kind in KINDS
        }
        env_weights = self.boards[board].get(
            "environments", {e: env["weight"] for e, env in self.environments.items()}
        )
        strata = [
            {
                "environment": e,
                "identity": self.environments[e]["identity"],
                "seed": seed,
                "weight": weight
                / (len(self.seeds) if self.seed_mode == "fixed" else self.seed_count),
            }
            for e, weight in env_weights.items()
            if weight > 0
            for seed in (self.seeds if self.seed_mode == "fixed" else range(self.seed_count))
        ]
        real_workloads = [
            workload.json() for workload in self.real_workloads if workload.family in models
        ]
        suites = [name for name in self.suites if name != "real" or real_workloads]
        for st in strata:
            resolved = timing | self.environments[st["environment"]]["timing"]
            if resolved != timing:
                st["timing"] = resolved
        return {
            "version": VERSION,
            "workload_policy": "six-workloads-50-30-20-v2-fresh-process",
            "board": board,
            "initial_reference": self.boards[board]["reference"]["sha256"],
            "baseline_hash": baseline_hash(),
            "seed_mode": self.seed_mode,
            "cells": cells,
            "strata": strata,
            "timing": timing,
            "admission": self.admission,
            "proof_identity": self.proof_identity,
            "benchmark_identity": self.benchmark_identity,
            "suites": suites,
            "real_workloads": real_workloads,
        }

    def identity(self, board: str) -> str:
        return digest(self.plan(board))
