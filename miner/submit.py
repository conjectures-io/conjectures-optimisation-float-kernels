"""Submit one complete tensor implementation to the central API; default https://conjectures.io."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import cast

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sig  # noqa: E402 - the miner's copy of the signing contract, pinned by test_miner_sig.py

DEFAULT_URL = "https://conjectures.io"
TIMEOUT = 60


def base(url):
    return f"{url.rstrip(chr(47))}/v1/competitions/{sig.COMPETITION}"


def refused(what: str, r: requests.Response) -> str:
    # One line a miner can act on, from either refusal shape: a FloatLib validator's
    # {"detail": {"reason": ...}}, or the platform's problem body {reason_code, detail}.
    content_type = r.headers.get("content-type", "")
    if content_type.startswith(("application/json", "application/problem+json")):
        body = cast("dict[str, object]", r.json())
        code, detail = body.get("reason_code"), body.get("detail")
        if isinstance(detail, dict):
            detail = cast("dict[str, object]", detail).get("reason", detail)
        if code == "DUPLICATE_SUBMISSION" and body.get("submission") is not None:
            detail = (
                f"{detail} Existing submission {body['submission']} "
                f"({body.get('state', 'unknown')})."
            )
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
    # The three headers that authorise one submission, sent as headers because no browser sends
    # those cross-site, so no page can replay a signature.
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
    lean = kernel_of(Path(d))
    kp = sig.load_keypair(hotkey)
    digest = sig.digest_of(lean)
    # Signed, and refused outside a few minutes of the receiver's clock, so it can't be replayed.
    timestamp = int(time.time())
    r = requests.post(
        f"{base(url)}/submissions",
        headers=signed_headers(kp, competition=sig.COMPETITION, digest=digest, timestamp=timestamp),
        files={"Kernel.lean": ("Kernel.lean", lean)},
        timeout=TIMEOUT,
    )
    if r.status_code not in (200, 201):
        sys.exit(refused("submit", r))
    value = r.json()
    print(f"submission {value['submission']} queued; digest {digest}")
    print(f"check with: submit.py status {value['submission']} --url {url}")


def cmd_status(args):
    response = requests.get(f"{base(args.url)}/submissions/{args.id}", timeout=TIMEOUT)
    if response.status_code != 200:
        sys.exit(refused("status", response))
    print(json.dumps(response.json(), indent=2))


def cmd_report(args):
    response = requests.get(f"{base(args.url)}/submissions/{args.id}/report", timeout=TIMEOUT)
    if response.status_code != 200:
        sys.exit(refused("report", response))
    print(json.dumps(response.json(), indent=2))


def cmd_leaderboard(args):
    response = requests.get(
        f"{base(args.url)}/leaderboard", params={"board": args.board}, timeout=TIMEOUT
    )
    if response.status_code != 200:
        sys.exit(refused("leaderboard", response))
    print(json.dumps(response.json(), indent=2))


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
        p.add_argument("--url", default=DEFAULT_URL, help=f"API origin (default {DEFAULT_URL})")

    p = sub.add_parser("submit")
    p.add_argument("dir", help="submission directory with Kernel.lean")
    p.add_argument("--hotkey", required=True, help="Bittensor hotkey file (or //Alice for tests)")
    common(p)
    for name in ("status", "report"):
        sp = sub.add_parser(name)
        sp.add_argument("id", type=int)
        common(sp)
    board = sub.add_parser("leaderboard")
    common(board)
    board.add_argument("--board", default="overall")
    args = ap.parse_args()
    CMDS[cast(str, args.cmd)](args)


if __name__ == "__main__":
    main()
