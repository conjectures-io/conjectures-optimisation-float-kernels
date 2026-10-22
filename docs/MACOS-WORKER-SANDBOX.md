# Native macOS benchmark worker: unsupported, fail closed

This change implements the task brief's unsupported fallback. It does **not** enable a
native macOS benchmark backend. The observed Seatbelt facilities do not yet establish
all essential worker guarantees. No candidate or public submission was run unsandboxed.
The existing native development setup remains available for operator-controlled code.

## Review location and behavior

Base: `619ead64f674985c15817db625ac1d9d831b0efa`.
Branch: `feat/macos-worker-capability-gate`.
Managed worktree: `/home/ignacy/.codex/worktrees/c12f/conjectures-optimisation-float-kernels`.
The calling checkout was not edited. Remote tests used a separate detached worktree at
`/tmp/exact-macos-worker-619ead6`; the Mac's existing checkout and experiments were preserved.

`GATE_SANDBOX_BACKEND` explicitly selects `linux-bwrap` or `macos-seatbelt`.
The default follows the host OS. Unknown values, mismatched platforms and other OSes
are errors; there is no cross-platform fallback. Linux's existing `GATE_SANDBOX=0`
development behavior remains as before; it never enables execution on macOS.

On macOS, setup with `--benchmark-worker` fails before downloads, installation or source
changes. Preflight and worker startup fail before loading credential files or connecting
to PostgreSQL. `sandbox.run`, `check_limits` and `process.supervised` reject native Mac
execution, including legacy unsandboxed settings. No jobs are claimed. Existing database
roles, credential loader and claim/lease protocol are unchanged.

Diagnostic identity hashes use actual CPU model/count/brand, architecture, macOS version
and build, Darwin kernel, compiler version, Lean toolchain, backend and policy revision.
They explicitly record `supported=false`, null CPU affinity and absent enforced limits.
They never claim Linux CPU pinning or memory-limit equivalence. Identity output alone
cannot authorize a worker. The backend implementation is included in pinned fingerprints.

## Verified on device, 2026-10-22

Host: `bwmac1-local`, Mac15,14, Apple M3 Ultra, arm64, 32 CPUs; macOS 27.0.1 build 26A434,
Darwin 27.0.0. `/usr/bin/sandbox-exec` exists; its installed manual labels it deprecated.
The standalone diagnostic compiles a small trusted C program with `/usr/bin/cc` in a
fresh temporary directory, uses a clean PATH/HOME/TMPDIR environment, and removes its
synthetic canaries and binary afterward. It reads no real credential files or SSH keys.

Run from the separate Mac worktree:

```sh
python3 scripts/probe-macos-worker.py
```

[Captured report](macos-worker-probes-2026-10-22.json): all 14 diagnostic checks passed.
A pass can demonstrate a **missing guarantee**, rather than safe worker execution:

- Synthetic mode-0600 credential read succeeds outside the sandbox and is denied inside.
- Following an allowed-directory symlink to that canary is denied.
- Writes to the canary and creation outside task scratch are denied; task-local writes work.
- Network use is denied with EPERM/EACCES, distinguishing denial from connection refusal.
- A forked, exec'd child inherits the canary read restriction.
- A child can call `setsid()`. A detached child survives timeout `killpg` and writes a marker;
  its trusted diagnostic code exits after one second. Normal group children are killed.
- Installing a one-MiB hard `RLIMIT_RSS` fails with EINVAL (22), both with and without Seatbelt.

The working diagnostic profile allows default runtime/Mach/process operations, denies network,
denies the specific synthetic credential read and denies writes outside scratch. It is **not**
a production policy, nor evidence that arbitrary home/credential reads are prevented. It is
never exposed through `sandbox.run` or passed candidate code. The initial deny-default runtime
allowlist aborted with SIGABRT before `main`, even for allowed scratch operations; those failures
were not accepted as successful denials. Minimal Mach/XPC and runtime permissions remain unresolved.

## Why execution is disabled

The existing Linux backend combines filesystem/network isolation, PID namespace lifetime,
parent-death cleanup and per-command cgroup memory/swap/task caps. Seatbelt filesystem and
network denial alone cannot replace those guarantees. The bounded `setsid` demonstration
shows why process-group cleanup alone is insufficient. A separate monitor without verified
containment would still have races and would not establish cleanup on supervisor SIGKILL.

Apple's [setrlimit manual](https://developer.apple.com/library/archive/documentation/System/Conceptual/ManPages_iPhoneOS/man2/setrlimit.2.html)
describes per-process limits and a user-wide process limit. Its legacy RSS description does
not promise a hard aggregate job-memory cap. Current on-device RSS installation also failed.
Apple directs application sandboxing toward [App Sandbox](https://developer.apple.com/documentation/security/app-sandbox);
no claim is made that app entitlements solve this native multi-process worker contract.

No shared Lean source/build writes were exposed to untrusted processes. No real credential
protection guarantee is inferred from the synthetic diagnostic. No sandboxed Lean compiler,
linker, paired benchmark, file-size/CPU-limit enforcement or crash-proof aggregate cleanup
was validated on macOS. Those remain requirements, not implementation claims. The legitimate
native C compiler was exercised; successful native C compilation is not a Lean-worker test.

## Validation and next steps

Linux targeted tests: 81 passed, 8 skipped (unavailable sandbox/PostgreSQL prerequisites).
On-device setup `./setup-macos.sh --benchmark-worker --check` exited 1 before installation.
[Startup report](macos-worker-startup-2026-10-22.json) verifies all six native refusal paths,
including both sandbox settings and startup with credential/DB access replaced by failing
canaries; all refused at the intended backend gate. Compiler identity was Apple clang 21.0.0.
Ruff check and `git diff --check` passed.

Tests cover macOS no-spawn and no-credential/DB startup paths, backend mismatch refusal,
identity changes, setup fail-closed behavior, existing Linux limits/setup, workspace locks,
timeouts and worker access. Skips are not evidence for Linux integration guarantees.

Before enabling native Mac workers, design and review a per-job containment service or
stronger isolation boundary with enforceable aggregate limits and crash-proof lifetime.
Do not reuse a shared login's RLIMIT_NPROC as a per-job task cap. Establish a deny-default
runtime/toolchain/Mach allowlist; isolate copied task source/config/build trees without
exposing credentials through symlinks, inherited descriptors or environment variables.
Then verify real Lean builds/linking and paired incumbent benchmarks, malicious descendants,
supervisor death, file/CPU/disk caps and genuine credential denial on device. Any larger
privileged-service/VM design needs its own review; this task changed no OS security settings,
installed no privileged service and did not deploy, merge or push anything.

Final Linux commands (from the managed worktree, using the existing source environment):

```sh
rtk proxy /home/ignacy/dev/const/s66v2/pg/conjectures-optimisation-float-kernels/.venv/bin/python -m pytest -q tests/test_macos_worker.py tests/test_setup_macos.py tests/test_setup_modes.py tests/test_bubblewrap_setup.py tests/test_gate_limits.py tests/test_workspace_lock.py tests/test_exact_timeouts.py tests/test_worker_access.py tests/test_fingerprint.py tests/test_exact_competition.py
rtk proxy /home/ignacy/dev/const/s66v2/pg/conjectures-optimisation-float-kernels/.venv/bin/ruff check --no-cache validator/macos_worker.py validator/sandbox.py validator/benchmark.py validator/process.py validator/tools/worker_preflight.py validator/workers/benchmark_worker.py tests/test_macos_worker.py tests/test_setup_macos.py scripts/probe-macos-worker.py fingerprint.py
rtk git diff --check
```
