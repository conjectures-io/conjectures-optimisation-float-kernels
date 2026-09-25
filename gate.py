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

HERE = Path(__file__).resolve().parent
LIB = HERE / "lean"
BUILD = LIB / ".lake" / "build"
CONFIG = LIB / ".lake" / "config"
SCRATCH = HERE / ".sandbox" / "config"
LAYER = HERE / ".sandbox" / "layer"
# Build outputs a submission may write, by file stem: its module, the gates, the bench binaries.
OWN_STEMS = {"Submission", "Verify", "VerifyMul", "VerifyFma", "Main", "MainMul", "MainFma"}
OWN_STEMS |= {"bench", "benchMul", "benchFma"}
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

    @property
    def pinned(self) -> Path:
        # The incumbent's bench binary, built once per change of its inputs.
        return HERE / "pinned" / self.name / "bench_incumbent"

    @property
    def inputs(self) -> list[Path]:
        # Everything the pinned binary is built from.
        return [self.incumbent, LIB / "bench" / self.main] + SHARED_INPUTS


# Each slot's incumbent; point GATE_INCUMBENT[_MUL|_FMA] at a winner to promote it (repins).
SLOTS = {
    "add": Slot(
        "add",
        "Verify",
        "bench",
        "Main.lean",
        Path(os.environ.get("GATE_INCUMBENT", HERE / "submissions" / "incumbent" / "Kernel.lean")),
    ),
    "mul": Slot(
        "mul",
        "VerifyMul",
        "benchMul",
        "MainMul.lean",
        Path(
            os.environ.get(
                "GATE_INCUMBENT_MUL", HERE / "submissions" / "mul-incumbent" / "Kernel.lean"
            )
        ),
    ),
    "fma": Slot(
        "fma",
        "VerifyFma",
        "benchFma",
        "MainFma.lean",
        Path(
            os.environ.get(
                "GATE_INCUMBENT_FMA", HERE / "submissions" / "fma-incumbent" / "Kernel.lean"
            )
        ),
    ),
}
LAKE = str(Path.home() / ".elan" / "bin" / "lake")

_PAIRS = int(os.environ.get("BENCH_PAIRS", "500000"))
_ROUNDS = int(os.environ.get("BENCH_ROUNDS", "25"))
# 0 runs builds and benches without bwrap, for hosts that cannot create user namespaces.
_SANDBOX = os.environ.get("GATE_SANDBOX", "1") != "0"
# The core both benches run on; pinning halves timing noise. Empty runs them unpinned.
_BENCH_CPU = os.environ.get("BENCH_CPU", "3")
_BUILD_TIMEOUT = 1800
_BENCH_TIMEOUT = 300
_MAX_BYTES = 256_000

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
    r"|attribute|IO)\b|unsafe|Unchecked|dbg|#[a-z_]+"
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
    incumbent_sha256: str = ""
    seconds: float = 0.0


# ── Process helpers ──────────────────────────────────────────


