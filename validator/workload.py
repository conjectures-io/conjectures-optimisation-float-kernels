"""Small immutable workload identities shared by benchmark preparation and reporting."""

from __future__ import annotations

import math
import re
import struct
from dataclasses import dataclass
from pathlib import PurePosixPath
from pathlib import Path
from typing import Literal

Suite = Literal["synthetic", "real"]
SYNTHETIC_REVISION = "seeded-profiles-v1"


@dataclass(frozen=True)
class WorkloadSpec:
    suite: Suite
    id: str
    family: str
    model: str
    dataset: str
    batch: int
    input_selection: str
    asset_revision: str | None
    seed: int | None
    profile: str | None

    def __post_init__(self) -> None:
        if not self.id or not self.family or not self.model or not self.dataset:
            raise ValueError("workload identity fields must be nonempty")
        if type(self.batch) is not int or self.batch < 1:
            raise ValueError("workload batch must be a positive integer")
        if self.suite == "synthetic":
            if self.asset_revision != SYNTHETIC_REVISION or self.seed is None or not self.profile:
                raise ValueError("synthetic workload requires its profile, seed and suite revision")
        elif self.suite == "real":
            if not self.asset_revision or self.seed is not None or self.profile is not None:
                raise ValueError("real workload requires an asset revision, not a synthetic seed")
        else:
            raise ValueError("unknown benchmark suite")

    @classmethod
    def synthetic_cell(cls, draw: dict, cell: str) -> WorkloadSpec:
        match = re.fullmatch(r"([a-z][a-z0-9_]*):b([1-9][0-9]*):([a-z]+)", cell)
        if match is None:
            raise ValueError(f"invalid synthetic cell {cell!r}")
        model, batch_text, kind = match.groups()
        if kind not in {"normal", "wide", "sparse"}:
            raise ValueError(f"unknown synthetic data profile {kind!r}")
        if set(draw) != {"id", "seed", "profile", "weight"}:
            raise ValueError("invalid synthetic workload draw")
        return cls(
            suite="synthetic",
            id=f"{draw['id']}|{cell}",
            family=model,
            model=model,
            dataset=f"generated-{kind}",
            batch=int(batch_text),
            input_selection=f"{draw['id']}:{draw['seed']}",
            asset_revision=SYNTHETIC_REVISION,
            seed=draw["seed"],
            profile=draw["profile"],
        )


