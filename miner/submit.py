"""Miner client: submit a Kernel.lean, read its verdict and the leaderboard (usage in MANUAL.md)."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import NotRequired, TypedDict, cast

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "validator"))
from service import sig  # noqa: E402 - shared with the service so both hash and sign identically

TIMEOUT = 60


class SubmitResult(TypedDict):
    # The service's reply to an accepted POST /submit.
    submission: int
    state: str
    digest: str
    # Unclaimed registrations left; absent from pre-entitlement services, so read with .get().
    slots_remaining: NotRequired[int]


class StatusResult(TypedDict):
    # One submission as GET /submissions/{id} returns it.
    id: int
    state: str
    submitted_at: str
    # The competition slot; absent from pre-slot services, so read with .get().
    slot: NotRequired[str]
    stage: str | None
    reason: str | None
    ratio: float | None
    classes: dict[str, dict[str, float]] | None


class LeaderboardEntry(TypedDict):
    # One ranked hotkey.
    rank: int
    ratio: float
    qualifies: bool
    hotkey: str


class LeaderboardResult(TypedDict):
    # GET /leaderboard?slot=...
    incumbent_sha256: str | None
    promotion_bar: float
    ranking: list[LeaderboardEntry]


def kernel_of(d: Path) -> bytes:
    # The one file a submission is.
    kernel = d / "Kernel.lean"
    if not kernel.is_file():
        sys.exit(f"{d} must contain Kernel.lean")
    return kernel.read_bytes()


def cmd_submit(args: argparse.Namespace) -> None:
    # Hash the file, sign (slot, hash, hotkey, now), upload; print the submission id.
    d, hotkey, url = cast(str, args.dir), cast(str, args.hotkey), cast(str, args.url)
    slot = cast(str, args.slot)
    lean = kernel_of(Path(d))
    kp = sig.load_keypair(hotkey)
    digest = sig.digest_of(lean)
    # Signed, and refused outside a few minutes of the service's clock, so it can't be replayed.
    timestamp = int(time.time())
    r = requests.post(
        f"{url}/submit",
        data={
            "hotkey": kp.ss58_address,
            "signature": sig.sign(kp, sig.submit_message(digest, kp.ss58_address, timestamp, slot)),
            "timestamp": timestamp,
            "slot": slot,
        },
        files={"Kernel.lean": ("Kernel.lean", lean)},
        timeout=TIMEOUT,
    )
    if r.status_code != 200:
        detail: object = r.text
        if r.headers.get("content-type", "").startswith("application/json"):
            detail = cast("dict[str, object]", r.json()).get("detail", r.text)
        reason: object = detail
        if isinstance(detail, dict):
            nested = cast("dict[str, object]", detail)
            reason = nested.get("reason", nested)
        sys.exit(f"submit refused ({r.status_code}): {reason}")
    s = cast(SubmitResult, r.json())
    print(f"submission {s['submission']} ({slot}) {s['state']}  digest {digest}")
    if (left := s.get("slots_remaining")) is not None:
        print(f"{left} registration slot(s) left; one registration buys one accepted submission")
    print(f"check with: submit.py status {s['submission']} --url {url}")


def cmd_status(args: argparse.Namespace) -> None:
    # Print a submission's state, and its verdict once gated.
    sub_id, url = cast(int, args.id), cast(str, args.url)
    r = requests.get(f"{url}/submissions/{sub_id}", timeout=TIMEOUT)
    if r.status_code != 200:
        sys.exit(f"{r.status_code}: {r.text}")
    s = cast(StatusResult, r.json())
    slot = s.get("slot", "add")
    print(f"submission {s['id']} ({slot})  state {s['state']}  submitted {s['submitted_at']}")
    if s["stage"]:
        print(f"stage {s['stage']}" + (f": {s['reason']}" if s["reason"] else ""))
    if s["ratio"] is not None:
        print(f"time vs incumbent {s['ratio']:.4f}x (lower is faster)")
        for name, c in (s["classes"] or {}).items():
            print(f"  {name:9} {c['submission_ns']:7.2f} ns vs {c['incumbent_ns']:7.2f} ns")


def cmd_leaderboard(args: argparse.Namespace) -> None:
    # Print one slot's ranking.
    url, slot = cast(str, args.url), cast(str, args.slot)
    r = requests.get(f"{url}/leaderboard", params={"slot": slot}, timeout=TIMEOUT)
    if r.status_code != 200:
        sys.exit(f"{r.status_code}: {r.text}")
    b = cast(LeaderboardResult, r.json())
    inc = (b["incumbent_sha256"] or "-")[:16]
    print(f"slot {slot}; incumbent {inc}; promotion bar {b['promotion_bar']}x")
    if not b["ranking"]:
        print("leaderboard: empty")
        return
    print(f"{'rank':>4} {'ratio':>8} {'qualifies':>9}  hotkey")
    for x in b["ranking"]:
        print(
            f"{x['rank']:>4} {x['ratio']:>8.4f} "
            f"{'yes' if x['qualifies'] else 'no':>9}  {x['hotkey']}"
        )


CMDS = {"submit": cmd_submit, "status": cmd_status, "leaderboard": cmd_leaderboard}


def main() -> None:
    # Dispatch a subcommand.
    ap = argparse.ArgumentParser(prog="submit.py", description=(__doc__ or "").split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("submit")
    p.add_argument("dir", help="submission directory with Kernel.lean")
    p.add_argument("--hotkey", required=True, help="Bittensor hotkey file (or //Alice for tests)")
    p.add_argument("--url", required=True, help="service URL, e.g. http://host:9300")
    p.add_argument("--slot", default="add", choices=["add", "mul", "fma"], help="default add")
    st = sub.add_parser("status")
    st.add_argument("id", type=int)
    st.add_argument("--url", required=True)
    lb = sub.add_parser("leaderboard")
    lb.add_argument("--url", required=True)
    lb.add_argument("--slot", default="add", choices=["add", "mul", "fma"])
    args = ap.parse_args()
    CMDS[cast(str, args.cmd)](args)


if __name__ == "__main__":
    main()
