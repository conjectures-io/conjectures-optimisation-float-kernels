"""Where a submission's Kernel.lean lands, written so a crash cannot leave half of one."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

NAME = "Kernel.lean"


def write_submission(directory: Path, kernel: bytes) -> None:
    # Write Kernel.lean via temp file and rename, so the gate never reads a partial file.
    directory.mkdir(parents=True, exist_ok=True)
    _atomic_write(directory / NAME, kernel)
    _fsync_dir(directory)


def _atomic_write(path: Path, payload: bytes) -> None:
    # Write through a temp file and rename, so a reader never sees half a file.
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            # fsync before rename, or a power loss can leave the new name on empty blocks.
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _fsync_dir(directory: Path) -> None:
    # Persist the directory entries themselves, so the renames survive a crash too.
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
