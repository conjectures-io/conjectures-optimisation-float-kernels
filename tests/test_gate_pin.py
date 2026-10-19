"""The pinned incumbent: a promoted champion, a miner's file, is built in a throwaway layer, and
the shared build tree is refreshed from operator code before every run."""

from __future__ import annotations

import dataclasses
import shutil
from pathlib import Path

import pytest

import gate

REPO = Path(__file__).resolve().parents[1]


def toolchain_present() -> bool:
    # The real build needs lake, bwrap and a built lean/ workspace from setup.sh.
    lake = Path.home() / ".elan/bin/lake"
    built = (REPO / "lean/.lake/packages/floatlib").is_dir()
    return lake.exists() and built and shutil.which("bwrap") is not None


@pytest.mark.slow
@pytest.mark.skipif(not toolchain_present(), reason="no Lean toolchain or build - run ./setup.sh")
@pytest.mark.parametrize("kernel", ["incumbent", "fastlog"])
def test_a_champion_bench_is_built_in_its_own_layer(tmp_path, kernel):
    # The shipped file under another path rebuilds nothing in the layer; fastlog rebuilds into it.
    champion = tmp_path / "Kernel.lean"
    shutil.copy2(REPO / "kernels" / "add" / kernel / "Kernel.lean", champion)
    slot = dataclasses.replace(gate.SLOTS["add"], incumbent=champion)
    digest = gate.pin(slot)
    assert slot.pinned.is_file() and len(digest) == 64
    assert not gate.PIN_LAYER.exists()


def test_every_run_rebuilds_the_shared_tree_from_the_shipped_incumbent(tmp_path, monkeypatch):
    # The pinned binary is current (its stamp matches), yet the slot's verify and bench targets are
    # still built, trusted, from the shipped incumbent: Lake decides what is stale, not the stamp.
    slot = gate.SLOTS["exact"]
    monkeypatch.setattr(gate, "HERE", tmp_path)
    monkeypatch.setattr(gate, "WORK", tmp_path / "work" / "Submission.lean")
    monkeypatch.setattr(gate.subprocess, "run", lambda *a, **k: None)
    builds = []

    def run(cmd, timeout, build, layer=None):
        builds.append((cmd[1:], build, gate.WORK.read_text()))
        return 0, ""

    monkeypatch.setattr(gate, "run", run)
    slot.pinned.parent.mkdir(parents=True)
    slot.pinned.write_bytes(b"binary")
    inputs = gate.hashlib.sha256(b"".join(p.read_bytes() for p in slot.inputs)).hexdigest()
    slot.pinned.with_name("inputs.sha256").write_text(inputs)
    gate.pin(slot)
    shipped = (REPO / "kernels/exact/incumbent/Kernel.lean").read_text()
    assert builds == [(["build", "VerifyExact", "benchExact"], "trusted", shipped)]
    assert slot.pinned.read_bytes() == b"binary"


@pytest.mark.slow
@pytest.mark.skipif(not toolchain_present(), reason="no Lean toolchain or build - run ./setup.sh")
def test_a_stale_shared_module_is_rebuilt_before_an_entry_is_gated(tmp_path, monkeypatch):
    # A shared module's build output goes missing (an interrupted build, a cleaned tree): the run
    # rebuilds it from operator code, and an honest entry's build writes only its own modules.
    olean = gate.BUILD / "lib" / "lean" / "ExactSuite.olean"
    olean.unlink(missing_ok=True)
    digest = gate.pin(gate.SLOTS["exact"])
    assert olean.is_file()
    entry = tmp_path / "Kernel.lean"
    shutil.copy2(REPO / "kernels/exact/incumbent/Kernel.lean", entry)
    monkeypatch.setattr(gate, "_ROUNDS", 1)
    monkeypatch.setattr(gate, "_CELL_NS", 1_000_000)
    v = gate.gate(tmp_path, digest, slot="exact")
    assert v.passed and v.stage == "score", v.reason
