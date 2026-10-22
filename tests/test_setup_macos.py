"""Exercise macOS setup orchestration without downloads or a real Lean build."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def executable(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/bash\nset -eu\n" + text)
    path.chmod(0o755)


@pytest.fixture
def project(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    shutil.copy2(ROOT / "setup-macos.sh", repo / "setup-macos.sh")
    for directory in ("lean", "requirements", "scripts", "kernels/exact/incumbent"):
        (repo / directory).mkdir(parents=True, exist_ok=True)
    (repo / "lean/lean-toolchain").write_text("leanprover/lean4:v4.34.0\n")
    (repo / "kernels/exact/incumbent/Kernel.lean").write_text("shipped exact entry\n")
    home = tmp_path / "home"
    tools = home / ".elan/bin"
    for name in ("git", "curl", "elan", "xcode-select", "xcrun"):
        executable(tools / name, "exit 0\n")
    executable(tools / "uname", "echo Darwin\n")
    executable(tools / "sysctl", "echo 245760\n")
    executable(
        tools / "lake",
        'printf "lake %s\\n" "$*" >> "$RUN_LOG"\n'
        'printf "files=%s\\n" "$(ulimit -Sn)" >> "$RUN_LOG"\n',
    )
    python = """case "$1" in
  -c) exit 0 ;;
  --version) echo 'Python 3.12.0' ;;
  *) printf 'python %s\\n' "$*" >> "$RUN_LOG" ;;
esac
"""
    executable(tools / "python3.12", python)
    executable(repo / ".venv/bin/python", python)
    env = os.environ | {
        "HOME": str(home),
        "PYTHON": str(tools / "python3.12"),
        "RUN_LOG": str(tmp_path / "commands"),
    }
    return repo, tools, env


def run(project, *args):
    repo, _, env = project
    return subprocess.run(
        ["bash", str(repo / "setup-macos.sh"), *args],
        cwd=repo,
        env=env,
        text=True,
        capture_output=True,
    )


def test_check_does_not_install_or_create_work_source(project):
    repo, _, env = project
    result = run(project, "--check")
    assert result.returncode == 0, result.stdout + result.stderr
    assert not (repo / "lean/work").exists()
    assert not Path(env["RUN_LOG"]).exists()


def test_native_build_initializes_exact_entry_and_excludes_linux_tools(project):
    repo, _, env = project
    result = run(project)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (repo / "lean/work/Submission.lean").read_text() == "shipped exact entry\n"
    commands = Path(env["RUN_LOG"]).read_text()
    assert "lake exe cache get" in commands
    assert "lake build ExactSuiteProofs VerifyExact benchExact" in commands
    assert "exactAudit" in commands
    assert "requirements/test.txt" not in commands
    assert all(tool not in commands for tool in ("bubblewrap", "taskset", "setpriv", "prlimit"))


def test_preserves_existing_submission_unless_source_selected(project):
    repo, _, _ = project
    work = repo / "lean/work/Submission.lean"
    work.parent.mkdir()
    work.write_text("my experimental kernel\n")
    result = run(project)
    assert result.returncode == 0, result.stdout + result.stderr
    assert work.read_text() == "my experimental kernel\n"
    package = repo / "custom entry"
    package.mkdir()
    (package / "Kernel.lean").write_text("selected kernel\n")
    result = run(project, "--source", str(package))
    assert result.returncode == 0, result.stdout + result.stderr
    assert work.read_text() == "selected kernel\n"


def test_dev_installs_test_and_plotting_dependencies(project):
    _, _, env = project
    result = run(project, "--dev")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "requirements/test.txt ruff==0.16.9 matplotlib numpy" in Path(env["RUN_LOG"]).read_text()


def test_old_venv_is_reported_and_preserved(project):
    repo, _, env = project
    executable(repo / ".venv/bin/python", "exit 1\n")
    result = run(project)
    assert result.returncode == 1
    assert "mv .venv .venv-python39-backup" in result.stdout
    assert (repo / ".venv/bin/python").exists()
    assert not Path(env["RUN_LOG"]).exists()


def test_rejects_linux_before_installing(project):
    _, tools, env = project
    executable(tools / "uname", "echo Linux\n")
    result = run(project)
    assert result.returncode == 1
    assert "macOS only" in result.stdout
    assert not Path(env["RUN_LOG"]).exists()


@pytest.mark.parametrize("limit", [256, 131072])
def test_build_children_inherit_sufficient_file_limit(project, limit):
    repo, _, env = project
    result = subprocess.run(
        [
            "bash",
            "-c",
            'ulimit -Sn "$1"; exec bash "$2"',
            "setup",
            str(limit),
            str(repo / "setup-macos.sh"),
        ],
        cwd=repo,
        env=env,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"files={max(limit, 65536)}" in Path(env["RUN_LOG"]).read_text()


def test_low_hard_file_limit_fails_before_installing(project):
    repo, _, env = project
    result = subprocess.run(
        [
            "bash",
            "-c",
            'ulimit -Sn 256; ulimit -Hn 512; exec bash "$1"',
            "setup",
            str(repo / "setup-macos.sh"),
        ],
        cwd=repo,
        env=env,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 1
    assert "Open-file limit is too low" in result.stdout
    assert not Path(env["RUN_LOG"]).exists()


@pytest.mark.parametrize("extra", [[], ["--check"], ["--dev"]])
def test_worker_setup_fails_before_install_or_source_changes(project, extra):
    repo, _, env = project
    result = run(project, "--benchmark-worker", *extra)
    assert result.returncode == 1
    assert "unsupported" in result.stdout
    assert "process-tree containment" in result.stdout
    assert not Path(env["RUN_LOG"]).exists()
    assert not (repo / "lean/work").exists()
