"""Identity of mathematical definitions and their pinned Lean dependencies, not worker policy."""

import hashlib

from config import digest
from process import ROOT


def specification_identity():
    files = [
        ROOT / "lean" / name for name in ("lean-toolchain", "lake-manifest.json", "lakefile.lean")
    ]
    files += sorted((ROOT / "lean/contract").rglob("*.lean"))
    return digest(
        {
            str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in files
        }
    )
