"""The sum slot's rule is TorchLean's: lean/stage2/Stage2/Link.lean must keep building.

The gate checks `sum` against `Competition.sumSpec` (ContractTensor), which needs no TorchLean;
Link.lean proves it equals TorchLean's `reduceSumLeftSpec`. If either definition drifts, that file
stops building and this test fails. Building TorchLean needs a C compiler, so it skips without one.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
LAKE = Path.home() / ".elan" / "bin" / "lake"


@pytest.mark.slow
@pytest.mark.skipif(not LAKE.exists(), reason="no Lean toolchain - run ./setup.sh")
@pytest.mark.skipif(shutil.which("cc") is None, reason="no C compiler: TorchLean needs one")
def test_the_gate_sum_rule_is_torchleans():
    # Builds TorchLean (once) and the stage-2 library, Link.lean included.
    env = {**os.environ, "PATH": f"{LAKE.parent}:{os.environ.get('PATH', '')}"}
    out = subprocess.run(
        [str(LAKE), "build", "Stage2"],
        cwd=REPO / "lean",
        env=env,
        capture_output=True,
        text=True,
        timeout=3600,
    )
    assert out.returncode == 0, out.stdout[-3000:] + out.stderr[-3000:]
