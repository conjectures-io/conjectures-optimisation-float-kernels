"""Stage 2's scalar kernels are copies of stage 1's winners; they must not drift from them."""

from __future__ import annotations

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

# (stage-1 kernel, its stage-2 copy, the copy's namespace).
COPIES = [
    ("kernels/add/tuned/Kernel.lean", "lean/stage2/Scalar/TunedAdd.lean", "TunedAdd"),
    ("kernels/mul/fast/Kernel.lean", "lean/stage2/Scalar/FastMul.lean", "FastMul"),
]


def as_copied(source: str, namespace: str) -> str:
    # The stage-1 kernel as stage 2 holds it: `Submission` renamed, nothing else.
    renames = {
        "namespace Submission\n": f"namespace {namespace}\n",
        "end Submission\n": f"end {namespace}\n",
    }
    return "".join(renames.get(line, line) for line in source.splitlines(keepends=True))


@pytest.mark.parametrize(("source", "copy", "namespace"), COPIES, ids=[c[1] for c in COPIES])
def test_the_stage2_copy_matches_its_stage1_kernel(source, copy, namespace):
    # A promoted stage-1 winner means re-copying it here; this catches a copy left behind.
    expected = as_copied((REPO / source).read_text(), namespace)
    assert (REPO / copy).read_text() == expected, f"{copy} differs from {source}; re-copy it"
