"""Check setup routing without downloading toolchains or modifying the real venv."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.fixture
def setup_project(tmp_path):
    root = Path(__file__).resolve().parents[1]
    shutil.copy2(root / "setup.sh", tmp_path / "setup.sh")
    for directory in ("scripts", "lean", ".venv/bin", "kernels/exact/incumbent"):
        (tmp_path / directory).mkdir(parents=True)
    (tmp_path / "scripts/install-bubblewrap.sh").write_text("exit 0\n")
    (tmp_path / "scripts/fetch-pinned-deps.py").write_text("")
    (tmp_path / "lean/lean-toolchain").write_text("test-toolchain\n")
    (tmp_path / "kernels/exact/incumbent/Kernel.lean").write_text("-- test\n")
    (tmp_path / ".env.example").write_text("POSTGRES_PASSWORD=example\n")
    for name in ("python", "pip", "lake", "docker", "pm2"):
        binary = tmp_path / ".venv/bin" / name
        binary.write_text('#!/bin/sh\nprintf "%s %s\\n" "${0##*/}" "$*" >> "$SETUP_LOG"\n')
        binary.chmod(0o755)
    return tmp_path


@pytest.mark.parametrize("mode", ["--benchmark-worker", "--dev", "--service", "--chain"])
def test_setup_installs_matching_dependencies_and_builds(setup_project, mode):
    log = setup_project / "calls.log"
    result = subprocess.run(
        ["bash", str(setup_project / "setup.sh"), mode],
        env={**os.environ, "SETUP_LOG": str(log)},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    calls = log.read_text()
    if mode == "--benchmark-worker":
        assert "lake build VerifyExact benchExact\n" in calls
        assert "requirements/benchmark-worker.txt" in calls
        assert "exactAudit" not in calls
        assert "requirements/miner.txt" not in calls
        assert "requirements/service.txt" not in calls
    else:
        assert "exactAudit" in calls
    if mode == "--dev":
        assert "requirements/dev.txt" in calls
    if mode in ("--service", "--chain"):
        assert "requirements/service.txt" in calls
    assert ("requirements/chain.txt" in calls) == (mode == "--chain")
    assert (setup_project / ".env").exists() == (mode in ("--service", "--chain"))


def test_minimal_mode_rejects_conflicting_profiles(setup_project):
    result = subprocess.run(
        ["bash", str(setup_project / "setup.sh"), "--benchmark-worker", "--dev"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "minimal mode" in result.stdout
