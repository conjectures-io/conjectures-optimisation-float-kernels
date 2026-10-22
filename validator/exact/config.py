"""Coordinator-owned instructions. Only resolved public evidence is persisted."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path

MODELS = {
    "mlp": "mlp",
    "resnet": "cnn",
    "mobilenet": "mnet",
    "normalized_mlp": "nmlp",
    "mixer": "mixer",
}
BATCHES = {"mlp": (1, 8), "cnn": (1, 4), "mnet": (1, 4), "nmlp": (1, 8), "mixer": (1, 4)}
KINDS = ("normal", "wide", "sparse")
VERSION = "exact-boards-v1"
# The champion audit (`gate.py --audit`): what a failure does (decision D8), the cases per kernel
# and input kind, and the time limit. `alert` changes no payout.
AUDIT_POLICIES = ("alert", "disqualify", "hold")
AUDIT_DEFAULTS = {"policy": "alert", "cases": 50, "seconds": 900}


def digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def measurement_hash(request: dict) -> str:
    return digest(
        {
            k: v
            for k, v in request.items()
            if k not in ("retry_seconds", "lease_seconds", "reuse_job_id")
        }
        | {"cells": sorted(request["cells"])}
    )


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
    seeds: tuple[int, ...]
    timing: dict
    admission: dict
    rewards: dict
    retry_seconds: float = 300
    lease_seconds: float = 120
    alert_seconds: float = 900
    verification_seconds: float = 1800
    proof_identity: str = ""
    benchmark_identity: str = ""
    seed_mode: str = "fixed"
    audit: dict = field(default_factory=lambda: dict(AUDIT_DEFAULTS))

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
            "audit",
        }
        if set(raw) - allowed:
            raise ValueError(f"unknown configuration keys: {set(raw) - allowed}")
        data = copy.deepcopy(raw)
        data["proof_identity"] = sha(data["proof_identity"])
        data["benchmark_identity"] = sha(data["benchmark_identity"])
        seeds = data["seeds"]
        if (
            not seeds
            or len(set(seeds)) != len(seeds)
            or any(type(s) is not int or not 0 <= s < 2**32 for s in seeds)
        ):
            raise ValueError("seeds must be unique UInt32 values")
        data["seeds"] = tuple(seeds)
        if data.get("seed_mode", "fixed") not in ("fixed", "fresh"):
            raise ValueError("seed_mode must be fixed or fresh")
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
            if set(model_weights) - MODELS.keys():
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
        if set(timing) != {"rounds", "inner", "cell_ms", "run_seconds", "job_seconds"}:
            raise ValueError("timing requires rounds, inner, cell_ms, run_seconds, job_seconds")
        for key, val in timing.items():
            if not math.isfinite(val) or val <= 0:
                raise ValueError(f"timing {key} must be positive")
        if any(type(timing[k]) is not int for k in ("rounds", "inner")):
            raise ValueError("rounds and inner must be integers")
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
        audit = AUDIT_DEFAULTS | data.get("audit", {})
        if set(audit) != set(AUDIT_DEFAULTS):
            raise ValueError("audit takes policy, cases and seconds only")
        if audit["policy"] not in AUDIT_POLICIES:
            raise ValueError(f"audit policy must be one of {AUDIT_POLICIES}")
        if type(audit["cases"]) is not int or not 1 <= audit["cases"] <= 1000:
            raise ValueError("audit cases must be an integer from 1 to 1000")
        if not math.isfinite(audit["seconds"]) or audit["seconds"] <= 0:
            raise ValueError("audit seconds must be finite and positive")
        data["audit"] = audit
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
            f"{MODELS[m]}:b{b}:{kind}": weight / 6
            for m, weight in models.items()
            for b in BATCHES[MODELS[m]]
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
                "weight": weight / len(self.seeds),
            }
            for e, weight in env_weights.items()
            if weight > 0
            for seed in self.seeds
        ]
        return {
            "version": VERSION,
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
