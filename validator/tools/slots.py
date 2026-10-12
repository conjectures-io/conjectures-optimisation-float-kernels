#!/usr/bin/env python3
"""Inspect slot pauses, retry waiting challengers, or explicitly demote a champion."""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import db  # noqa: E402
from service.worker import gate_slot  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status")
    retry = commands.add_parser("retry")
    demote = commands.add_parser("demote")
    for command in (retry, demote):
        command.add_argument("--slot", required=True, choices=db.SLOTS)
    demote.add_argument("--submission-id", required=True, type=int)
    demote.add_argument("--reason", required=True)
    args = parser.parse_args()
    store = db.connect()
    try:
        if args.command == "status":
            for slot, pause in sorted(store.pauses.all().items()):
                print(
                    f"{slot}: challenger={pause.challenger_id} reference={pause.reference_id} "
                    f"sha256={pause.reference_sha256} retry={pause.next_retry_at.isoformat()} "
                    f"{pause.phase}: {pause.reason}"
                )
            return 0
        with gate_slot() as free:
            if not free:
                parser.error("gate is busy; retry after the active verification finishes")
            if args.command == "retry":
                store.pauses.retry(args.slot)
            else:
                if not args.reason.strip():
                    parser.error("a nonempty demotion reason is required")
                store.pauses.demote(args.slot, args.submission_id, args.reason)
    except ValueError as exc:
        parser.error(str(exc))
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
