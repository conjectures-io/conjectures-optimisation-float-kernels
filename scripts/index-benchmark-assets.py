#!/usr/bin/env python3
"""Validate local benchmark fixtures and write a Hugging Face-ready asset index."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ASSETS = ROOT / "data/benchmark/assets"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--assets-dir",
        type=Path,
        default=Path(os.environ.get("TORCHLEAN_BENCHMARK_ASSETS", DEFAULT_ASSETS)),
        help="fixture bundle root (default: data/benchmark/assets)",
    )
    args = parser.parse_args()
    assets_dir = args.assets_dir if args.assets_dir.is_absolute() else ROOT / args.assets_dir
    assets_dir = assets_dir.resolve()
    if not assets_dir.is_dir():
        parser.error(f"benchmark asset directory does not exist: {assets_dir}")

    fixtures = []
    seen_ids = set()
    for manifest_path in sorted((assets_dir / "models").glob("**/manifest.json")):
        manifest = json.loads(manifest_path.read_text())
        if manifest.get("format") != "sample-addressable-f32-v1":
            parser.error(f"unsupported fixture format in {manifest_path}")
        fixture_id = manifest.get("workload_id")
        if not isinstance(fixture_id, str) or fixture_id in seen_ids:
            parser.error(f"missing or duplicate workload_id in {manifest_path}: {fixture_id!r}")
        seen_ids.add(fixture_id)
        entry = dict(manifest)
        entry["manifest_path"] = manifest_path.relative_to(assets_dir).as_posix()
        for path_key, digest_key in (
            ("weights_path", "weights_sha256"),
            ("samples_path", "samples_sha256"),
        ):
            relative = Path(manifest[path_key])
            if relative.is_absolute() or ".." in relative.parts:
                parser.error(f"asset path escapes the bundle in {manifest_path}: {relative}")
            asset_path = (assets_dir / relative).resolve()
            try:
                asset_path.relative_to(assets_dir)
            except ValueError:
                parser.error(f"asset path escapes the bundle in {manifest_path}: {relative}")
            if not asset_path.is_file():
                parser.error(f"missing asset referenced by {manifest_path}: {relative}")
            actual = sha256(asset_path)
            if actual != manifest.get(digest_key):
                parser.error(
                    f"checksum mismatch for {asset_path}: expected {manifest.get(digest_key)}, got {actual}"
                )
            entry[f"{path_key.removesuffix('_path')}_size_bytes"] = asset_path.stat().st_size
        fixtures.append(entry)

    if not fixtures:
        parser.error(f"no fixture manifests found under {assets_dir / 'models'}")
    catalog = {
        "schema_version": 1,
        "asset_format": "sample-addressable-f32-v1",
        "fixture_count": len(fixtures),
        "fixtures": fixtures,
    }
    output = assets_dir / "manifest.json"
    output.write_text(json.dumps(catalog, indent=2, sort_keys=True) + "\n")
    print(f"Indexed and checksum-verified {len(fixtures)} fixtures: {output}")


if __name__ == "__main__":
    main()
