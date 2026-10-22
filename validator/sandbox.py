"""Shared sandbox, build preparation and proof checks for tensor kernels."""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from loguru import logger

HERE = Path(__file__).resolve().parents[1]
LIB = HERE / "lean"
BUILD = LIB / ".lake" / "build"
CONFIG = LIB / ".lake" / "config"
SCRATCH = HERE / ".sandbox" / "config"
LAYER = HERE / ".sandbox" / "layer"
OWN_STEMS = {"Submission", "VerifyExact", "MainExact", "benchExact", "ExactAudit", "exactAudit"}
WORK = LIB / "work" / "Submission.lean"
SHIPPED = HERE / "kernels/exact/incumbent/Kernel.lean"
ELAN = Path.home() / ".elan"
LAKE = str(ELAN / "bin" / "lake")
# setup installs this without changing the OS package; activation is not required.
BWRAP = str(HERE / ".venv" / "bin" / "bwrap")
if not Path(BWRAP).is_file():
    BWRAP = shutil.which("bwrap") or "bwrap"

# The only environment a sandboxed build or bench inherits (the toolchain and the locale, plus the
# bench's BENCH_* via `env_extra`), never the validator's secrets.
_ENV_PASS = ("PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "TMPDIR")
_ENV_PREFIX = ("ELAN", "LEAN", "LAKE")


# What `systemd-run --user` needs to reach the user's systemd; bwrap unsets both again, so the
# sandboxed command never sees them.
_ENV_SCOPE = ("XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS")


def sandbox_env(env_extra: dict | None = None) -> dict:
    # A minimal environment: the toolchain and locale keys, the ELAN/LEAN/LAKE vars, then extras.
    base = {k: os.environ[k] for k in _ENV_PASS + _ENV_SCOPE if k in os.environ}
    base |= {k: v for k, v in os.environ.items() if k.startswith(_ENV_PREFIX)}
    if env_extra:
        base |= env_extra
    return base


_PAIRS = int(os.environ.get("BENCH_PAIRS", "500000"))
_ROUNDS = int(os.environ.get("BENCH_ROUNDS", "25"))
# 0 runs builds and benches without bwrap, for hosts that cannot create user namespaces.
_SANDBOX = os.environ.get("GATE_SANDBOX", "1") != "0"
# The core both benches run on, which halves timing noise; empty runs them unpinned.
_BENCH_CPU = os.environ.get("BENCH_CPU", "" if platform.system() == "Darwin" else "3")
_BUILD_TIMEOUT = 1800
_BENCH_TIMEOUT = 300
# The time an entry's own builds, kernel replays and bench runs may take in all; a full entry uses
# about two minutes, and one that runs out is rejected and charged, so it cannot hold the gate free.
_BUDGET_SECONDS = float(os.environ.get("GATE_BUDGET_SECONDS", "600"))
_MAX_BYTES = 256_000
# Memory and swap caps in MB for the systemd scope of every sandboxed command (builds peak near 1
# GB, benches near 20 MB), so a runaway is killed rather than swapping; 0 lifts both.
_BUILD_MEMORY_MB = int(os.environ.get("GATE_BUILD_MEMORY_MB", "4096"))
_BENCH_MEMORY_MB = int(os.environ.get("GATE_BENCH_MEMORY_MB", "512"))
# Disk caps in MB: any one file a sandboxed command writes (a bench binary is about 150 MB), and all
# an entry's builds write in total; over either is a charged rejection. 0 lifts a cap.
_FILE_MB = int(os.environ.get("GATE_FILE_MB", "512"))
_BUILD_DISK_MB = int(os.environ.get("GATE_BUILD_DISK_MB", "1024"))
# Days the gate keeps its own per-run logs and results; 0 keeps them forever.
_KEEP_DAYS = float(os.environ.get("GATE_KEEP_DAYS", "14"))
# Processes per scope; Lean spawns a handful, a fork bomb far more.
_TASKS_MAX = int(os.environ.get("GATE_TASKS_MAX", "256"))

AXIOMS = {"propext", "Classical.choice", "Quot.sound"}
# The options a submission may set, each with the highest value allowed: room for a heavy proof,
# never unlimited (0 lifts a heartbeat limit), so elaboration always ends on its own.
OPTIONS = {"maxHeartbeats": 4_000_000, "maxRecDepth": 8192, "synthInstance.maxHeartbeats": 400_000}
ATTRIBUTES = {
    "inline",
    "noinline",
    "always_inline",
    "specialize",
    "simp",
    "reducible",
    "irreducible",
}
# Names that escape the proof, or run miner code at elaboration or start-up time.
BANNED = re.compile(
    r"\b(sorry|admit|native_decide|axiom|implemented_by|extern|export|csimp|init|initialize"
    r"|builtin_initialize|run_cmd|run_elab|run_meta|macro|macro_rules|elab|elab_rules|syntax"
    r"|attribute|IO|include_str|include_bytes|includeStr|run_tac|declare_simp_like_tactic)\b"
    r"|\w*simproc\w*|\bregister_\w+|\w+_elab\b|\w*_syntax_\w*|unsafe|Unchecked|dbg|#[a-z_]+"
)


@dataclass
class Verdict:
    submission: str
    stage: str = "intake"
    passed: bool = False
    reason: str = ""
    verifier_fingerprint: str = ""
    verifier_fingerprint_end: str = ""
    seconds: float = 0.0
    exhausted: bool = False


# ── Process helpers ──────────────────────────────────────────


def limits(memory_mb: int) -> list[str]:
    # The systemd scope that caps a command's memory, swap and process count; empty when lifted.
    if memory_mb <= 0:
        return []
    props = [f"MemoryMax={memory_mb}M", "MemorySwapMax=0", f"TasksMax={_TASKS_MAX}"]
    return ["systemd-run", "--user", "--scope", "--quiet"] + [a for p in props for a in ("-p", p)]


def backend() -> str:
    """Explicit selection without cross-platform or unsandboxed fallback."""
    native = "macos-seatbelt" if platform.system() == "Darwin" else "linux-bwrap"
    selected = os.environ.get("GATE_SANDBOX_BACKEND", native)
    if selected != native or platform.system() not in {"Linux", "Darwin"}:
        raise RuntimeError(f"unsupported sandbox backend {selected!r} on {platform.system()}")
    return selected


def require_backend() -> None:
    if backend() == "macos-seatbelt":
        from macos_worker import require_supported

        require_supported()


def check_limits() -> None:
    require_backend()
    # Refuses to gate when the caps are asked for but the host cannot apply them: without this, a
    # missing user systemd would surface as every submission's build failing.
    if not shutil.which("setpriv"):
        raise RuntimeError("setpriv (util-linux) is required for subprocess parent-death cleanup")
    if _SANDBOX:
        try:
            probe = subprocess.run([BWRAP, "--help"], capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError("bubblewrap is unavailable; run ./setup.sh") from exc
        if probe.returncode or any(
            flag not in probe.stdout for flag in ("--overlay-src", "--overlay", "--ro-overlay")
        ):
            raise RuntimeError("bubblewrap lacks overlay support; run ./setup.sh")
    for memory_mb in {_BUILD_MEMORY_MB, _BENCH_MEMORY_MB}:
        if not (wrap := limits(memory_mb)):
            continue
        if not shutil.which("systemd-run"):
            raise RuntimeError("systemd-run is required for GATE_*_MEMORY_MB; 0 lifts the caps")
        probe = subprocess.run(
            wrap + ["true"], env=sandbox_env(), capture_output=True, text=True, timeout=30
        )
        if probe.returncode != 0:
            raise RuntimeError(f"systemd resource limits are unavailable: {probe.stderr.strip()}")
    if _FILE_MB > 0 and not shutil.which("prlimit"):
        raise RuntimeError("prlimit (util-linux) is required for GATE_FILE_MB; 0 lifts the cap")


def killed(code: int) -> bool:
    # SIGKILL, as a negative Popen code or a shell's 128+9: the memory cap's OOM kill.
    return code in (-signal.SIGKILL, 128 + signal.SIGKILL)


def overfile(code: int) -> bool:
    # SIGXFSZ, as a negative Popen code or a shell's 128+25: a file past the per-file disk cap.
    return code in (-signal.SIGXFSZ, 128 + signal.SIGXFSZ)


# The exit code own() reports when an entry's builds wrote more than the total disk cap.
OVER_DISK = 253


def run(
    cmd: list[str],
    timeout: float,
    build: str = "",
    env_extra: dict | None = None,
    memory_mb: int = _BUILD_MEMORY_MB,
    layer: Path = LAYER,
):
    # Runs `cmd` in bwrap with no network, a minimal environment and a `memory_mb` scope, HOME and
    # the repo root hidden behind tmpfs and only the toolchain, the Lean workspace and `pinned`
    # (absent before the first pin, hence -try) put back read-only, so no build can read a secret.
    require_backend()
    home, pinned = Path.home(), HERE / "pinned"
    box = [BWRAP, "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp"]
    box += ["--tmpfs", str(home), "--ro-bind", str(ELAN), str(ELAN)]
    box += ["--tmpfs", str(HERE), "--ro-bind", str(LIB), str(LIB)]
    box += ["--ro-bind-try", str(pinned), str(pinned)]
    # The user's runtime dir holds its D-Bus and systemd sockets.
    if (runtime := os.environ.get("XDG_RUNTIME_DIR")) and Path(runtime).is_dir():
        box += ["--tmpfs", runtime]
    box += [a for k in _ENV_SCOPE for a in ("--unsetenv", k)]
    box += ["--unshare-all", "--die-with-parent"]
    if build == "trusted":
        box += ["--bind", str(BUILD), str(BUILD)]
    elif build == "write":
        box += ["--overlay-src", str(BUILD), "--overlay", str(layer / "upper"), str(layer / "work")]
        box += [str(BUILD)]
    elif build == "read":
        box += ["--overlay-src", str(BUILD), "--overlay-src", str(LAYER / "upper")]
        box += ["--ro-overlay", str(BUILD)]
    if _SANDBOX:
        # The last run could write here and leave mode-000 dirs a bare rmtree cannot remove.
        wipe(SCRATCH)
        shutil.copytree(CONFIG, SCRATCH, symlinks=True)
        box += ["--bind", str(SCRATCH), str(CONFIG)]
    fsize = ["prlimit", f"--fsize={_FILE_MB * 2**20}", "--"] if _FILE_MB > 0 else []
    argv = (
        ["setpriv", "--pdeathsig", "KILL", "--"]
        + limits(memory_mb)
        + fsize
        + (box + ["--"] if _SANDBOX else [])
        + cmd
    )
    t0 = time.monotonic()
    with subprocess.Popen(
        argv,
        cwd=LIB,
        env=sandbox_env(env_extra),
        text=True,
        start_new_session=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    ) as proc:
        try:
            out, _ = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            out, _ = proc.communicate()
            logger.warning(f"[run] {Path(cmd[0]).name} timed out after {timeout:.0f}s")
            return 124, out
        except BaseException:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.communicate()
            raise
    logger.debug(
        f"[run] {' '.join(cmd[:3])} exit={proc.returncode} in {time.monotonic() - t0:.1f}s"
    )
    return proc.returncode, out


class Budget:
    # What is left of one entry's time budget, and whether it ran out or hit a memory cap.
    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        # A fresh budget for the next entry.
        self.left, self.exhausted = _BUDGET_SECONDS, False


_budget = Budget()


def own(cmd: list[str], cap: float, **kw) -> tuple[int, str]:
    # A command the entry controls: capped at what its budget has left, and charged against it.
    t0 = time.monotonic()
    code, out = run(cmd, max(min(cap, _budget.left), 1.0), **kw)
    _budget.left -= time.monotonic() - t0
    if kw.get("build") == "write" and _BUILD_DISK_MB > 0 and code == 0:
        if disk_mb(LAYER / "upper") > _BUILD_DISK_MB:
            code = OVER_DISK
    if code in (124, OVER_DISK) or killed(code) or overfile(code):
        _budget.exhausted = True
    return code, out


def disk_mb(path: Path) -> float:
    # What a directory tree holds on disk, in MB.
    files = (p for p in path.rglob("*") if p.is_file() and not p.is_symlink())
    return sum(p.stat().st_blocks * 512 for p in files) / 2**20


def prune(directory: Path, patterns: tuple[str, ...]) -> int:
    # Deletes this gate's own per-run files older than GATE_KEEP_DAYS, so logs cannot fill the disk.
    if _KEEP_DAYS <= 0 or not directory.is_dir():
        return 0
    cutoff = time.time() - _KEEP_DAYS * 86400
    old = [f for pat in patterns for f in directory.glob(pat) if f.stat().st_mtime < cutoff]
    for f in old:
        f.unlink(missing_ok=True)
    return len(old)


def wipe(path: Path) -> None:
    # Removes a scratch tree, including the mode-000 dirs overlayfs leaves in its work dir.
    if path.exists():
        subprocess.run(["chmod", "-R", "u+rwx", str(path)], check=False)
        shutil.rmtree(path, ignore_errors=True)


def foreign(upper: Path) -> list[str]:
    # Files the submission's build wrote or deleted outside its own modules.
    files = (p for p in upper.rglob("*") if not p.is_dir())
    return sorted(str(p.relative_to(upper)) for p in files if p.name.split(".")[0] not in OWN_STEMS)


def errors(out: str, code: int = 1) -> str:
    # The first few error lines of a Lean or Lake log, for the verdict reason.
    if killed(code):
        return f"killed: over the {_BUILD_MEMORY_MB} MB build memory cap"
    if code == 124:
        return f"over the entry's {_BUDGET_SECONDS:.0f}s time budget"
    if overfile(code):
        return f"a file over the {_FILE_MB} MB disk cap"
    if code == OVER_DISK:
        return f"the build wrote over the {_BUILD_DISK_MB} MB disk cap"
    return " | ".join(ln.strip() for ln in out.splitlines() if "error" in ln)[:600]


def progress(phase: str, **fields) -> None:
    # Written by trusted orchestration, outside the miner sandbox. Atomic for timeout readers.
    if path := os.environ.get("GATE_PROGRESS_PATH"):
        target = Path(path)
        previous = json.loads(target.read_text()) if target.exists() else {}
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps({**previous, **fields, "phase": phase}))
        temporary.replace(target)


def prepare_shared(*, native=False, audit=False):
    """Only operator code writes shared modules; entries build into an overlay."""
    subprocess.run([LAKE, "env", "true"], cwd=LIB, check=True, capture_output=True)
    WORK.parent.mkdir(exist_ok=True)
    shutil.copy2(SHIPPED, WORK)
    targets = ["VerifyExact", "benchExact"] if native else ["VerifyExact"]
    if audit:
        targets += ["ExactAuditCore", "exactAudit"]
    code, out = run([LAKE, "build", *targets], _BUILD_TIMEOUT, build="trusted")
    if code:
        raise RuntimeError(f"shared build preparation failed: {errors(out, code)}")


def intake(src: Path, v: Verdict) -> str | None:
    # The submission is one UTF-8 Lean file of bounded size; later stages read it as text.
    if not src.is_file():
        return f"{src} is missing"
    if src.stat().st_size > _MAX_BYTES:
        return f"{src.stat().st_size} bytes, over {_MAX_BYTES}"
    try:
        src.read_bytes().decode("utf-8")
    except UnicodeDecodeError as exc:
        return f"not UTF-8: invalid byte at offset {exc.start}"
    return None


def policy(src: Path, v: Verdict) -> str | None:
    # Imports the slot's contract only; no escape hatch, build-time code, or unlisted option.
    text = src.read_text()
    contract = "ContractExact"
    for line in text.splitlines():
        if re.search(r"\bimport\b", line) and line.strip() != f"import {contract}":
            return f"import other than {contract}: {line.strip()!r}"
    if m := BANNED.search(text):
        return f"banned name {m.group(0)!r} at line {text.count(chr(10), 0, m.start()) + 1}"
    if reason := options(text):
        return reason
    for attrs in re.findall(r"@\[([^\]]*)\]", text):
        for a in attrs.split(","):
            name = a.split()[-1] if a.split() else ""
            if name not in ATTRIBUTES:
                return f"attribute {a.strip()!r} is not allowed"
    return None


def options(text: str) -> str | None:
    # Every set_option names an allowed option with a value in range, read with comments removed so
    # none can hide between them; one the reader cannot account for is refused, not skipped.
    plain = re.sub(r"--[^\n]*", " ", re.sub(r"/-.*?-/", " ", text, flags=re.S))
    found = re.findall(r"\bset_option\s+(\S+)\s+(\S+)", plain)
    if len(found) != len(re.findall(r"\bset_option\b", text)):
        return "a set_option the policy cannot read (hidden in or between comments or strings)"
    for name, value in found:
        if name not in OPTIONS:
            return f"set_option {name} is not allowed"
        if not value.isdigit() or not 0 < int(value) <= OPTIONS[name]:
            return f"set_option {name} {value}: allowed values are 1 to {OPTIONS[name]}"
    return None


def statement(src: Path, v: Verdict) -> str | None:
    # The obligation typechecks and the bench links, writing only into this submission's layer.
    WORK.parent.mkdir(exist_ok=True)
    shutil.copy2(src, WORK)
    code, out = own([LAKE, "build", "VerifyExact"], _BUILD_TIMEOUT, build="write")
    if code != 0:
        return f"the obligation does not typecheck: {errors(out, code)}"
    if bad := foreign(LAYER / "upper"):
        return f"the build wrote outside the submission's modules: {bad[:5]}"
    logger.debug(f"[statement] layer holds {sum(1 for _ in (LAYER / 'upper').rglob('*'))} entries")
    return None


def kernel(src: Path, v: Verdict) -> str | None:
    # Replays Submission and the slot's gate through the kernel instead of trusting oleans.
    for module in ("Submission", "VerifyExact"):
        code, out = own([LAKE, "env", "leanchecker", module], _BUILD_TIMEOUT, build="read")
        if code != 0:
            limit = killed(code) or overfile(code) or code == 124
            detail = errors(out, code) if limit else out.strip()[-300:]
            return f"leanchecker rejects {module}: {detail}"
        logger.info(f"[kernel] {module} replays cleanly")
    return None


def axioms(src: Path, v: Verdict) -> str | None:
    # `#print axioms accepted` lists Lean's three and nothing else.
    gate_file = "verify/VerifyExact.lean"
    code, out = own([LAKE, "env", "lean", gate_file], _BUILD_TIMEOUT, build="read")
    m = re.search(r"depends on axioms: \[([^\]]*)\]", out.replace("\n", " "))
    if code != 0 or not m:
        return f"no axiom report: {errors(out, code) or out[-300:]}"
    found = {a.strip() for a in m.group(1).split(",") if a.strip()}
    logger.info(f"[axioms] {sorted(found)}")
    return f"extra axioms {sorted(found - AXIOMS)}" if found - AXIOMS else None


def run_stages(src, verdict, stages):
    for name, stage in stages:
        verdict.stage = name
        try:
            reason = stage(src, verdict)
        except Exception as exc:
            logger.exception("proof stage failed")
            reason = f"gate error: {exc}"
        if reason:
            verdict.reason = reason
            break
    else:
        verdict.passed = True
