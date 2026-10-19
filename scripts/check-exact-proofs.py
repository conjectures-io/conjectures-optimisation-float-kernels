#!/usr/bin/env python3
"""Build the operator proofs and reject unexpected axioms; optionally replay their modules."""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULES = ["ExactToolkit.SqrtProof", "ExactToolkit.TensorProofs", "ExactToolkit.ModelProofs"]
ALLOWED = {"propext", "Classical.choice", "Quot.sound"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--replay", action="store_true", help="Also replay with leanchecker")
    args = parser.parse_args()
    lake = shutil.which("lake") or str(Path.home() / ".elan/bin/lake")
    cwd = ROOT / "lean"
    subprocess.run([lake, "build", *MODULES], cwd=cwd, check=True)
    source = cwd / "audit/SpecProofs.lean"
    expected = set(re.findall(r"^#print axioms (\S+)", source.read_text(), flags=re.MULTILINE))
    report = subprocess.run(
        [lake, "env", "lean", str(source)], cwd=cwd, text=True, capture_output=True, check=True
    )
    print(report.stdout, end="")
    found = dict(re.findall(r"'([^']+)' depends on axioms: \[([^]]*)\]", report.stdout))
    for name in re.findall(r"'([^']+)' does not depend on any axioms", report.stdout):
        found[name] = ""
    if set(found) != expected:
        raise RuntimeError(f"missing/unexpected axiom reports: {set(found) ^ expected}")
    for name, values in found.items():
        extra = {a.strip() for a in values.split(",") if a.strip()} - ALLOWED
        if extra:
            raise RuntimeError(f"{name}: forbidden axioms {sorted(extra)}")
    if args.replay:
        for module in MODULES:
            subprocess.run([lake, "env", "leanchecker", module], cwd=cwd, check=True)
    print(f"Checked {len(expected)} theorem axiom reports; no extra axioms.")


if __name__ == "__main__":
    main()
