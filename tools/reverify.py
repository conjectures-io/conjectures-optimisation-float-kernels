#!/usr/bin/env python3
"""Re-stamp accepted submissions after a verifier change (see identity.py).

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

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "validator"))

import db  # noqa: E402
from db.verifier import required_fingerprint  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="requeue them for re-verification")
    args = parser.parse_args()
    fingerprint = required_fingerprint()
    store = db.connect()
    try:
        rows = store.submissions.stale(fingerprint)
        print(f"current verifier {fingerprint[:16]}: {len(rows)} stale accepted submission(s)")
        for row in rows:
            was = (row.verifier_fingerprint or "none")[:16]
            print(f"  {row.id:>6}  {row.slot:3}  {row.hotkey[:12]}…  ratio {row.ratio}  was {was}")
        if args.apply and rows:
            ids = store.submissions.reverify(fingerprint)
            print(f"requeued {len(ids)} for the gate worker: {ids}")
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
