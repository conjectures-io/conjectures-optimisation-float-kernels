#!/usr/bin/env python3
"""Render reviewable Axiom v2 monitor bodies. No network calls or secret inputs."""

import argparse
import json
import re
from pathlib import Path


def render(dataset, environment, audit_host, notifier_id):
    values = {"DATASET": dataset, "ENVIRON": environment, "AUDIT_HOST": audit_host}
    for name, value in values.items():
        if not re.fullmatch(r"[A-Za-z0-9_.:@-]+", value):
            raise ValueError(f"{name} must contain only letters, digits, _, ., :, @ or -")
    if not notifier_id or notifier_id.isspace():
        raise ValueError("a notifier ID is required")
    source = Path(__file__).resolve().parents[1] / "deploy/axiom/monitors.json"
    monitors = json.loads(source.read_text())
    for monitor in monitors:
        for field in ("name", "aplQuery"):
            for name, value in values.items():
                monitor[field] = monitor[field].replace("$" + name, value)
        monitor["notifierIds"] = [notifier_id]
    return monitors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--environment", required=True)
    parser.add_argument(
        "--audit-host", required=True, help="auditor's real hostname, not its SSH alias"
    )
    parser.add_argument("--notifier-id", required=True)
    args = parser.parse_args()
    try:
        monitors = render(args.dataset, args.environment, args.audit_host, args.notifier_id)
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps(monitors, indent=2))


if __name__ == "__main__":
    main()
