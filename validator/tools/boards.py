"""Operator disqualification. Stop the coordinator first; trial audits never mutate history."""

import argparse
import uuid
from pathlib import Path

from config import Config
from sqlalchemy import text

import db
from db.evaluations import EvaluationsDb


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    commands = parser.add_subparsers(dest="action", required=True)
    p = commands.add_parser("disqualify")
    p.add_argument("promotion_id", type=int)
    p.add_argument("--reason", required=True)
    args = parser.parse_args()
    config = Config.load(args.config)
    store = db.connect()
    try:
        with store.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as lock:
            if not lock.scalar(
                text("SELECT pg_try_advisory_lock(hashtextextended('tensor:coordinator',0))")
            ):
                raise RuntimeError("stop the coordinator before changing champion eligibility")
            repo = EvaluationsDb(store.sessions)
            repo.set_leader(uuid.uuid4().hex)
            repo.disqualify(args.promotion_id, args.reason, config)
            print(f"Disqualified promotion {args.promotion_id}; reward publication updated.")
    finally:
        store.close()


if __name__ == "__main__":
    main()