def run(cmd: list[str], timeout: float, build: str = "", env: dict | None = None):
    # Runs `cmd` in bwrap with no network and a throwaway Lake config; `build` sets the build mount.
    box = ["bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp"]
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
    argv = (box + ["--"] if _SANDBOX else []) + cmd
    t0 = time.monotonic()
    with subprocess.Popen(
        argv,
        cwd=LIB,
        env=env,
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


def wipe(path: Path) -> None:
    # Removes a scratch tree, including the mode-000 dirs overlayfs leaves in its work dir.
    if path.exists():
        subprocess.run(["chmod", "-R", "u+rwx", str(path)], check=False)
        shutil.rmtree(path, ignore_errors=True)


def foreign(upper: Path) -> list[str]:
    # Files the submission's build wrote or deleted outside its own modules.
    files = (p for p in upper.rglob("*") if not p.is_dir())
    return sorted(str(p.relative_to(upper)) for p in files if p.name.split(".")[0] not in OWN_STEMS)


def errors(out: str) -> str:
    # The first few error lines of a Lean or Lake log, for the verdict reason.
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
    # The submission is one Lean file of bounded size.
    if not src.is_file():
        return f"{src} is missing"
    if src.stat().st_size > _MAX_BYTES:
        return f"{src.stat().st_size} bytes, over {_MAX_BYTES}"
    return None


def policy(src: Path, v: Verdict) -> str | None:
    # Imports Contract only; no escape hatch, elaboration-time code, or unlisted option/attribute.
    text = src.read_text()
    for line in text.splitlines():
        if re.search(r"\bimport\b", line) and line.strip() != "import Contract":
            return f"import other than Contract: {line.strip()!r}"
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
    code, out = run([LAKE, "build", slot.verify], _BUILD_TIMEOUT, build="write")
    if code != 0:
        return f"the obligation does not typecheck: {errors(out)}"
    code, out = run([LAKE, "build", slot.bench], _BUILD_TIMEOUT, build="write")
    if code != 0:
        return f"the bench binary does not build: {errors(out)}"
    if bad := foreign(LAYER / "upper"):
        return f"the build wrote outside the submission's modules: {bad[:5]}"
    logger.debug(f"[statement] layer holds {sum(1 for _ in (LAYER / 'upper').rglob('*'))} entries")
    return None


def kernel(src: Path, v: Verdict) -> str | None:
    # Replays Submission and the slot's gate through the kernel instead of trusting oleans.
    for module in ("Submission", SLOTS[v.slot].verify):
        code, out = run([LAKE, "env", "leanchecker", module], _BUILD_TIMEOUT, build="read")
        if code != 0:
            return f"leanchecker rejects {module}: {out.strip()[-300:]}"
        logger.info(f"[kernel] {module} replays cleanly")
    return None


def axioms(src: Path, v: Verdict) -> str | None:
    # `#print axioms accepted` lists Lean's three and nothing else.
    gate_file = f"verify/{SLOTS[v.slot].verify}.lean"
    code, out = run([LAKE, "env", "lean", gate_file], _BUILD_TIMEOUT, build="read")
    m = re.search(r"depends on axioms: \[([^\]]*)\]", out.replace("\n", " "))
    if code != 0 or not m:
        return f"no axiom report: {errors(out) or out[-300:]}"
    found = {a.strip() for a in m.group(1).split(",") if a.strip()}
    logger.info(f"[axioms] {sorted(found)}")
    return f"extra axioms {sorted(found - AXIOMS)}" if found - AXIOMS else None


def bench(binary: Path, seed: int, build: str) -> dict[str, tuple[int, int]] | str:
    # One bench process: class -> (best ns, checksum), or the failure text.
    env = {**os.environ, "BENCH_SEED": str(seed), "BENCH_PAIRS": str(_PAIRS)}
    pin_cpu = ["taskset", "-c", _BENCH_CPU] if _BENCH_CPU and shutil.which("taskset") else []
    code, out = run(pin_cpu + [str(binary)], _BENCH_TIMEOUT, build=build, env=env)
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
    # Gates one submission on a fresh build layer, which is dropped afterwards.
    v = Verdict(submission=sub_dir.name, slot=slot, mode=mode, incumbent_sha256=digest)
    src, t0 = sub_dir / "Kernel.lean", time.monotonic()
    wipe(LAYER)
    (LAYER / "upper").mkdir(parents=True)
    (LAYER / "work").mkdir()
    try:
        run_stages(src, v, BENCH_STAGES if mode == "bench" else STAGES)
    finally:
        wipe(LAYER)
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


def main() -> int:
    # Gates each submission under one lock; exit 0 all accepted, 1 any rejected, 2 validator error.
    argv, opts = sys.argv[1:], {}
    for flag in ("--verdict", "--slot"):
        if flag in argv:
            i = argv.index(flag)
            opts[flag] = argv[i + 1] if i + 1 < len(argv) else None
            argv = argv[:i] + argv[i + 2 :]
    verdict_path = Path(opts["--verdict"]) if opts.get("--verdict") else None
    slot = opts.get("--slot", "add")
    args = [a for a in argv if a != "--bench"]
    mode = "bench" if "--bench" in argv else "gate"
    bad_verdict = "--verdict" in sys.argv and (verdict_path is None or len(args) != 1)
    if not args or bad_verdict or slot not in SLOTS:
        print("usage: gate.py [--bench] [--slot add|mul|fma] submissions/<name> [...]")
        print("       gate.py <submission dir> [--slot add|mul|fma] --verdict <file.json>")
        return 2
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    logger.add(HERE / "logs" / f"gate-{stamp}.log", level="DEBUG")
    results = HERE / "results"
    results.mkdir(exist_ok=True)
    failed, broken = 0, False
    with open(HERE / "gate.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            digest = pin(SLOTS[slot])
        except Exception:  # noqa: BLE001 - no pinned incumbent means nothing can be scored
            logger.exception("[pin] failed")
            return 2
        for arg in args:
            v = gate(Path(arg).resolve(), digest, mode, slot)
            out = results / f"{v.submission}-{mode}-{stamp}.json"
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
