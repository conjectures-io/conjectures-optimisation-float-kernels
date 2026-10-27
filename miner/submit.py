"""Submit one complete TorchLean implementation to the central API; default https://conjectures.io."""

from __future__ import annotations

import argparse
import json
import shlex
import sys
import time
from pathlib import Path
from typing import cast

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sig  # noqa: E402 - the miner's copy of the signing contract, pinned by test_miner_sig.py

DEFAULT_URL = "https://conjectures.io"
TIMEOUT = 60
MAX_FILE_BYTES = 256_000


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
    with kernel.open("rb") as source:
        content = source.read(MAX_FILE_BYTES + 1)
    if not content:
        sys.exit("Kernel.lean must not be empty")
    if len(content) > MAX_FILE_BYTES:
        sys.exit(f"Kernel.lean must be at most {MAX_FILE_BYTES} bytes")
    try:
        content.decode("utf-8")
    except UnicodeDecodeError:
        sys.exit("Kernel.lean must be UTF-8")
    return content


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
    if r.status_code != 201:
        sys.exit(refused("submit", r))
    value = r.json()
    print(f"submission {value['submission']}; proof state: {value['proof_state']}; digest {digest}")
    if value.get("intake_mode") == "dev":
        print("dev intake: subnet registration was not required")
    command = shlex.join(
        [sys.executable, "miner/submit.py", "status", str(value["submission"]), "--url", url]
    )
    print(f"check with: {command}")


def cmd_status(args):
    response = requests.get(f"{base(args.url)}/submissions/{args.id}", timeout=TIMEOUT)
    if response.status_code != 200:
        sys.exit(refused("status", response))
    print(json.dumps(response.json(), indent=2))


CMDS = {"submit": cmd_submit, "status": cmd_status}


def main() -> None:
    # Dispatch a subcommand.
    ap = argparse.ArgumentParser(prog="submit.py", description=(__doc__ or "").split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        # The API origin; the competition identifier is fixed in the signing contract.
        p.add_argument("--url", default=DEFAULT_URL, help=f"API origin (default {DEFAULT_URL})")

    p = sub.add_parser("submit")
    p.add_argument("dir", help="submission directory with Kernel.lean")
    p.add_argument("--hotkey", required=True, help="Bittensor hotkey file (or //Alice for tests)")
    common(p)
    status = sub.add_parser("status", help="show proof status and recorded board evaluations")
    status.add_argument("id", type=int)
    common(status)
    args = ap.parse_args()
    try:
        CMDS[cast(str, args.cmd)](args)
    except requests.RequestException as error:
        sys.exit(f"API request failed: {error}")
    except (OSError, ValueError) as error:
        sys.exit(str(error))


if __name__ == "__main__":
    main()