@dataclass(frozen=True)
class RealWorkloadSpec:
    """A local fixture with separately pinned weights and fixed-width sample records."""

    id: str
    family: str
    model: str
    dataset: str
    adapter: str
    weights_path: str
    weights_sha256: str
    samples_path: str
    samples_sha256: str
    asset_revision: str
    sample_count: int
    sample_words: int
    samples_per_run: int

    @classmethod
    def parse(cls, raw: dict) -> RealWorkloadSpec:
        fields = set(cls.__dataclass_fields__)
        if not isinstance(raw, dict) or set(raw) != fields:
            raise ValueError("invalid real workload fields")
        for field in ("id", "family", "model", "dataset", "adapter", "asset_revision"):
            if not isinstance(raw[field], str) or not raw[field]:
                raise ValueError(f"real workload {field} must be nonempty text")
        if (
            any(
                not isinstance(raw[field], str)
                or "\\" in raw[field]
                or any(char in raw[field] for char in ",|=\n\r")
                for field in ("weights_path", "samples_path")
            )
        ):
            raise ValueError("real workload paths must be relative POSIX paths")
        adapters = {
            "image-zoo-v1": {"mlp", "resnet", "mobilenet", "normalized_mlp", "mixer"},
            "uci-har-v1": {"mlp", "resnet", "mobilenet", "normalized_mlp", "mixer"},
            "appliance-mixer-v1": {"mixer"},
        }
        if raw["adapter"] not in adapters or raw["family"] not in adapters[raw["adapter"]]:
            raise ValueError("unsupported real workload adapter/family pair")
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", raw["id"]):
            raise ValueError("invalid real workload id")
        for field in ("weights_path", "samples_path"):
            path = PurePosixPath(raw[field])
            if path.is_absolute() or ".." in path.parts or not path.parts:
                raise ValueError(f"real workload {field} must stay inside the repository")
        for field in ("weights_sha256", "samples_sha256"):
            if not isinstance(raw[field], str) or not re.fullmatch(r"[0-9a-f]{64}", raw[field]):
                raise ValueError(f"real workload {field} must be a SHA-256 digest")
        for field in ("sample_count", "sample_words", "samples_per_run"):
            value = raw[field]
            if type(value) is not int or value < 1:
                raise ValueError(f"real workload {field} must be a positive integer")
        if raw["samples_per_run"] > raw["sample_count"]:
            raise ValueError("samples_per_run cannot exceed sample_count")
        return cls(**raw)

    def json(self) -> dict:
        from dataclasses import asdict

        return asdict(self)

    def validate_assets(self, weights: Path, samples: Path) -> None:
        """Check pinned weights and the sample-record dimensions before timing."""
        lengths = []
        size_bytes = weights.stat().st_size
        offset = 0
        with weights.open("rb") as stream:
            while offset < size_bytes:
                header = stream.read(4)
                if len(header) != 4:
                    raise ValueError(f"{self.id}: truncated tensor header")
                count = struct.unpack("<I", header)[0]
                offset += 4
                if count > (size_bytes - offset) // 4:
                    raise ValueError(f"{self.id}: truncated tensor data")
                lengths.append(count)
                stream.seek(count * 4, 1)
                offset += count * 4
        if not lengths:
            raise ValueError(f"{self.id}: empty weights file")
        input_size = self.sample_words
        if samples.stat().st_size != self.sample_count * input_size * 4:
            raise ValueError(f"{self.id}: sample file size does not match sample metadata")
        if self.adapter == "uci-har-v1":
            if input_size % 128:
                raise ValueError(f"{self.id}: invalid UCI-HAR input dimensions")
            channels = input_size // 128
            mixer_width = input_size // 16
            expected = {
                "mlp": [input_size * 128, 128, 128 * 64, 64, 64 * 6, 6],
                "normalized_mlp": [input_size * 64, 64, 64, 64 * 32, 32, 32, 32, 32 * 6, 6],
                "resnet": [channels * 8 * 3, 8, 8, 8 * 8, 8, 8, 8 * 8, 8, 8, 6 * 1008, 6],
                "mobilenet": [
                    channels * 8 * 3,
                    8,
                    8,
                    8 * 3,
                    8,
                    8,
                    16 * 8,
                    16,
                    16,
                    16 * 3,
                    16,
                    16,
                    16 * 16,
                    16,
                    16,
                    6 * 1952,
                    6,
                ],
                "mixer": [
                    16 * 16,
                    input_size // 16,
                    input_size // 16,
                    input_size // 16,
                    input_size // 16,
                    2 * mixer_width * mixer_width,
                    2 * mixer_width,
                    2 * mixer_width * mixer_width,
                    mixer_width,
                    6 * input_size,
                    6,
                ],
            }[self.family]
            if self.family == "mixer" and input_size % 16:
                raise ValueError(f"{self.id}: UCI-HAR Mixer input must divide into 16 tokens")
            if lengths != expected:
                raise ValueError(f"{self.id}: tensor count or shape does not match UCI-HAR adapter")
            return
        if self.adapter == "appliance-mixer-v1":
            if input_size != 24 * 8 or len(lengths) != 20:
                raise ValueError(f"{self.id}: invalid Appliance Mixer fixture dimensions")
            expected = [
                24 * 24,
                8,
                8,
                8,
                8,
                16 * 8,
                16,
                8 * 16,
                8,
                24 * 24,
                8,
                8,
                8,
                8,
                16 * 8,
                16,
                8 * 16,
                8,
                6 * 192,
                6,
            ]
            if lengths != expected:
                raise ValueError(
                    f"{self.id}: tensor count or shape does not match Appliance Mixer adapter"
                )
            return
        side = math.isqrt(input_size)
        if side * side != input_size:
            raise ValueError(f"{self.id}: image input must be square")
        channels = (side // 4) ** 2
        expected = {
            "mlp": [32 * input_size, 32, 16 * 32, 16, 10 * 16, 10],
            "normalized_mlp": [32 * input_size, 32, 32, 16 * 32, 16, 16, 16, 10 * 16, 10],
            "resnet": [72, 8, 8, 576, 8, 8, 576, 8, 8, 16 * 8 * 9, 16, 16, 160, 10],
            "mobilenet": [72, 8, 8, 72, 8, 8, 128, 16, 16, 144, 16, 16, 256, 16, 16, 160, 10],
            "mixer": [
                256,
                channels,
                channels,
                channels,
                channels,
                2 * channels * channels,
                2 * channels,
                2 * channels * channels,
                channels,
                10 * 16 * channels,
                10,
            ],
        }[self.family]
        if self.family == "mixer" and side % 4:
            raise ValueError(f"{self.id}: mixer image side must be divisible by four")
        if lengths != expected:
            raise ValueError(f"{self.id}: tensor count or shape does not match image-zoo adapter")
