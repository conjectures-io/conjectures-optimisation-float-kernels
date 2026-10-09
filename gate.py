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

import fingerprint as verifier

HERE = Path(__file__).resolve().parent
LIB = HERE / "lean"
BUILD = LIB / ".lake" / "build"
CONFIG = LIB / ".lake" / "config"
SCRATCH = HERE / ".sandbox" / "config"
LAYER = HERE / ".sandbox" / "layer"
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
    # A slot's incumbent: kernels/<slot>/incumbent, or the winner an operator promoted (repins)
    # with GATE_INCUMBENT for add, GATE_INCUMBENT_MUL and GATE_INCUMBENT_FMA for the others.
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
# Processes per scope; Lean spawns a handful, a fork bomb far more.
_TASKS_MAX = int(os.environ.get("GATE_TASKS_MAX", "256"))

AXIOMS = {"propext", "Classical.choice", "Quot.sound"}
OPTIONS = {"maxHeartbeats", "maxRecDepth", "synthInstance.maxHeartbeats"}
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
    r"|attribute|IO|include_str|include_bytes|includeStr)\b|unsafe|Unchecked|dbg|#[a-z_]+"
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
    # statistical admission resamples (validator/admission.py).
    rounds: list[list[int]] = field(default_factory=list)
    incumbent_sha256: str = ""
    # The champion's Kernel.lean, hashed: a promotion changes it and starts a fresh board, while a
    # bench or gate hotfix (a new incumbent binary, a new fingerprint) does not.
    champion_sha256: str = ""
    # Verifier provenance; neither hash controls eligibility.
    verifier_fingerprint: str = ""
    verifier_fingerprint_end: str = ""
    seconds: float = 0.0
    # True when the entry ran out of its time budget or over a memory cap: rejected, and charged.
    exhausted: bool = False


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


def killed(code: int) -> bool:
    # SIGKILL, as a negative Popen code or a shell's 128+9: the memory cap's OOM kill.
    return code in (-signal.SIGKILL, 128 + signal.SIGKILL)


def run(
    cmd: list[str],
    timeout: float,
    build: str = "",
    env_extra: dict | None = None,
    memory_mb: int = _BUILD_MEMORY_MB,
):
    # Runs `cmd` in bwrap with no network, a minimal environment and a `memory_mb` scope, HOME and
    # the repo root hidden behind tmpfs and only the toolchain, the Lean workspace and `pinned`
    # (absent before the first pin, hence -try) put back read-only, so no build can read a secret.
    home, pinned = Path.home(), HERE / "pinned"
    box = ["bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp"]
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
        box += ["--overlay-src", str(BUILD), "--overlay", str(LAYER / "upper"), str(LAYER / "work")]
        box += [str(BUILD)]
    elif build == "read":
        box += ["--overlay-src", str(BUILD), "--overlay-src", str(LAYER / "upper")]
        box += ["--ro-overlay", str(BUILD)]
    if _SANDBOX:
        shutil.rmtree(SCRATCH, ignore_errors=True)
        shutil.copytree(CONFIG, SCRATCH, symlinks=True)
        box += ["--bind", str(SCRATCH), str(CONFIG)]
    argv = limits(memory_mb) + (box + ["--"] if _SANDBOX else []) + cmd
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
    if code == 124 or killed(code):
        _budget.exhausted = True
    return code, out


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
    return " | ".join(ln.strip() for ln in out.splitlines() if "error" in ln)[:600]


def pin(slot: Slot) -> str:
    # Refreshes the Lake config, rebuilds the slot's pinned incumbent when its inputs change.
    subprocess.run([LAKE, "env", "true"], cwd=LIB, check=True, capture_output=True)
    inputs = hashlib.sha256(b"".join(p.read_bytes() for p in slot.inputs)).hexdigest()
    stamp = slot.pinned.with_name("inputs.sha256")
    if not slot.pinned.exists() or not stamp.exists() or stamp.read_text() != inputs:
        logger.info(f"[pin] building {slot.bench} from {slot.incumbent}, inputs {inputs[:16]}")
        WORK.parent.mkdir(exist_ok=True)
        shutil.copy2(slot.incumbent, WORK)
        code, out = run([LAKE, "build", slot.bench], _BUILD_TIMEOUT, build="trusted")
        if code != 0:
            raise RuntimeError(f"the {slot.name} incumbent bench does not build: {errors(out)}")
        slot.pinned.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(BUILD / "bin" / slot.bench, slot.pinned)
        stamp.write_text(inputs)
    digest = hashlib.sha256(slot.pinned.read_bytes()).hexdigest()
    logger.info(f"[pin] {slot.name} incumbent {slot.pinned} sha256={digest[:16]}")
    return digest


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
    for opt in re.findall(r"set_option\s+([\w.]+)", text):
        if opt not in OPTIONS:
            return f"set_option {opt} is not allowed"
    for attrs in re.findall(r"@\[([^\]]*)\]", text):
        for a in attrs.split(","):
            name = a.split()[-1] if a.split() else ""
            if name not in ATTRIBUTES:
                return f"attribute {a.strip()!r} is not allowed"
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
            detail = errors(out, code) if killed(code) or code == 124 else out.strip()[-300:]
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


def bench(binary: Path, seed: int, build: str) -> dict[str, tuple[int, int]] | str:
    # One bench process: class -> (best ns, checksum), or the failure text; the submission's own
    # (built in its layer) count against its time budget, the pinned incumbent's do not.
    env_extra = {"BENCH_SEED": str(seed), "BENCH_PAIRS": str(_PAIRS)}
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
    rows = [ln.split() for ln in out.splitlines() if len(ln.split()) == 4]
    return {name: (int(ns), int(sum_)) for name, _, ns, sum_ in rows}


def score(src: Path, v: Verdict) -> str | None:
    # Alternates incumbent and submission processes on a fresh seed; checksums must match per class.
    v.seed = int(os.environ.get("BENCH_SEED", secrets.randbelow(2**32)))
    logger.info(f"[score] seed={v.seed} pairs={_PAIRS} rounds={_ROUNDS} cpu={_BENCH_CPU or 'any'}")
    slot = SLOTS[v.slot]
    runs: dict[str, list[dict[str, tuple[int, int]]]] = {"inc": [], "sub": []}
    for r in range(_ROUNDS):
        order = [("inc", slot.pinned, ""), ("sub", BUILD / "bin" / slot.bench, "read")]
        for who, binary, build in order if r % 2 == 0 else order[::-1]:
            res = bench(binary, v.seed, build)
            if isinstance(res, str):
                return res
            runs[who].append(res)
    inc, sub = runs["inc"], runs["sub"]
    for name, (_, want) in inc[0].items():
        if any(s.get(name, (0, None))[1] != want for s in sub):
            return f"checksum mismatch on class {name}: the submission disagrees with the incumbent"
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
    results = HERE / "results"
    results.mkdir(exist_ok=True)
    failed, broken = 0, False
    with open(HERE / "gate.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            check_limits()
            digest = pin(SLOTS[slot])
        except Exception:  # noqa: BLE001 - no limits or no pinned incumbent: nothing can be scored
            logger.exception("[pin] failed")
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
