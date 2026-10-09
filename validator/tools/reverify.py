#!/usr/bin/env python3
"""Re-measure accepted submissions, free: after a verifier change, or after a promotion.

A change to the gate, the contract, a Verify module, the bench or the toolchain changes the
verifier fingerprint, and older accepted verdicts stay eligible; with no --slot this lists them,
and --apply requeues them under the current verifier. A promotion changes a slot's champion and
starts a fresh board; with --slot this lists the slot's verdicts against an older champion, and
--apply requeues them against the new one. A re-accepted submission keeps the registration it
already spent.

    .venv/bin/python validator/tools/reverify.py                         stale verifier, listed
    .venv/bin/python validator/tools/reverify.py --apply                 ... and requeued
    .venv/bin/python validator/tools/reverify.py --slot full             older champion, listed
    .venv/bin/python validator/tools/reverify.py --slot full --apply     ... and requeued
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from loguru import logger

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import db  # noqa: E402
import log  # noqa: E402
from db.verifier import required_fingerprint  # noqa: E402


def main() -> int:
    # Lists what needs re-measuring; --apply requeues it for the gate worker.
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="requeue them for re-verification")
    parser.add_argument("--slot", help="re-measure this slot's verdicts against an older champion")
    args = parser.parse_args()
    log.setup()
    store = db.connect()
    try:
        if args.slot:
            champion = store.submissions.current_champion(args.slot) or "none"
            rows = store.submissions.outdated(args.slot)
            logger.info(
                f"[reverify] {args.slot} champion {champion[:16]}: "
                f"{len(rows)} accepted submission(s) against an older one"
            )
        else:
            fingerprint = required_fingerprint()
            rows = store.submissions.stale(fingerprint)
            logger.info(
                f"[reverify] current verifier {fingerprint[:16]}: "
                f"{len(rows)} stale accepted submission(s)"
            )
        for row in rows:
            was = (row.champion_sha256 if args.slot else row.verifier_fingerprint) or "none"
            logger.info(
                f"[reverify] {row.id:>6}  {row.slot:4}  {row.hotkey[:12]}…  "
                f"ratio {row.ratio}  was {was[:16]}"
            )
        if args.apply and rows:
            ids = (
                store.submissions.remeasure(args.slot)
                if args.slot
                else store.submissions.reverify(fingerprint)
            )
            logger.info(f"[reverify] requeued {len(ids)} for the gate worker: {ids}")
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
