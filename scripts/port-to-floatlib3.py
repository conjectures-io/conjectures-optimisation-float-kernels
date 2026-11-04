"""Port a Lean file from FloatLib 0d91825 to FloatLib 3.0.0, in place; prints what changed.

The mechanical edits only: renamed rounding modes and lemmas, the binary32 format's new name,
lemmas that dropped hypotheses, and FloatLib's status-flag operations (`dot` and `maximum` now
unfold through `map_value_dot` and `Internal.Value.maximum`). Proofs built on FloatLib internals
that 3.0.0 removed (such as the word-level `addFiniteImpl?`) need real rework.
Usage: python3 scripts/port-to-floatlib3.py Kernel.lean [more.lean ...]
"""

import re
import sys

# (pattern, replacement, flags) applied in order.
EDITS = [
    (r"log2_shiftRightJam m j hm", "log2_shiftRightJam m j", 0),
    (
        r"dotState_exact_toRat(\s*)\(destination := Fmt\) L R hLR \w+ \w+",
        r"dotState_exact_toRat\1(destination := Fmt) L R hLR",
        0,
    ),
    (
        r"^(\s*)unfold dotSpec Model\.dot$",
        r"\1unfold dotSpec\n\1rw [← Model.Reduction.map_value_dot]",
        re.M,
    ),
    (r"dotWithStatus_eq_", "dot_status_eq_", 0),
    (
        r"ExecFloat\.Binary\.format\s*\(exponentBits\s*:=\s*8\)\s*\(fractionBits\s*:=\s*23\)",
        "FloatFormat.binary32",
        0,
    ),
    (r"\.nearestEven\b", ".nearest", 0),
    (r"\.towardNegativeInfinity\b", ".down", 0),
    (r"\.towardPositiveInfinity\b", ".up", 0),
    (
        r"^(\s*)unfold Model\.maximum$",
        r"\1unfold Model.maximum Model.Internal.Value.maximum",
        re.M,
    ),
]

for path in sys.argv[1:]:
    with open(path) as f:
        original = f.read()
    text = original
    for pattern, replacement, flags in EDITS:
        text = re.sub(pattern, replacement, text, flags=flags)
    if text != original:
        with open(path, "w") as f:
            f.write(text)
        print("ported", path)
