"""The pinned incumbent: a promoted champion, a miner's file, is built in a throwaway layer."""

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
