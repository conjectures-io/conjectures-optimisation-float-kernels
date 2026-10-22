"""Sandboxed differential audit; report only, independent of proof and reward decisions."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import shutil
import time
from dataclasses import asdict
from pathlib import Path

import sandbox as build
from process import install_signals

import fingerprint


def compare(src, verdict, cases, seed, report):
    shutil.copy2(src, build.WORK)
    code, out = build.own([build.LAKE, "build", "exactAudit"], build._BUILD_TIMEOUT, build="write")
    if code or (bad := build.foreign(build.LAYER / "upper")):
        return f"gate error: audit build failed: {build.errors(out, code) if code else bad[:5]}"
    code, out = build.own(
        [str(build.BUILD / "bin/exactAudit"), "--json", str(cases), str(seed)],
        build._BENCH_TIMEOUT,
        build="read",
        memory_mb=build._BENCH_MEMORY_MB,
    )
    lines = [line for line in out.splitlines() if line.startswith('{"cases"')]
    if code or not lines:
        return f"gate error: audit did not complete (exit {code}): {out[-300:]}"
    data = json.loads(lines[-1])
    if data.get("seed") != seed or type(data.get("cases")) is not int or data["cases"] <= 0:
        return "gate error: incomplete audit evidence"
    if type(data.get("mismatches")) is not int or not 0 <= data["mismatches"] <= data["cases"]:
        return "gate error: invalid mismatch count"
    report.update(data)
    if data["mismatches"]:
        return f"audit: {data['mismatches']} of {data['cases']} cases differ from the specification"
    return None


def main():
    install_signals()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--verdict", type=Path, required=True)
    args = parser.parse_args()
    cases = int(os.environ.get("GATE_AUDIT_CASES", "50"))
    seed = int(os.environ.get("GATE_AUDIT_SEED", "971"))
    if not 1 <= cases <= 1000 or not 0 <= seed < 2**32:
        parser.error("invalid audit coverage or seed")
    identity = fingerprint.proof()
    verdict = build.Verdict(submission=args.directory.name, verifier_fingerprint=identity)
    report = {}
    started = time.monotonic()
    with open(build.HERE / "gate.lock", "a") as lock:
        if os.environ.get("GATE_LOCK_HELD") != "1":
            fcntl.flock(lock, fcntl.LOCK_EX)
        original = build.WORK.read_bytes() if build.WORK.exists() else None
        try:
            build.check_limits()
            build.prepare_shared(audit=True)
            build._budget.reset()
            build.wipe(build.LAYER)
            (build.LAYER / "upper").mkdir(parents=True)
            (build.LAYER / "work").mkdir()
            build.run_stages(
                args.directory.resolve() / "Kernel.lean",
                verdict,
                [
                    ("intake", build.intake),
                    ("policy", build.policy),
                    ("audit", lambda src, v: compare(src, v, cases, seed, report)),
                ],
            )
            code = 0 if verdict.passed else 2 if verdict.reason.startswith("gate error:") else 1
        except Exception as exc:
            verdict.passed, verdict.stage, verdict.reason = False, "infrastructure", str(exc)
            code = 2
        finally:
            build.wipe(build.LAYER)
            if original is None:
                build.WORK.unlink(missing_ok=True)
            else:
                build.WORK.write_bytes(original)
    verdict.verifier_fingerprint_end = fingerprint.proof()
    if verdict.verifier_fingerprint_end != identity:
        verdict.passed, verdict.reason, code = False, "audit environment changed", 2
    verdict.seconds = time.monotonic() - started
    value = asdict(verdict) | {"mode": "audit", "classes": {"audit": report}}
    args.verdict.parent.mkdir(parents=True, exist_ok=True)
    args.verdict.write_text(json.dumps(value, indent=2))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
