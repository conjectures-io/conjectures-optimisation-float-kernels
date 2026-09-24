"""Self-test - every fixture submission must get its expected verdict from the real gate."""

from __future__ import annotations

import fcntl
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger

import gate

HERE = Path(__file__).resolve().parent
EXPECTED = HERE / "submissions" / "expected.tsv"


@dataclass
class Case:
    # One expected verdict: the submission, how to gate it, and what must come out.
    name: str
    mode: str
    passed: bool
    stage: str
    ratio_min: float | None
    ratio_max: float | None


def load() -> list[Case]:
    # Reads the expected verdicts; `-` means no ratio bound.
    cases = []
    for line in EXPECTED.read_text().splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        name, mode, passed, stage, lo, hi = line.split("\t")
        bound = [None if b == "-" else float(b) for b in (lo, hi)]
        cases.append(Case(name, mode, passed == "true", stage, bound[0], bound[1]))
    return cases


def check(case: Case, digest: str) -> str | None:
    # Gates one case and returns what differs from the expectation, or None.
    stages = gate.STAGES
    if case.mode == "nopolicy":
        gate.STAGES = [s for s in stages if s[0] != "policy"]
    try:
        v = gate.gate(HERE / "submissions" / case.name, digest)
    finally:
        gate.STAGES = stages
    if (v.passed, v.stage) != (case.passed, case.stage):
        return f"got passed={v.passed} stage={v.stage} ({v.reason[:120]})"
    if case.ratio_min is not None and not (case.ratio_min <= (v.ratio or 0) <= case.ratio_max):
        return f"ratio {v.ratio:.3f} outside [{case.ratio_min}, {case.ratio_max}]"
    return None


def main() -> int:
    # Runs every case under the gate's lock and exits 1 if any verdict is not the expected one.
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    logger.add(HERE / "logs" / f"selftest-{stamp}.log", level="DEBUG")
    cases = load()
    failures = []
    with open(HERE / "gate.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        digest = gate.pin()
        for case in cases:
            problem = check(case, digest)
            status = "FAIL" if problem else "ok"
            logger.info(f"[selftest] {status:4} {case.name:22} {case.mode:8} {problem or ''}")
            if problem:
                failures.append(case.name)
    logger.info(f"[selftest] {len(cases) - len(failures)}/{len(cases)} cases as expected")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
