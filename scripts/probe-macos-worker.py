#!/usr/bin/env python3
"""Bounded Seatbelt feasibility probes using synthetic data, never candidate code.

Run on macOS: python3 scripts/probe-macos-worker.py
A successful diagnostic run does NOT authorize a benchmark worker.
"""

import json
import os
import platform
import signal
import subprocess
import tempfile
import time
from pathlib import Path

SOURCE = r"""
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/resource.h>
#include <sys/socket.h>
#include <sys/wait.h>
#include <netinet/in.h>
#include <unistd.h>
int main(int argc, char **argv) {
  if (argc < 2) return 2;
  if (!strcmp(argv[1], "read") || !strcmp(argv[1], "write")) {
    int fd = open(argv[2], !strcmp(argv[1], "read") ? O_RDONLY : O_WRONLY|O_CREAT, 0600);
    if (fd >= 0) { close(fd); return 0; }
    return (errno == EPERM || errno == EACCES) ? 10 : 11;
  }
  if (!strcmp(argv[1], "network")) {
    int fd = socket(AF_INET, SOCK_STREAM, 0);
    if (fd < 0) return (errno == EPERM || errno == EACCES) ? 10 : 11;
    struct sockaddr_in a = {.sin_family=AF_INET, .sin_port=htons(9),
                            .sin_addr.s_addr=htonl(0x7f000001)};
    int rc = connect(fd, (struct sockaddr *)&a, sizeof(a));
    int denied = rc < 0 && (errno == EPERM || errno == EACCES);
    close(fd); return denied ? 10 : 0;
  }
  if (!strcmp(argv[1], "child-read") || !strcmp(argv[1], "setsid")) {
    pid_t pid = fork(); if (pid < 0) return 12;
    if (!pid) {
      if (!strcmp(argv[1], "setsid")) _exit(setsid() < 0 ? 13 : 0);
      execl(argv[0], argv[0], "read", argv[2], NULL); _exit(14);
    }
    int status; waitpid(pid, &status, 0);
    return WIFEXITED(status) ? WEXITSTATUS(status) : 15;
  }
  if (!strcmp(argv[1], "rss")) {
    struct rlimit lim = {1048576, 1048576};
    if (setrlimit(RLIMIT_RSS, &lim)) {
      printf("setrlimit_RSS_unavailable_errno=%d\n", errno); return 0;
    }
    volatile char *p = malloc(16*1048576); if (!p) return 17;
    for (int i=0; i<16*1048576; i+=4096) p[i] = 1;
    struct rusage usage; getrusage(RUSAGE_SELF, &usage);
    printf("rss_bytes=%ld\n", usage.ru_maxrss);
    free((void *)p); return 0;
  }
  if (!strcmp(argv[1], "timeout") || !strcmp(argv[1], "detached-timeout")) {
    pid_t child = fork(); if (child < 0) return 18;
    if (!child) {
      if (!strcmp(argv[1], "detached-timeout") && setsid() < 0) _exit(19);
      sleep(1);
      int fd = open(argv[2], O_WRONLY|O_CREAT, 0600);
      if (fd >= 0) close(fd);
      _exit(0);
    }
    sleep(60); return 0;
  }
  return 2;
}
"""


def main():
    if platform.system() != "Darwin":
        raise SystemExit("macOS only; no probes launched")
    report = {"platform": platform.platform(), "worker_supported": False, "checks": {}}
    with tempfile.TemporaryDirectory(prefix="macos-worker-probe-") as name:
        root = Path(name).resolve()
        task = root / "task"
        task.mkdir()
        private = root / "synthetic-credentials"
        private.write_text("synthetic-canary-only")
        private.chmod(0o600)
        (task / "escape").symlink_to(private)
        source = task / "probe.c"
        source.write_text(SOURCE)
        binary = task / "probe"
        env = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": str(task), "TMPDIR": str(task)}
        subprocess.run(
            ["/usr/bin/cc", str(source), "-o", str(binary)],
            env=env,
            check=True,
            capture_output=True,
            timeout=30,
        )
        # Diagnostic policy only: broad runtime reads/Mach/process access. It is
        # NEVER a worker policy. The deny-default runtime variant aborted (-SIGABRT)
        # before main on the tested host, so its denials were not counted as passes.
        profile = """(version 1)
(allow default)
(deny network*)
(deny file-read* (literal %s))
(deny file-write* (require-not (subpath %s)))
""" % (json.dumps(str(private)), json.dumps(str(task)))
        prefix = ["/usr/bin/sandbox-exec", "-p", profile, str(binary)]

        def check(label, args, expected, boxed=True):
            result = subprocess.run(
                (prefix if boxed else [str(binary)]) + args,
                cwd=task,
                env=env,
                capture_output=True,
                text=True,
                timeout=5,
            )
            report["checks"][label] = {
                "exit": result.returncode,
                "expected": expected,
                "passed": result.returncode == expected,
                "stdout": result.stdout.strip(),
            }
            if result.returncode != expected:
                report["checks"][label]["stderr"] = result.stderr[-1000:]

        check("canary_read_control", ["read", str(private)], 0, False)
        check("private_read_denied", ["read", str(private)], 10)
        check("symlink_read_denied", ["read", str(task / "escape")], 10)
        check("private_write_denied", ["write", str(private)], 10)
        check("outside_create_denied", ["write", str(root / "outside")], 10)
        check("task_write_allowed", ["write", str(task / "output")], 0)
        check("network_control", ["network"], 0, False)
        check("network_denied", ["network"], 10)
        check("child_exec_inherits_denial", ["child-read", str(private)], 10)
        check("child_can_escape_process_group", ["setsid"], 0)
        check("rss_control", ["rss"], 0, False)
        check("rss_cap_not_established", ["rss"], 0)
        for mode, expected_marker in (("timeout", False), ("detached-timeout", True)):
            marker = task / (mode + "-marker")
            started = time.monotonic()
            proc = subprocess.Popen(
                prefix + [mode, str(marker)],
                cwd=task,
                env=env,
                start_new_session=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                proc.wait(timeout=0.5)
                timed_out = False
            except subprocess.TimeoutExpired:
                timed_out = True
            finally:
                if proc.poll() is None:
                    os.killpg(proc.pid, signal.SIGKILL)
                proc.wait(timeout=5)
            # The synthetic detached child exits after one second, even if containment fails.
            time.sleep(0.8)
            report["checks"][mode + "_process_group_cleanup"] = {
                "passed": timed_out
                and proc.returncode == -signal.SIGKILL
                and marker.exists() == expected_marker,
                "escaped_child_wrote_marker": marker.exists(),
                "seconds": round(time.monotonic() - started, 2),
            }
    print(json.dumps(report, indent=2))
    return 0 if all(x["passed"] for x in report["checks"].values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
