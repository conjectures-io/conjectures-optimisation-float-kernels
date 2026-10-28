"""Run the tensor proof fixtures, including attacks with the policy deliberately bypassed."""

from __future__ import annotations

import fcntl
import sys
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parent / "validator")]
import sandbox  # noqa: E402
import verification  # noqa: E402

CASES = [
    ("incumbent", False, True, "axioms"),
    ("baseline", False, True, "axioms"),
    ("slowdepthwise", False, True, "axioms"),
    ("wrong", False, False, "statement"),
    ("sorryax", False, False, "axioms"),
    ("forge", False, False, "policy"),
    ("tamper", False, False, "policy"),
    # A global instance that rewrites the operator's bench output (1 ns) or audit count (0).
    ("fakeclock", False, False, "policy"),
    ("fakeaudit", False, False, "policy"),
    ("forge", True, False, "kernel"),
    ("tamper", True, False, "statement"),
]


def main():
    failures = []
    with open(sandbox.HERE / "gate.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        original = sandbox.WORK.read_bytes() if sandbox.WORK.exists() else None
        policy = sandbox.policy
        try:
            sandbox.check_limits()
            for fixture, bypass, passed, stage in CASES:
                sandbox.prepare_shared()
                sandbox.policy = (lambda *args: None) if bypass else policy
                verdict = verification.verify(sandbox.HERE / "kernels/exact" / fixture)
                okay = (verdict.passed, verdict.stage) == (passed, stage)
                print(
                    f"{fixture} policy_bypassed={bypass}: {verdict.stage} "
                    f"passed={verdict.passed}: {'OK' if okay else verdict.reason}"
                )
                if not okay:
                    failures.append(fixture)
        finally:
            sandbox.policy = policy
            if original is None:
                sandbox.WORK.unlink(missing_ok=True)
            else:
                sandbox.WORK.write_bytes(original)
    return bool(failures)


if __name__ == "__main__":
    raise SystemExit(main())
