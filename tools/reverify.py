#!/usr/bin/env python3
"""Re-stamp accepted submissions after a verifier change (see fingerprint.py).

A change to the gate, the contract, a Verify module, the bench or the toolchain changes the
verifier fingerprint, and every accepted verdict from the old one drops off the leaderboard.
This lists them, and with --apply requeues them so the gate worker re-verifies each under the
current verifier. A re-accepted submission keeps the registration it already spent.

    .venv/bin/python tools/reverify.py            list stale accepted submissions
    .venv/bin/python tools/reverify.py --apply    requeue them for the gate worker
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from loguru import logger

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "validator"))

import db  # noqa: E402
import log  # noqa: E402
from db.verifier import required_fingerprint  # noqa: E402


def main() -> int:
    # Lists accepted submissions judged by another verifier; --apply requeues them for the gate.
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="requeue them for re-verification")
    args = parser.parse_args()
    log.setup()
    fingerprint = required_fingerprint()
    store = db.connect()
    try:
        rows = store.submissions.stale(fingerprint)
        logger.info(
            f"[reverify] current verifier {fingerprint[:16]}: "
            f"{len(rows)} stale accepted submission(s)"
        )
        for row in rows:
            was = (row.verifier_fingerprint or "none")[:16]
            logger.info(
                f"[reverify] {row.id:>6}  {row.slot:4}  {row.hotkey[:12]}…  "
                f"ratio {row.ratio}  was {was}"
            )
        if args.apply and rows:
            ids = store.submissions.reverify(fingerprint)
            logger.info(f"[reverify] requeued {len(ids)} for the gate worker: {ids}")
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
