#!/usr/bin/env python3
"""Write the paid kings' Kernel.lean and the champion each beat, to re-time them on a new bench.

Read-only. For each of the full slot's paid champions, newest first, it writes
<out>/<rank>-<id>/Kernel.lean, and <out>/<rank>-<id>/beat/Kernel.lean: the source whose sha256 the
king's own verdict recorded as its champion, an earlier promoted submission or the shipped
incumbent. It then prints the gate command that times each king against what it beat. The gate
takes the host lock, so a run waits for the worker's current verification and writes its verdict
under results/, never to the store.

    set -a; . ./.env; set +a
    .venv/bin/python validator/tools/kings.py --out /tmp/kings
"""

from __future__ import annotations

import argparse
import hashlib
import shlex
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import db  # noqa: E402
from reward import RANK_SHARES  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
SHIPPED = ROOT / "kernels" / "full" / "incumbent" / "Kernel.lean"


def export(store, out: Path, slot: str = "full") -> list[dict]:
    # Writes each paid king and the source it beat under `out`; returns what was written.
    chain = store.submissions.champions(slot)
    by_sha = {promotion.champion_sha256: row.id for row, promotion in chain}
    kings = [(row, p) for row, p in chain if p.demoted_at is None][: len(RANK_SHARES)]
    written = []
    for rank, (row, promotion) in enumerate(kings, start=1):
        kernel = store.submissions.kernel(row.id)
        if kernel is None:
            raise SystemExit(f"king {row.id} has no stored Kernel.lean")
        beat_id = by_sha.get(row.champion_sha256 or "")
        if beat_id is not None:
            beat = store.submissions.kernel(beat_id)
        elif hashlib.sha256(SHIPPED.read_bytes()).hexdigest() == row.champion_sha256:
            beat = SHIPPED.read_bytes()
        else:
            raise SystemExit(
                f"king {row.id} beat {row.champion_sha256}, which is neither a promoted "
                "submission nor the shipped incumbent"
            )
        if beat is None:
            raise SystemExit(f"king {row.id} beat submission {beat_id}, which has no stored file")
        home = out / f"{rank}-{row.id}"
        (home / "beat").mkdir(parents=True, exist_ok=True)
        (home / "Kernel.lean").write_bytes(kernel)
        (home / "beat" / "Kernel.lean").write_bytes(beat)
        written.append(
            {
                "rank": rank,
                "id": row.id,
                "hotkey": row.hotkey,
                "ratio": row.ratio,
                "beat": beat_id if beat_id is not None else "shipped",
                "dir": home,
                "champion_sha256": promotion.champion_sha256,
            }
        )
    return written


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", required=True, type=Path, help="a new or empty directory")
    args = parser.parse_args()
    if args.out.exists() and any(args.out.iterdir()):
        parser.error(f"{args.out} is not empty")
    store = db.connect()
    try:
        kings = export(store, args.out)
    finally:
        store.close()
    if not kings:
        print("no promoted champion: the shipped incumbent holds the slot")
        return 0
    for k in kings:
        print(
            f"rank {k['rank']}: submission {k['id']} ({k['hotkey'][:12]}…), ratio {k['ratio']}, "
            f"beat {k['beat']}"
        )
    print("\nTime each king against what it beat (one at a time; each waits for the gate lock):")
    for k in kings:
        home = shlex.quote(str(k["dir"]))
        print(
            f"GATE_INCUMBENT_FULL={home}/beat/Kernel.lean .venv/bin/python gate.py --slot full "
            f"{home} --verdict {home}/verdict.json"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
