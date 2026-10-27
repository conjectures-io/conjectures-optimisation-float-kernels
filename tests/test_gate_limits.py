"""The gate's red-team limits: policy holes closed, disk caps, log pruning, no leftovers."""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path

import pytest
import sandbox as gate

HEADER = "import ContractExact\n\nnamespace Submission\n\n"
PROOF = (
    "theorem add_spec : ∀ x y : Binary32, add x y = ExecFloat.Spec.add x y :=\n"
    "  fun x y => ExecFloat.Proof.add_eq_spec x y\n"
)


@pytest.fixture(autouse=True)
def isolated_sandbox_config(monkeypatch, tmp_path):
    # These direct sandbox probes never own the worker's checkout lock.
    monkeypatch.setattr(gate, "SCRATCH", tmp_path / "sandbox-config")


def policy_of(tmp_path: Path, body: str) -> str | None:
    # The policy's reason for refusing a file with `body` between the header and the proof.
    src = tmp_path / "Kernel.lean"
    src.write_text(HEADER + body + "\n" + PROOF + "\nend Submission\n")
    return gate.policy(src, gate.Verdict(submission="probe"))


@pytest.mark.parametrize(
    "body",
    [
        "open Lean Meta Simp in\nsimproc evil (ExecFloat.add _ _) := fun _ => return .continue",
        "open Lean Meta Simp in\ndsimproc evil (ExecFloat.add _ _) := fun _ => return .continue",
        "theorem t : True := by\n  run_tac pure ()",
        "register_simp_attr evil",
        "declare_config_elab evilConfig Nat",
    ],
)
def test_build_time_meta_code_is_refused(tmp_path, body):
    # A simproc or run_tac runs the miner's own code while the gate elaborates the file.
    assert "banned name" in (policy_of(tmp_path, body) or "")


@pytest.mark.parametrize(
    ("body", "refused"),
    [
        ("set_option maxHeartbeats 0 in", True),
        ("set_option maxHeartbeats 100000000 in", True),
        ("set_option maxRecDepth 1000000 in", True),
        ("set_option maxHeartbeats 400000 in", False),
        ("set_option /- hidden -/ debug.skipKernelTC true", True),
        ("set_option -- hidden\n  debug.skipKernelTC true", True),
        ('def a : String := "/-"\nset_option maxRecDepth 600\ndef b : String := "-/"', True),
    ],
)
def test_options_are_read_through_comments_and_capped(tmp_path, body, refused):
    # No unlimited elaboration, and no option hidden from the policy behind a comment or string.
    assert (policy_of(tmp_path, body) is not None) is refused


def test_a_build_over_the_disk_cap_is_charged(tmp_path, monkeypatch):
    # The layer is measured after each build of the entry's own; past the cap it is exhausted.
    monkeypatch.setattr(gate, "WORK", tmp_path / "work" / "Submission.lean")
    monkeypatch.setattr(gate, "run", lambda cmd, timeout, **kw: (0, ""))
    monkeypatch.setattr(gate, "disk_mb", lambda path: gate._BUILD_DISK_MB + 1.0)
    gate._budget.reset()
    v = gate.Verdict(submission="fill")
    src = tmp_path / "Kernel.lean"
    src.write_text("import ContractExact\n")
    gate.run_stages(src, v, [("statement", gate.statement)])
    assert not v.passed and "disk cap" in v.reason and gate._budget.exhausted


def test_a_file_over_the_per_file_cap_is_charged(monkeypatch):
    # SIGXFSZ from prlimit's file-size limit counts as exhausting the gate, like a memory kill.
    monkeypatch.setattr(gate, "run", lambda cmd, timeout, **kw: (-signal.SIGXFSZ, ""))
    gate._budget.reset()
    code, _ = gate.own(["true"], gate._BUILD_TIMEOUT, build="read")
    assert gate.overfile(code) and gate._budget.exhausted
    assert "disk cap" in gate.errors("", code)


def test_old_per_run_files_are_pruned_and_others_kept(tmp_path):
    # Only the gate's own per-run logs and results go, and only once they are old.
    old = time.time() - (gate._KEEP_DAYS + 1) * 86400
    files = {
        "gate-old.log": old,
        "gate-new.log": time.time(),
        "selftest-old.log": old,
        "full-1-gate-old.json": old,
        "timing-study-old.json": old,
    }
    for name, mtime in files.items():
        (tmp_path / name).write_text("x")
        os.utime(tmp_path / name, (mtime, mtime))
    assert gate.prune(tmp_path, ("gate-*.log", "*-gate-*.json")) == 2
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "gate-new.log",
        "selftest-old.log",
        "timing-study-old.json",
    ]


def _sandbox_works() -> bool:
    # The real sandbox needs bwrap, user namespaces, a user systemd and prlimit on this host.
    if not shutil.which("bwrap"):
        return False
    try:
        gate.check_limits()
    except Exception:  # noqa: BLE001 - any missing piece means this host cannot run the check
        return False
    with tempfile.TemporaryDirectory(prefix="sandbox-probe-") as directory:
        original = gate.SCRATCH
        gate.SCRATCH = Path(directory) / "config"
        try:
            return gate.run(["true"], 30)[0] == 0
        except Exception:  # noqa: BLE001 - e.g. bwrap present but no Lean build tree to bind
            return False
        finally:
            gate.SCRATCH = original


@pytest.mark.skipif(not _sandbox_works(), reason="no working sandbox on this host")
def test_nothing_a_sandboxed_command_starts_outlives_it():
    # A bench that left work running would slow the incumbent's next turn: the pid namespace
    # kills every process when the command exits.
    marker = f"gate-leftover-{os.getpid()}"
    code, out = gate.run(["sh", "-c", f"exec -a {marker} sleep 60 & echo started"], 30)
    assert code == 0 and "started" in out
    time.sleep(1)
    left = subprocess.run(["pgrep", "-f", marker], capture_output=True, text=True)
    assert left.stdout.strip() == "", f"left running: {left.stdout}"


@pytest.mark.skipif(not _sandbox_works(), reason="no working sandbox on this host")
def test_a_run_cannot_leave_the_config_copy_unremovable():
    # The config copy is writable in every run, so one that locks a dir in it must not stop the
    # next run, the champion's bench included, from starting on a fresh copy.
    cfg = gate.CONFIG
    plant = f"mkdir -p {cfg}/x/y && touch {cfg}/x/y/f && chmod 000 {cfg}/x && echo planted"
    code, out = gate.run(["sh", "-c", plant], 30)
    assert code == 0 and "planted" in out
    code, out = gate.run(["sh", "-c", f"[ -e {cfg}/x ] && echo stale || echo fresh"], 30)
    assert code == 0 and "fresh" in out


@pytest.mark.skipif(not _sandbox_works(), reason="no working sandbox on this host")
def test_the_per_file_cap_bites_inside_the_real_sandbox(monkeypatch):
    # prlimit's file-size limit reaches through systemd-run and bwrap to whatever writes the file.
    monkeypatch.setattr(gate, "_FILE_MB", 1)
    code, _ = gate.run(["sh", "-c", "head -c 2097152 /dev/zero > /tmp/fill"], 30)
    assert gate.overfile(code), code
