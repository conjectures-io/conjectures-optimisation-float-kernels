"""Explicit operator disqualification/restoration of exact sources, across every board."""

import argparse

import db
from db.exact import ExactDb
from exact.config import Config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("disqualify", "restore"))
    parser.add_argument("source_hash")
    parser.add_argument("--reason", required=True)
    parser.add_argument("--actor", required=True)
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    config = Config.load(args.config)
    store = db.connect()
    try:
        ExactDb(store.sessions).set_disqualification(
            args.source_hash, args.action == "disqualify", args.reason, args.actor, config
        )
    finally:
        store.close()


if __name__ == "__main__":
    main()
