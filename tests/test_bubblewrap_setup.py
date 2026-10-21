"""Sandbox capability checks must happen before any miner code is compiled."""

import shutil
import subprocess
from pathlib import Path

import pytest
import sandbox as gate


def installer(tmp_path, version, flags):
    project = tmp_path / "project"
    (project / "scripts").mkdir(parents=True)
    script = project / "scripts/install-bubblewrap.sh"
    shutil.copy2(Path(__file__).resolve().parents[1] / "scripts/install-bubblewrap.sh", script)
    binary = project / ".venv/bin/bwrap"
    binary.parent.mkdir(parents=True)
    binary.write_text(
        '#!/bin/sh\nif [ "$1" = --version ]; then\n'
        f"  echo 'bubblewrap {version}'\nelse\n  echo '{flags}'\nfi\n"
    )
    binary.chmod(0o755)
    return script, binary


@pytest.mark.parametrize(
    ("version", "flags"),
    [("0.9.0", ""), ("0.11.2", "--overlay-src --overlay")],
)
def test_check_refuses_old_or_incomplete_project_binary(tmp_path, version, flags):
    script, binary = installer(tmp_path, version, flags)
    before = binary.read_bytes()
    result = subprocess.run(["bash", str(script), "--check"], capture_output=True, text=True)
    assert result.returncode == 1
    assert "MISSING" in result.stdout
    assert binary.read_bytes() == before


@pytest.mark.parametrize("args", [[], ["--check"]])
def test_compatible_project_binary_is_reused_without_building(tmp_path, args):
    script, binary = installer(tmp_path, "0.11.2", "--overlay-src --overlay --ro-overlay")
    before = binary.stat().st_mtime_ns
    result = subprocess.run(["bash", str(script), *args], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "ok" in result.stdout
    assert binary.stat().st_mtime_ns == before
    assert not (binary.parent / "python").exists()  # no venv/build needed on a repeat run


def test_old_bubblewrap_is_an_infrastructure_failure_before_builds(monkeypatch):
    monkeypatch.setattr(gate, "_SANDBOX", True)
    monkeypatch.setattr(
        gate.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, "--ro-bind", ""),
    )
    with pytest.raises(RuntimeError, match="lacks overlay support"):
        gate.check_limits()


def test_missing_bubblewrap_is_an_infrastructure_failure(monkeypatch):
    monkeypatch.setattr(gate, "_SANDBOX", True)

    def missing(*args, **kwargs):
        raise FileNotFoundError("bwrap")

    monkeypatch.setattr(gate.subprocess, "run", missing)
    with pytest.raises(RuntimeError, match="bubblewrap is unavailable"):
        gate.check_limits()
