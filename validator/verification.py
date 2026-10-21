"""Proof-only command: python -m verification <directory> --verdict <file>."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import time
from dataclasses import asdict
from pathlib import Path

import sandbox as build
from process import install_signals

import fingerprint


def verify(directory):
    identity = fingerprint.proof()
    verdict = build.Verdict(submission=directory.name, verifier_fingerprint=identity)
    started = time.monotonic()
    build._budget.reset()
    build.wipe(build.LAYER)
    (build.LAYER / "upper").mkdir(parents=True)
    (build.LAYER / "work").mkdir()
    try:
        build.run_stages(
            directory / "Kernel.lean",
            verdict,
            [
                ("intake", build.intake),
                ("policy", build.policy),
                ("statement", build.statement),
                ("kernel", build.kernel),
                ("axioms", build.axioms),
            ],
        )
    finally:
        build.wipe(build.LAYER)
    verdict.exhausted = build._budget.exhausted and not verdict.passed
    verdict.verifier_fingerprint_end = fingerprint.proof()
    verdict.seconds = time.monotonic() - started
    return verdict


def main():
    install_signals()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--verdict", type=Path, required=True)
    args = parser.parse_args()
    with open(build.HERE / "gate.lock", "a") as lock:
        if os.environ.get("GATE_LOCK_HELD") != "1":
            fcntl.flock(lock, fcntl.LOCK_EX)
        original = build.WORK.read_bytes() if build.WORK.exists() else None
        try:
            build.check_limits()
            build.prepare_shared()
            verdict = verify(args.directory.resolve())
            value = asdict(verdict)
            code = 0 if verdict.passed else 2 if verdict.reason.startswith("gate error:") else 1
            if value["verifier_fingerprint_end"] != value["verifier_fingerprint"]:
                code = 2
                value["passed"], value["reason"] = False, "verification identity changed"
        except Exception as exc:
            value, code = {"passed": False, "stage": "infrastructure", "reason": str(exc)}, 2
        finally:
            if original is None:
                build.WORK.unlink(missing_ok=True)
            else:
                build.WORK.write_bytes(original)
    args.verdict.parent.mkdir(parents=True, exist_ok=True)
    args.verdict.write_text(json.dumps(value, indent=2))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
