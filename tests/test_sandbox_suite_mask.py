"""The private suite file is the gate's alone: every build and bench sandbox masks it."""

from __future__ import annotations

import subprocess
from pathlib import Path

import gate


def test_the_suite_file_is_masked_unless_a_tmpfs_already_hides_it(monkeypatch, tmp_path):
    suite = tmp_path / "suites" / "scoring.json"
    suite.parent.mkdir()
    suite.write_text("{}")
    link = tmp_path / "current.json"
    link.symlink_to(suite)
    monkeypatch.delenv("GATE_FULL_SUITE", raising=False)
    assert gate.suite_masks(()) == []
    monkeypatch.setenv("GATE_FULL_SUITE", str(link))
    # Both the name the operator gave and the file it points to.
    assert gate.suite_masks((Path("/nonexistent"),)) == sorted([link, suite])
    assert gate.suite_masks((tmp_path,)) == []
    monkeypatch.setenv("GATE_FULL_SUITE", str(tmp_path / "missing.json"))
    assert gate.suite_masks(()) == []


def test_every_sandboxed_command_binds_dev_null_over_the_suite(monkeypatch):
    seen: dict = {}

    def masks(hidden):
        seen["hidden"] = hidden
        return [Path("/etc/floatlib/suites/scoring.json")]

    class Done(Exception):
        pass

    def popen(argv, **kw):
        seen["argv"] = argv
        raise Done

    monkeypatch.setattr(gate, "suite_masks", masks)
    monkeypatch.setattr(gate, "_SANDBOX", True)
    monkeypatch.setattr(gate, "limits", lambda mb: [])
    monkeypatch.setattr(gate, "wipe", lambda path: None)
    monkeypatch.setattr(gate.shutil, "copytree", lambda *a, **k: None)
    monkeypatch.setattr(subprocess, "Popen", popen)
    try:
        gate.run(["true"], 1)
    except Done:
        pass
    argv = seen["argv"]
    i = argv.index("/etc/floatlib/suites/scoring.json")
    assert argv[i - 2 : i] == ["--ro-bind", "/dev/null"]
    # After the read-only root it overrides, and inside the sandbox, before the command.
    end = len(argv) - 1 - argv[::-1].index("--")
    assert argv[end + 1 :] == ["true"] and argv.index("/") < i < end
    assert Path("/tmp") in seen["hidden"] and Path.home() in seen["hidden"]
