"""Fail-closed native macOS worker gate; no candidate execution entry point."""

import platform
import subprocess

BACKEND = "macos-seatbelt"
POLICY = "macos-worker-unsupported-v1"
UNSUPPORTED = (
    "native macOS benchmark workers are unsupported: Seatbelt alone has no verified "
    "per-job aggregate memory/swap cap or parent-death/process-tree containment; "
    "RLIMIT_RSS is advisory and RLIMIT_NPROC is per user. "
    "No candidate was launched. See docs/WORKER-DATABASE-ACCESS.md"
)


def require_supported():
    raise RuntimeError(UNSUPPORTED)


def identity():
    """Actual host diagnostics, never authorization to execute."""

    def read(*args):
        return subprocess.check_output(args, text=True, timeout=10).strip()

    return {
        "architecture": platform.machine(),
        "cpu": {
            key: read("/usr/sbin/sysctl", "-n", key)
            for key in ("hw.model", "hw.ncpu", "machdep.cpu.brand_string")
        },
        "os": read("/usr/bin/sw_vers"),
        "kernel": platform.release(),
        "compiler": read("/usr/bin/cc", "--version").splitlines()[0],
        "backend": BACKEND,
        "policy": POLICY,
        "supported": False,
        "cpu_affinity": None,
        "limits": {"aggregate_memory": None, "swap": None, "process_tree": None},
    }
