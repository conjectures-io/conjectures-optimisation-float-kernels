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

from multiseed import workloads
from timing import validate_timing

MODELS = ("mlp", "resnet", "mobilenet", "normalized_mlp", "mixer")
BATCHES = {m: (1, 8) if m in ("mlp", "normalized_mlp") else (1, 4) for m in MODELS}
KINDS = ("normal", "wide", "sparse")
VERSION = "exact-boards-v3-multiseed"


def digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


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

    @classmethod
    def parse(cls, request):
        if set(request) != set(cls.__dataclass_fields__):
            raise ValueError("invalid measurement request fields")
        cells = request["cells"]
        if not isinstance(cells, (list, tuple)) or not cells or len(set(cells)) != len(cells):
            raise ValueError("measurement requires unique cell names")
        valid_cells = {f"{m}:b{b}:{kind}" for m in MODELS for b in BATCHES[m] for kind in KINDS}
        if set(cells) - valid_cells:
            raise ValueError("unknown measurement cell")
        for key in ("identity", "candidate_hash", "reference_hash", "benchmark_identity"):
            sha(request[key])
        if type(request["seed"]) is not int or not 0 <= request["seed"] < 2**32:
            raise ValueError("invalid measurement seed")
        if request["workloads"] != workloads(request["seed"]):
            raise ValueError("workload plan mismatch")
        validate_timing(request["timing"])
        return cls(**(request | {"cells": tuple(sorted(cells))}))

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
        }
        if set(raw) - allowed:
            raise ValueError(f"unknown configuration keys: {set(raw) - allowed}")
        data = copy.deepcopy(raw)
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
            if set(env) != {"weight", "identity"}:
                raise ValueError(f"{name}: environment requires weight and identity only")
            sha(env["identity"])
        boards = data["boards"]
        if not boards:
            raise ValueError("at least one board required")
        for name, board in boards.items():
            if not re.fullmatch(r"[a-z][a-z0-9_]*", name):
                raise ValueError("invalid board key")
            if set(board) - {"models", "reference", "environments"} or not {
                "models",
                "reference",
            } <= set(board):
                raise ValueError("board requires models and reference")
            model_weights = weights(board["models"], "models")
            if set(model_weights) - set(MODELS):
                raise ValueError("unknown workload model")
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
        return {
            "version": VERSION,
            "workload_policy": "six-workloads-50-30-20-v1",
            "board": board,
            "initial_reference": self.boards[board]["reference"]["sha256"],
            "seed_mode": self.seed_mode,
            "cells": cells,
            "strata": strata,
            "timing": self.timing,
            "admission": self.admission,
            "proof_identity": self.proof_identity,
            "benchmark_identity": self.benchmark_identity,
        }

    def identity(self, board: str) -> str:
        return digest(self.plan(board))
