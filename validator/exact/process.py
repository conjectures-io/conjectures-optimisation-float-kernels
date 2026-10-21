"""Supervise sandbox process trees without holding database connections."""

from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def environment():
    return {**os.environ, "PYTHONPATH": os.pathsep.join((str(ROOT), str(ROOT / "validator")))}


def terminate(proc):
    if proc.poll() is None:
        os.killpg(proc.pid, signal.SIGTERM)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()


def install_signals():
    def stop(_signum, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)


def supervised(
    command, heartbeat, timeout, interval, log_path, env_extra=None, pass_fds=(), cancelled=None
):
    with open(log_path, "wb") as log:
        proc = subprocess.Popen(
            ["setpriv", "--pdeathsig", "KILL", "--", *command],
            env=environment() | (env_extra or {}),
            cwd=ROOT,
            stdout=log,
            stderr=log,
            start_new_session=True,
            pass_fds=pass_fds,
        )
        start = time.monotonic()
        next_heartbeat = start
        try:
            while proc.poll() is None:
                if cancelled is not None and cancelled():
                    raise RuntimeError("supervisor stopping")
                if time.monotonic() - start > timeout:
                    raise TimeoutError("execution budget exhausted")
                if time.monotonic() >= next_heartbeat:
                    if not heartbeat():
                        raise RuntimeError("lease lost")
                    next_heartbeat = time.monotonic() + interval
                time.sleep(min(interval, 0.5))
            return proc.returncode
        finally:
            terminate(proc)
