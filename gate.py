"""Proof-gated FloatLib kernel gate - intake, policy, statement, axioms, score - per slot."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger

import benchmark_suite
import fingerprint as verifier

HERE = Path(__file__).resolve().parent
LIB = HERE / "lean"
BUILD = LIB / ".lake" / "build"
CONFIG = LIB / ".lake" / "config"
SCRATCH = HERE / ".sandbox" / "config"
LAYER = HERE / ".sandbox" / "layer"
# The throwaway layer a champion's bench is built in: a miner's file, so never in the shared tree.
PIN_LAYER = HERE / ".sandbox" / "pin"
# Build outputs a submission may write, by file stem: its module, the gates, the bench binaries.
OWN_STEMS = {"Submission", "Verify", "Main", "bench"}
for _op in ("Mul", "Fma", "Div", "Sqrt", "Sum", "Bmm", "Full"):
    OWN_STEMS |= {f"Verify{_op}", f"Main{_op}", f"bench{_op}"}
WORK = LIB / "work" / "Submission.lean"
# Shared inputs of every pinned binary; a change to any of them rebuilds it.
SHARED_INPUTS = [
    LIB / p
    for p in ("bench/Bench.lean", "contract/Contract.lean", "lakefile.lean")
    + ("lake-manifest.json", "lean-toolchain")
]


@dataclass(frozen=True)
class Slot:
    # One competition slot: its Lean gate module, its bench exe and main, and its incumbent.
    name: str
    verify: str
    bench: str
    main: str
    incumbent: Path
    # Files beyond the shared core that this slot's binary is built from, relative to lean/.
    extra: tuple[str, ...] = ()
    # The one module a submission to this slot may import.
    contract: str = "Contract"

    @property
    def pinned(self) -> Path:
        # The incumbent's bench binary, built once per change of its inputs.
        return HERE / "pinned" / self.name / "bench_incumbent"

    @property
    def inputs(self) -> list[Path]:
        # Everything the pinned binary is built from.
        own = [LIB / "bench" / self.main, *(LIB / name for name in self.extra)]
        return [self.incumbent, *own] + SHARED_INPUTS


KERNELS = HERE / "kernels"


def incumbent(slot: str) -> Path:
    # A slot's incumbent: kernels/<slot>/incumbent, or the file GATE_INCUMBENT (add) or
    # GATE_INCUMBENT_<SLOT> names, which the gate worker points at the slot's current champion.
    env = "GATE_INCUMBENT" if slot == "add" else f"GATE_INCUMBENT_{slot.upper()}"
    return Path(os.environ.get(env, KERNELS / slot / "incumbent" / "Kernel.lean"))


SLOTS = {
    "add": Slot("add", "Verify", "bench", "Main.lean", incumbent("add")),
    "mul": Slot("mul", "VerifyMul", "benchMul", "MainMul.lean", incumbent("mul")),
    "fma": Slot("fma", "VerifyFma", "benchFma", "MainFma.lean", incumbent("fma")),
    "div": Slot(
        "div",
        "VerifyDiv",
        "benchDiv",
        "MainDiv.lean",
        incumbent("div"),
        ("bench/BenchDivSqrt.lean",),
    ),
    "sqrt": Slot(
        "sqrt",
        "VerifySqrt",
        "benchSqrt",
        "MainSqrt.lean",
        incumbent("sqrt"),
        ("bench/BenchDivSqrt.lean",),
    ),
    # Stage 2's first tensor slot: proved against TorchLean's `reduceSumLeftSpec` (restated in
    # ContractTensor; lean/stage2/Stage2/Link.lean proves the two equal), so the gate needs no
    # TorchLean.
    "sum": Slot(
        "sum",
        "VerifySum",
        "benchSum",
        "MainSum.lean",
        incumbent("sum"),
        ("bench/BenchTensor.lean", "contract/ContractTensor.lean"),
        contract="ContractTensor",
    ),
    # The full entry, the one paid board: FloatLib's add and mul and TorchLean's sum and bmm,
    # proved together and timed together; the slots above are its single-kernel practice boards.
    "full": Slot(
        "full",
        "VerifyFull",
        "benchFull",
        "MainFull.lean",
        incumbent("full"),
        (
            "bench/BenchTensor.lean",
            "bench/BenchBmm.lean",
            "bench/BenchFull.lean",
            "bench/BenchSuite.lean",
            "contract/ContractTensor.lean",
        ),
        contract="ContractTensor",
    ),
    # Stage 2's batched matmul, proved against TorchLean's `bmmSpec` the same way.
    "bmm": Slot(
        "bmm",
        "VerifyBmm",
        "benchBmm",
        "MainBmm.lean",
        incumbent("bmm"),
        ("bench/BenchTensor.lean", "bench/BenchBmm.lean", "contract/ContractTensor.lean"),
        contract="ContractTensor",
    ),
}
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
_BENCH_CPU = os.environ.get("BENCH_CPU", "3")
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
    # One submission's outcome, logged and written as the JSON result.
    submission: str
    slot: str = "add"
    mode: str = "gate"
    stage: str = "intake"
    passed: bool = False
    reason: str = ""
    seed: int = 0
    ratio: float | None = None
    classes: dict[str, dict[str, float]] = field(default_factory=dict)
    # Per round, [incumbent ns, submission ns] summed over the timed classes: the paired evidence
    # statistical admission resamples (validator/admission.py). Under a suite, [1, that round's
    # score] instead, and `suite` holds each case's times (benchmark_suite.summarise).
    rounds: list[list[float]] = field(default_factory=list)
    suite: dict = field(default_factory=dict)
    incumbent_sha256: str = ""
    # The champion's Kernel.lean, hashed: a promotion changes it and starts a fresh board, while a
    # bench or gate hotfix (a new incumbent binary, a new fingerprint) does not.
    champion_sha256: str = ""
    # Verifier provenance; neither hash controls eligibility.
    verifier_fingerprint: str = ""
    verifier_fingerprint_end: str = ""
    seconds: float = 0.0
    # True when the entry ran out of its time budget or over a memory cap.
    exhausted: bool = False
    # The reference could not run; this does not establish whose fault it is.
    incumbent_failed: bool = False
    reference_failure_phase: str = ""
    reference_complete: bool = False


# ── Process helpers ──────────────────────────────────────────


def limits(memory_mb: int) -> list[str]:
    # The systemd scope that caps a command's memory, swap and process count; empty when lifted.
    if memory_mb <= 0:
        return []
    props = [f"MemoryMax={memory_mb}M", "MemorySwapMax=0", f"TasksMax={_TASKS_MAX}"]
    return ["systemd-run", "--user", "--scope", "--quiet"] + [a for p in props for a in ("-p", p)]


def check_limits() -> None:
    # Refuses to gate when the caps are asked for but the host cannot apply them: without this, a
    # missing user systemd would surface as every submission's build failing.
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
    argv = limits(memory_mb) + fsize + (box + ["--"] if _SANDBOX else []) + cmd
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


def pin(slot: Slot) -> str:
    # Refreshes the Lake config, rebuilds the slot's pinned incumbent when its inputs change.
    subprocess.run([LAKE, "env", "true"], cwd=LIB, check=True, capture_output=True)
    inputs = hashlib.sha256(b"".join(p.read_bytes() for p in slot.inputs)).hexdigest()
    stamp = slot.pinned.with_name("inputs.sha256")
    if not slot.pinned.exists() or not stamp.exists() or stamp.read_text() != inputs:
        logger.info(f"[pin] building {slot.bench} from {slot.incumbent}, inputs {inputs[:16]}")
        shipped = KERNELS / slot.name / "incumbent" / "Kernel.lean"
        WORK.parent.mkdir(exist_ok=True)
        # The shared build tree is refreshed from operator code only: the shipped incumbent.
        shutil.copy2(shipped, WORK)
        code, out = run([LAKE, "build", slot.bench], _BUILD_TIMEOUT, build="trusted")
        if code != 0:
            raise RuntimeError(
                f"the {slot.name} shipped incumbent bench does not build: {errors(out)}"
            )
        slot.pinned.parent.mkdir(parents=True, exist_ok=True)
        if slot.incumbent.resolve() == shipped.resolve():
            shutil.copy2(BUILD / "bin" / slot.bench, slot.pinned)
        else:
            try:
                shutil.copy2(champion_bench(slot), slot.pinned)
            finally:
                wipe(PIN_LAYER)
        stamp.write_text(inputs)
    digest = hashlib.sha256(slot.pinned.read_bytes()).hexdigest()
    logger.info(f"[pin] {slot.name} incumbent {slot.pinned} sha256={digest[:16]}")
    return digest


def champion_bench(slot: Slot) -> Path:
    # Builds a champion's bench, a miner's file, in a throwaway layer under the same checks as an
    # entry's build, and returns the binary as the layered build tree shows it.
    wipe(PIN_LAYER)
    (PIN_LAYER / "upper").mkdir(parents=True)
    (PIN_LAYER / "work").mkdir()
    shutil.copy2(slot.incumbent, WORK)
    code, out = run([LAKE, "build", slot.bench], _BUILD_TIMEOUT, build="write", layer=PIN_LAYER)
    if code != 0:
        raise RuntimeError(f"the {slot.name} champion bench does not build: {errors(out, code)}")
    if not _SANDBOX:
        return BUILD / "bin" / slot.bench
    upper = PIN_LAYER / "upper"
    if bad := foreign(upper):
        raise RuntimeError(f"the {slot.name} champion build wrote outside its modules: {bad[:5]}")
    if _BUILD_DISK_MB > 0 and disk_mb(upper) > _BUILD_DISK_MB:
        raise RuntimeError(f"the {slot.name} champion build wrote over {_BUILD_DISK_MB} MB")
    # Lake writes nothing when the champion is the file the shared tree was just built from.
    built = upper / "bin" / slot.bench
    return built if built.exists() else BUILD / "bin" / slot.bench


# ── Stages ───────────────────────────────────────────────────


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
    contract = SLOTS[v.slot].contract
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
    slot = SLOTS[v.slot]
    WORK.parent.mkdir(exist_ok=True)
    shutil.copy2(src, WORK)
    code, out = own([LAKE, "build", slot.verify], _BUILD_TIMEOUT, build="write")
    if code != 0:
        return f"the obligation does not typecheck: {errors(out, code)}"
    code, out = own([LAKE, "build", slot.bench], _BUILD_TIMEOUT, build="write")
    if code != 0:
        return f"the bench binary does not build: {errors(out, code)}"
    if bad := foreign(LAYER / "upper"):
        return f"the build wrote outside the submission's modules: {bad[:5]}"
    logger.debug(f"[statement] layer holds {sum(1 for _ in (LAYER / 'upper').rglob('*'))} entries")
    return None


def kernel(src: Path, v: Verdict) -> str | None:
    # Replays Submission and the slot's gate through the kernel instead of trusting oleans.
    for module in ("Submission", SLOTS[v.slot].verify):
        code, out = own([LAKE, "env", "leanchecker", module], _BUILD_TIMEOUT, build="read")
        if code != 0:
            limit = killed(code) or overfile(code) or code == 124
            detail = errors(out, code) if limit else out.strip()[-300:]
            return f"leanchecker rejects {module}: {detail}"
        logger.info(f"[kernel] {module} replays cleanly")
    return None


def axioms(src: Path, v: Verdict) -> str | None:
    # `#print axioms accepted` lists Lean's three and nothing else.
    gate_file = f"verify/{SLOTS[v.slot].verify}.lean"
    code, out = own([LAKE, "env", "lean", gate_file], _BUILD_TIMEOUT, build="read")
    m = re.search(r"depends on axioms: \[([^\]]*)\]", out.replace("\n", " "))
    if code != 0 or not m:
        return f"no axiom report: {errors(out, code) or out[-300:]}"
    found = {a.strip() for a in m.group(1).split(",") if a.strip()}
    logger.info(f"[axioms] {sorted(found)}")
    return f"extra axioms {sorted(found - AXIOMS)}" if found - AXIOMS else None


def bench(
    binary: Path, seed: int, build: str, plan: str | None = None
) -> dict[str, tuple[int, int]] | str:
    # One bench process: class -> (best ns, checksum), or the failure text; the submission's own
    # (built in its layer) count against its time budget, the pinned incumbent's do not.
    # A panic aborts the process, so a kernel cannot print into the output the gate parses.
    # `plan` is a suite plan's JSON (the full entry under GATE_FULL_SUITE); its cases replace the
    # legacy classes.
    env_extra = {"BENCH_SEED": str(seed), "BENCH_PAIRS": str(_PAIRS), "LEAN_ABORT_ON_PANIC": "1"}
    if plan is not None:
        env_extra["BENCH_PLAN"] = plan
    pin_cpu = ["taskset", "-c", _BENCH_CPU] if _BENCH_CPU and shutil.which("taskset") else []
    code, out = (own if build else run)(
        pin_cpu + [str(binary)],
        _BENCH_TIMEOUT,
        build=build,
        env_extra=env_extra,
        memory_mb=_BENCH_MEMORY_MB,
    )
    if killed(code):
        return f"{binary.name} killed: over the {_BENCH_MEMORY_MB} MB bench memory cap"
    if code == 124 and build:
        return f"{binary.name}: over the entry's {_BUDGET_SECONDS:.0f}s time budget"
    if code != 0:
        return f"{binary.name} exit {code}: {out[-300:]}"
    rows = [p for p in (ln.split() for ln in out.splitlines()) if _row(p)]
    return {name: (int(ns), int(sum_)) for name, _, ns, sum_ in rows}


def _row(fields: list[str]) -> bool:
    # Whether a line is a bench row, `name timed ns sum`, so a stray line is skipped, never parsed.
    return (
        len(fields) == 4 and fields[1] in ("true", "false") and all(f.isdigit() for f in fields[2:])
    )


def score(src: Path, v: Verdict) -> str | None:
    # Alternates incumbent and submission processes on a fresh seed; checksums must match per class.
    v.seed = int(os.environ.get("BENCH_SEED", secrets.randbelow(2**32)))
    logger.info(f"[score] seed={v.seed} pairs={_PAIRS} rounds={_ROUNDS} cpu={_BENCH_CPU or 'any'}")
    slot = SLOTS[v.slot]
    # The full entry under a suite: a fresh plan of cases from the seed, kept out of the verdict.
    spec = benchmark_suite.load() if v.slot == "full" else None
    plan = benchmark_suite.make_plan(spec, v.seed, _PAIRS) if spec else None
    extra = {"plan": json.dumps(plan, separators=(",", ":"))} if plan else {}
    expected = {c["name"] for c in plan["cases"]} if plan else set()
    if spec:
        logger.info(f"[score] suite {spec['version']} ({len(expected)} cases)")
    runs: dict[str, list[dict[str, tuple[int, int]]]] = {"inc": [], "sub": []}
    for r in range(_ROUNDS):
        order = [("inc", slot.pinned, ""), ("sub", BUILD / "bin" / slot.bench, "read")]
        for who, binary, build in order if r % 2 == 0 else order[::-1]:
            progress("reference-benchmark" if who == "inc" else "challenger")
            try:
                res = bench(binary, v.seed, build, **extra)
            except Exception as exc:
                if who != "inc":
                    raise
                v.incumbent_failed = True
                v.reference_failure_phase = "reference-benchmark"
                return f"gate error: the incumbent's bench failed: {exc}"
            if isinstance(res, str) and who == "inc":
                v.incumbent_failed = True
                v.reference_failure_phase = "reference-benchmark"
                return f"gate error: the incumbent's bench failed: {res}"
            if isinstance(res, str):
                return res
            if who == "inc" and (
                not res
                or not any(ns > 0 for ns, _ in res.values())
                or (runs[who] and res.keys() != runs[who][0].keys())
            ):
                v.incumbent_failed = True
                v.reference_failure_phase = "reference-benchmark"
                return "gate error: the incumbent's benchmark output is incomplete"
            if plan and (
                res.keys() != expected
                or any(ns <= 0 for c in plan["cases"] if c["timed"] for ns in [res[c["name"]][0]])
            ):
                if who != "inc":
                    return "the entry's bench did not time every case of the suite"
                v.incumbent_failed = True
                v.reference_failure_phase = "reference-benchmark"
                return "gate error: the incumbent's suite output is incomplete"
            runs[who].append(res)
            if who == "inc" and len(runs[who]) == _ROUNDS:
                v.reference_complete = True
                progress("challenger", reference_complete=True)
    inc, sub = runs["inc"], runs["sub"]
    for name, (_, want) in inc[0].items():
        if any(s.get(name, (0, None))[1] != want for s in sub):
            return f"checksum mismatch on class {name}: the submission disagrees with the incumbent"
    if plan:
        v.ratio, v.classes, v.rounds, v.suite = benchmark_suite.summarise(spec, plan, inc, sub)
        for name, c in v.classes.items():
            logger.info(
                f"[score] {name:16} inc {c['incumbent_ns']:.2f} ns  "
                f"sub {c['submission_ns']:.2f} ns  "
                f"ratio {c['ratio']:.3f}  spread {c['spread_inc']:.3f}/{c['spread_sub']:.3f}"
            )
        logger.info(f"[score] ratio {v.ratio:.3f} of the incumbent over the suite's cells")
        return None
    total_inc = total_sub = 0
    for name in inc[0]:
        best_i, best_s = min(x[name][0] for x in inc), min(x[name][0] for x in sub)
        if best_i == 0:
            continue
        spread_i = max(x[name][0] for x in inc) / best_i
        spread_s = max(x[name][0] for x in sub) / best_s
        v.classes[name] = {
            "incumbent_ns": best_i / _PAIRS,
            "submission_ns": best_s / _PAIRS,
            "ratio": best_s / best_i,
            "spread_inc": spread_i,
            "spread_sub": spread_s,
        }
        logger.info(
            f"[score] {name:9} inc {best_i / _PAIRS:.2f} ns  sub {best_s / _PAIRS:.2f} ns  "
            f"ratio {best_s / best_i:.3f}  spread {spread_i:.3f}/{spread_s:.3f}"
        )
        total_inc, total_sub = total_inc + best_i, total_sub + best_s
    v.ratio = total_sub / total_inc
    timed = list(v.classes)
    v.rounds = [
        [sum(i[name][0] for name in timed), sum(s[name][0] for name in timed)]
        for i, s in zip(inc, sub)
    ]
    logger.info(f"[score] ratio {v.ratio:.3f} of the incumbent over all timed classes")
    return None


STAGES = [
    ("intake", intake),
    ("policy", policy),
    ("statement", statement),
    ("kernel", kernel),
    ("axioms", axioms),
    ("score", score),
]
# Measure before proving: builds and scores an unproved kernel; its verdict never counts.
BENCH_STAGES = [("intake", intake), ("statement", statement), ("score", score)]


# ── Entry point ──────────────────────────────────────────────


def gate(sub_dir: Path, digest: str, mode: str = "gate", slot: str = "add") -> Verdict:
    # Gates one submission on a throwaway build layer and records provenance at both ends.
    fingerprint = verifier.current()
    v = Verdict(
        submission=sub_dir.name,
        slot=slot,
        mode=mode,
        incumbent_sha256=digest,
        champion_sha256=hashlib.sha256(SLOTS[slot].incumbent.read_bytes()).hexdigest(),
        verifier_fingerprint=fingerprint,
    )
    src, t0 = sub_dir / "Kernel.lean", time.monotonic()
    _budget.reset()
    wipe(LAYER)
    (LAYER / "upper").mkdir(parents=True)
    (LAYER / "work").mkdir()
    try:
        run_stages(src, v, BENCH_STAGES if mode == "bench" else STAGES)
    finally:
        wipe(LAYER)
    v.exhausted = _budget.exhausted and not v.passed
    v.verifier_fingerprint_end = verifier.current()
    if v.verifier_fingerprint_end != fingerprint:
        logger.warning(
            "[drift] verifier changed during the run: {} -> {}",
            fingerprint,
            v.verifier_fingerprint_end,
        )
    v.seconds = round(time.monotonic() - t0, 1)
    return v


def run_stages(src: Path, v: Verdict, stages: list) -> None:
    # Records the first rejecting stage and its reason on `v`, or marks it passed.
    for name, stage in stages:
        v.stage = name
        try:
            reason = stage(src, v)
        except Exception as exc:  # noqa: BLE001 - an infra failure is reported, never a crash
            logger.exception(f"[{name}] crashed")
            reason = f"gate error: {exc}"
        if reason:
            v.reason = reason
            logger.warning(f"[{name}] REJECTED {v.submission}: {reason}")
            break
        logger.info(f"[{name}] ok")
    else:
        v.passed = True


def log_to(name: str, stamp: str) -> None:
    # The console at LOG_LEVEL (INFO unless set), and every line, DEBUG included, in logs/.
    logger.remove()
    logger.add(sys.stderr, level=os.environ.get("LOG_LEVEL", "INFO"))
    logger.add(HERE / "logs" / f"{name}-{stamp}.log", level="DEBUG")


def main() -> int:
    # Gates each submission under one lock; exit 0 all accepted, 1 any rejected, 2 validator error.
    argv, opts = sys.argv[1:], {}
    for flag in ("--verdict", "--slot"):
        if flag in argv:
            i = argv.index(flag)
            opts[flag] = argv[i + 1] if i + 1 < len(argv) else None
            argv = argv[:i] + argv[i + 2 :]
    verdict_path = Path(opts["--verdict"]) if opts.get("--verdict") else None
    slot = opts.get("--slot", "full")
    args = [a for a in argv if a != "--bench"]
    mode = "bench" if "--bench" in argv else "gate"
    bad_verdict = "--verdict" in sys.argv and (verdict_path is None or len(args) != 1)
    if not args or bad_verdict or slot not in SLOTS:
        slots = "|".join(SLOTS)
        print(f"usage: gate.py [--bench] [--slot {slots}] <kernel dir> [...]", file=sys.stderr)
        print(
            f"       gate.py <submission dir> [--slot {slots}] --verdict <file.json>",
            file=sys.stderr,
        )
        return 2
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log_to("gate", stamp)
    if pruned := prune(HERE / "logs", ("gate-*.log",)) + prune(
        HERE / "results", ("*-gate-*.json", "*-bench-*.json")
    ):
        logger.info(f"[gate] pruned {pruned} per-run file(s) older than {_KEEP_DAYS:g} days")
    results = HERE / "results"
    results.mkdir(exist_ok=True)
    failed, broken = 0, False
    with open(HERE / "gate.lock", "w") as lock:
        # The worker takes the lock before claiming a submission and says so; any other run waits.
        if os.environ.get("GATE_LOCK_HELD") != "1":
            fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            reference = SLOTS[slot].incumbent
            reference_hash = (
                hashlib.sha256(reference.read_bytes()).hexdigest() if (reference.is_file()) else ""
            )
            progress("reference-build", champion_sha256=reference_hash, reference_complete=False)
            check_limits()
            digest = pin(SLOTS[slot])
            progress("challenger")
        except Exception:  # noqa: BLE001 - no limits or no pinned incumbent: nothing can be scored
            logger.exception("[pin] failed")
            if verdict_path is not None:
                verdict_path.write_text(
                    json.dumps(
                        {
                            "passed": False,
                            "stage": "pin",
                            "incumbent_failed": True,
                            "reference_failure_phase": "reference-build",
                            "champion_sha256": locals().get("reference_hash", ""),
                            "reason": f"reference preparation failed: {sys.exception()}",
                        }
                    )
                )
            return 2
        for arg in args:
            v = gate(Path(arg).resolve(), digest, mode, slot)
            out = results / f"{v.slot}-{v.submission}-{mode}-{stamp}.json"
            out.write_text(json.dumps(asdict(v), indent=2))
            if verdict_path is not None:
                verdict_path.write_text(json.dumps(asdict(v), indent=2))
            logger.info(
                f"[{mode}] {v.submission} slot={slot} passed={v.passed} stage={v.stage} "
                f"ratio={v.ratio}"
                + (" (UNPROVED - bench only, not a verdict)" if mode == "bench" else "")
            )
            failed += not v.passed
            # A stage that crashed is the validator's fault, not the miner's.
            broken = broken or v.reason.startswith("gate error:")
    return 2 if broken else 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
