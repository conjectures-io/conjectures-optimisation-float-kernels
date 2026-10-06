"""Console logging for the validator's processes: LOG_LEVEL (INFO unless set) to stderr."""

from __future__ import annotations

import os
import sys

from loguru import logger


def setup() -> None:
    # Replace loguru's DEBUG default; pm2 keeps each process's stderr in its own log file.
    logger.remove()
    logger.add(sys.stderr, level=os.environ.get("LOG_LEVEL", "INFO"))
