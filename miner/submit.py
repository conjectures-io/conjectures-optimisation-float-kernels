"""Miner client: submit a Kernel.lean, read its verdict and report, read the leaderboard.

Talks to any API laid out like the conjectures platform's competition surface,
`/v1/competitions/<slug>/...`: a FloatLib validator, or conjectures.io once it serves FloatLib.
`floatlib` is the full entry (add, mul, sum, bmm in one file; the paid board and the default
slot), `floatlib-<slot>` each single-kernel practice board. Needs only requirements/miner.txt.
Usage in MANUAL.md, sections 12 and 15:

python miner/submit.py submit my-entry --hotkey <file> --url <api> [--slot full|add|mul|...]
python miner/submit.py status <submission id> --url <api> [--slot ...]
python miner/submit.py report <submission id> --url <api> [--slot ...]
python miner/submit.py leaderboard --url <api> [--slot ...]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import TypedDict, cast

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sig  # noqa: E402 - the miner's copy of the signing contract, pinned by test_miner_sig.py

TIMEOUT = 60
SLOTS = ("full", "add", "mul", "fma", "div", "sqrt", "sum", "bmm")


class SubmitResult(TypedDict):
    # The reply to an accepted submission.
    competition: str
    submission: int
    slot: str
    state: str
    digest: str
    # False when this exact file was already queued: the retry got the same submission.
    created: bool
    # Unclaimed registrations left; only an accepted submission spends one.
    slots_remaining: int


class StatusResult(TypedDict):
    # One submission as GET .../submissions/{id} returns it.
    id: int
    slot: str
    state: str
    submitted_at: str
    stage: str | None
    reason: str | None
    ratio: float | None
    classes: dict[str, dict[str, float]] | None


class LeaderboardEntry(TypedDict):
    # One ranked hotkey.
    rank: int
    submission: int
    ratio: float
    qualifies: bool
    hotkey: str
    # Statistical admission's outcome for this submission.
    admission: str | None


class LeaderboardResult(TypedDict):
    # GET .../leaderboard
    incumbent_sha256: str | None
    promotion_bar: float
    ranking: list[LeaderboardEntry]
    # The admitted leader, and whether this board pays it (practice boards do not).
    leader: int | None
    pays: bool


def base(url: str, slot: str) -> str:
    # Every endpoint of one slot.
    return f"{url.rstrip('/')}/v1/competitions/{sig.slug_of(slot)}"


def refused(what: str, r: requests.Response) -> str:
    # One line a miner can act on, from either refusal shape: a FloatLib validator's
    # {"detail": {"reason": ...}}, or the platform's problem body {reason_code, detail}.
    content_type = r.headers.get("content-type", "")
    if content_type.startswith(("application/json", "application/problem+json")):
        body = cast("dict[str, object]", r.json())
        code, detail = body.get("reason_code"), body.get("detail")
        if isinstance(detail, dict):
            detail = cast("dict[str, object]", detail).get("reason", detail)
        if code or detail:
            return f"{what} refused ({r.status_code}{f' {code}' if code else ''}): {detail}"
    return f"{what} refused ({r.status_code}): {r.text[:300]}"


def kernel_of(d: Path) -> bytes:
    # The one file a submission is.
    kernel = d / "Kernel.lean"
    if not kernel.is_file():
        sys.exit(f"{d} must contain Kernel.lean")
    return kernel.read_bytes()


def signed_headers(kp: sig.Keypair, *, competition: str, digest: str, timestamp: int) -> dict:
    # The three headers that authorise one submission. Headers, not form fields: a request
    # carrying them is not one a browser sends cross-site, so no page can replay a signature.
    message = sig.submit_message(
        competition=competition, digest=digest, hotkey=kp.ss58_address, timestamp=timestamp
    )
    return {
        "X-Conjectures-Hotkey": kp.ss58_address,
        "X-Conjectures-Timestamp": str(timestamp),
        "X-Conjectures-Signature": sig.sign(kp, message),
    }


def cmd_submit(args: argparse.Namespace) -> None:
    # Hash the file, sign (slug, hash, hotkey, now), upload; print the submission id.
    d, hotkey, url = cast(str, args.dir), cast(str, args.hotkey), cast(str, args.url)
    slot = cast(str, args.slot)
    lean = kernel_of(Path(d))
    kp = sig.load_keypair(hotkey)
    digest = sig.digest_of(lean)
    # Signed, and refused outside a few minutes of the receiver's clock, so it can't be replayed.
    timestamp = int(time.time())
    r = requests.post(
        f"{base(url, slot)}/submissions",
        headers=signed_headers(
            kp, competition=sig.slug_of(slot), digest=digest, timestamp=timestamp
        ),
        files={"Kernel.lean": ("Kernel.lean", lean)},
        timeout=TIMEOUT,
    )
    if r.status_code not in (200, 201):
        sys.exit(refused("submit", r))
    s = cast(SubmitResult, r.json())
    again = "" if s["created"] else " (already queued: same file, same submission)"
    print(f"submission {s['submission']} ({slot}) {s['state']}{again}  digest {digest}")
    print(
        f"{s['slots_remaining']} registration slot(s) left; "
        "one registration buys one accepted submission"
    )
    print(f"check with: submit.py status {s['submission']} --slot {slot} --url {url}")


def cmd_status(args: argparse.Namespace) -> None:
    # Print a submission's state, and its verdict once gated.
    sub_id, url, slot = cast(int, args.id), cast(str, args.url), cast(str, args.slot)
    r = requests.get(f"{base(url, slot)}/submissions/{sub_id}", timeout=TIMEOUT)
    if r.status_code != 200:
        sys.exit(refused("status", r))
    s = cast(StatusResult, r.json())
    print(f"submission {s['id']} ({s['slot']})  state {s['state']}  submitted {s['submitted_at']}")
    if s["stage"]:
        print(f"stage {s['stage']}" + (f": {s['reason']}" if s["reason"] else ""))
    if s["ratio"] is not None:
        print(f"time vs incumbent {s['ratio']:.4f}x (lower is faster)")
        for name, c in (s["classes"] or {}).items():
            print(f"  {name:9} {c['submission_ns']:7.2f} ns vs {c['incumbent_ns']:7.2f} ns")


def cmd_report(args: argparse.Namespace) -> None:
    # Print the gate's full verdict, which says exactly which stage refused and why.
    sub_id, url, slot = cast(int, args.id), cast(str, args.url), cast(str, args.slot)
    r = requests.get(f"{base(url, slot)}/submissions/{sub_id}/report", timeout=TIMEOUT)
    if r.status_code != 200:
        sys.exit(refused("report", r))
    report = cast("dict[str, str | None]", r.json())["report"]
    if report is None:
        print("no report yet: the gate has not run on this submission")
        return
    try:
        print(json.dumps(json.loads(report), indent=2))
    except ValueError:
        print(report)


def cmd_leaderboard(args: argparse.Namespace) -> None:
    # Print one slot's ranking.
    url, slot = cast(str, args.url), cast(str, args.slot)
    r = requests.get(f"{base(url, slot)}/leaderboard", timeout=TIMEOUT)
    if r.status_code != 200:
        sys.exit(refused("leaderboard", r))
    b = cast(LeaderboardResult, r.json())
    inc = (b["incumbent_sha256"] or "-")[:16]
    print(f"slot {slot}; incumbent {inc}; promotion bar {b['promotion_bar']}x")
    if not b["ranking"]:
        print("leaderboard: empty")
        return
    leader = b["leader"]
    print(f"{'rank':>4} {'ratio':>8} {'qualifies':>9} {'admission':>15}  hotkey")
    for x in b["ranking"]:
        lead = leader is not None and x["submission"] == leader
        mark = (
            ("  <- leader, paid" if b["pays"] else "  <- leader (practice board, unpaid)")
            if lead
            else ""
        )
        print(
            f"{x['rank']:>4} {x['ratio']:>8.4f} "
            f"{'yes' if x['qualifies'] else 'no':>9} {x['admission'] or '-':>15}  "
            f"{x['hotkey']}{mark}"
        )


CMDS = {
    "submit": cmd_submit,
    "status": cmd_status,
    "report": cmd_report,
    "leaderboard": cmd_leaderboard,
}


def main() -> None:
    # Dispatch a subcommand.
    ap = argparse.ArgumentParser(prog="submit.py", description=(__doc__ or "").split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        # The options every subcommand takes: where the API is, and which slot.
        p.add_argument("--url", required=True, help="API origin, e.g. http://host:9300")
        p.add_argument("--slot", default="full", choices=SLOTS, help="default full, the paid entry")

    p = sub.add_parser("submit")
    p.add_argument("dir", help="submission directory with Kernel.lean")
    p.add_argument("--hotkey", required=True, help="Bittensor hotkey file (or //Alice for tests)")
    common(p)
    for name in ("status", "report"):
        sp = sub.add_parser(name)
        sp.add_argument("id", type=int)
        common(sp)
    common(sub.add_parser("leaderboard"))
    args = ap.parse_args()
    CMDS[cast(str, args.cmd)](args)


if __name__ == "__main__":
    main()
