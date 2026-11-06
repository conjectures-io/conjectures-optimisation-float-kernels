#!/usr/bin/env python3
"""Download and checksum-verify the private TorchLean benchmark asset dataset."""

from __future__ import annotations

import argparse
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPO_ID = "sagitarim/torchlean-benchmark-assets"
TOKEN_VARIABLE = "HF_TOKEN_TORCHLEAN_BENCHMARK"


def token_from_env_file(path: Path) -> str | None:
    """Read just the named token from a simple shell/.env-style file, without logging it."""
    if not path.is_file():
        return None
    for line in path.read_text().splitlines():
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        if text.startswith("export "):
            text = text[7:].lstrip()
        key, separator, value = text.partition("=")
        if separator and key.strip() == TOKEN_VARIABLE:
            value = value.strip()
            if value[:1] in {"'", '"'} and value[-1:] == value[:1]:
                value = shlex.split(value)[0] if value else ""
            else:
                value = value.split("#", 1)[0].strip()
            return value or None
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-id", default=REPO_ID)
    parser.add_argument(
        "--revision",
        default=os.environ.get("HF_TORCHLEAN_BENCHMARK_REVISION", "main"),
        help="Hub branch, tag, or commit hash (prefer an immutable commit hash)",
    )
    parser.add_argument(
        "--destination",
        type=Path,
        default=ROOT / "data/benchmark/assets",
        help="local download directory (default: data/benchmark/assets)",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=ROOT / ".env",
        help=f"file containing {TOKEN_VARIABLE} (default: .env)",
    )
    args = parser.parse_args()

    token = os.environ.get(TOKEN_VARIABLE) or token_from_env_file(args.env_file)
    if not token:
        parser.error(f"set {TOKEN_VARIABLE} in the environment or {args.env_file}")

    hf = shutil.which("hf")
    if not hf:
        parser.error("Hugging Face CLI `hf` was not found on PATH")

    destination = args.destination.expanduser()
    if not destination.is_absolute():
        destination = ROOT / destination
    destination = destination.resolve()
    destination.mkdir(parents=True, exist_ok=True)

    child_env = os.environ.copy()
    child_env["HF_TOKEN"] = token
    try:
        subprocess.run(
            [
                hf,
                "download",
                args.repo_id,
                "--repo-type",
                "dataset",
                "--revision",
                args.revision,
                "--local-dir",
                str(destination),
            ],
            cwd=ROOT,
            env=child_env,
            check=True,
        )
        subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/index-benchmark-assets.py"),
                "--assets-dir",
                str(destination),
            ],
            cwd=ROOT,
            env=child_env,
            check=True,
        )
    except FileNotFoundError as exc:
        print(f"required command not found: {exc.filename}", file=sys.stderr)
        return 2
    except subprocess.CalledProcessError as exc:
        print(f"asset download or verification failed (exit {exc.returncode})", file=sys.stderr)
        return exc.returncode or 1

    print(f"Verified TorchLean benchmark assets at {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
