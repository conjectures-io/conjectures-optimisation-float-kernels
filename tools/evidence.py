"""Evidence pack - runs the whole flow once and writes evidence/EVIDENCE.md from the real outputs."""

from __future__ import annotations

import fcntl
import glob
import json
import os
import platform
import re
import subprocess
import sys
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
import gate  # noqa: E402 - the gate lives at the repository root, one level up
import selftest  # noqa: E402 - so do the expected verdicts

OUT = HERE / "evidence"
# The file exploits/eval's `#eval` writes; it must never appear on the host.
PWNED = Path("/tmp/pwned")


def sh(cmd: list[str], cwd: Path | None = None) -> str:
    # First line of a command's output, or the reason it could not run.
    try:
        out = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=120)
        return (out.stdout or out.stderr).strip().splitlines()[0]
    except Exception as exc:  # noqa: BLE001 - a missing tool is reported, not fatal
        return f"unavailable ({exc})"


def environment(digests: dict[str, str]) -> list[tuple[str, str]]:
    # The machine, toolchain and pins the evidence was produced with.
    manifest = json.loads((gate.LIB / "lake-manifest.json").read_text())
    revs = {p["name"]: p["rev"] for p in manifest["packages"]}
    cpu = next(ln.split(":", 1)[1].strip() for ln in open("/proc/cpuinfo") if "model name" in ln)
    return [
        ("CPU", f"{cpu}, {os.cpu_count()} threads"),
        ("OS", platform.platform()),
        ("Lean", sh([gate.LAKE, "env", "lean", "--version"], cwd=gate.LIB)),
        ("Lake", sh([gate.LAKE, "--version"], cwd=gate.LIB)),
        ("FloatLib", revs.get("floatlib", "?")),
        ("Mathlib", revs.get("mathlib", "?")),
        ("bubblewrap", sh(["bwrap", "--version"])),
        ("Python", platform.python_version()),
        *[(f"pinned `{slot}` incumbent sha256", d) for slot, d in digests.items()],
    ]


def previous() -> dict:
    # The last evidence pack's data, so measurements whose raw logs are gone carry over.
    path = OUT / "evidence.json"
    return json.loads(path.read_text()) if path.exists() else {}


def setup_time() -> str:
    # Wall time of the first from-scratch setup, if its log was kept, else the last pack's.
    log = HERE / "logs" / "setup-first.log"
    m = re.search(r"real\s+(\S+)", log.read_text()) if log.exists() else None
    if m:
        return m.group(1)
    old = previous()
    if old.get("setup") and not old["setup"].startswith("not recorded"):
        # A value already carried over keeps its original date.
        return (
            old["setup"] if "(" in old["setup"] else f"{old['setup']} (measured {old['generated']})"
        )
    return "not recorded (run `./setup.sh` on a fresh clone, log to logs/)"


def timing_study() -> dict | None:
    # The latest completed timing study, if one was run here, else the last pack's.
    done = [
        f
        for f in glob.glob(str(HERE / "results" / "timing-study-*.json"))
        if "summary" in json.loads(Path(f).read_text())
    ]
    if done:
        latest = max(done, key=os.path.getmtime)
        study = json.loads(Path(latest).read_text())
        stamp = datetime.fromtimestamp(os.path.getmtime(latest), timezone.utc)
        return {**study, "measured": f"{stamp:%Y-%m-%d %H:%M UTC}"}
    old = previous()
    study = old.get("timing_study")
    if not study:
        return None
    if "summary" in study:
        return study
    # Packs before 2026-10 stored only the summary; its runs and mean run times give the length.
    minutes = sum(s["runs"] * s["seconds_mean"] for s in study.values()) / 60
    return {"summary": study, "minutes": minutes, "measured": old["generated"]}


def gate_rows(digests: dict[str, str]) -> list[dict]:
    # Every expected-verdict case through the real gate, with the gate's own reason.
    rows = []
    for case in selftest.load():
        v = selftest.run_case(case, digests[case.slot])
        problem = selftest.check(case, v)
        rows.append(
            {
                "case": asdict(case),
                "verdict": asdict(v),
                "as_expected": problem is None,
                "problem": problem,
            }
        )
        logger.info(
            f"[evidence] {case.name:22} {case.slot:4} {case.mode:8} {v.stage:9} ok={problem is None}"
        )
    return rows


def containment(digest: str) -> dict:
    # Runs exploits/eval with policy off: its `#eval` executes in the build, but only inside bwrap.
    before = PWNED.exists()
    case = selftest.Case("exploits/eval", "add", "nopolicy", True, "score", None, None)
    v = selftest.run_case(case, digest)
    return {"host_file_before": before, "host_file_after": PWNED.exists(), "verdict": asdict(v)}


def cell(text: str, limit: int = 110) -> str:
    # A table-safe, shortened cell.
    text = text.replace("|", "/").replace("\n", " ")
    return text if len(text) <= limit else text[: limit - 3] + "..."


def render(env, setup, rows, contain, study, stamp) -> str:
    # The evidence document, entirely from the data above.
    ok = sum(r["as_expected"] for r in rows)
    accepted = [r for r in rows if r["verdict"]["passed"]]
    slots = list(dict.fromkeys(r["case"]["slot"] for r in rows))
    best = {
        slot: min(
            (r for r in accepted if r["case"]["slot"] == slot), key=lambda r: r["verdict"]["ratio"]
        )
        for slot in slots
        if any(r["case"]["slot"] == slot for r in accepted)
    }
    exploits = len({r["case"]["name"] for r in rows if r["case"]["name"].startswith("exploits/")})
    rejected = [r for r in rows if not r["verdict"]["passed"]]
    contained = not contain["host_file_after"]
    lines = [
        "# Evidence - FloatLib kernel competition",
        "",
        "```mermaid",
        "flowchart LR",
        '    M["Kernel.lean"] --> I["0 intake"] --> P["1 policy"] --> S["2 statement<br/>bwrap + overlay"]',
        '    S --> K["3 kernel<br/>leanchecker"] --> A["4 axioms"] --> B["5 score<br/>vs pinned incumbent"]',
        '    B --> V["verdict JSON + log"]',
        "```",
        "",
        "## Quick Summary",
        "",
        f"- Generated {stamp} by `tools/evidence.py` from real gate runs on the machine in section 1; nothing here is typed by hand.",
        f"- **{ok}/{len(rows)}** fixtures across the {', '.join(f'`{s}`' for s in slots)} slots got exactly their expected verdict: {len(accepted)} accepted, {len(rejected)} rejected at the expected stage.",
        "- Best accepted kernel per slot this run: "
        + ", ".join(
            f"`{r['case']['name']}` at **{r['verdict']['ratio']:.3f}x**" for r in best.values()
        )
        + ", each of its own slot's incumbent's time.",
        f"- Sandbox containment: an `#eval` that writes `{PWNED}` ran during the build, and the file {'did **not** appear' if contained else '**appeared**'} on the host.",
    ]
    if study:
        s = study["summary"]
        lines.append(
            f"- Timing (`add`, study of {study['measured']}): over {study['minutes']:.0f} minutes, the incumbent against itself cleared the 0.97 bar "
            f"{s['incumbent']['at_or_below_bar']} of {s['incumbent']['runs']} times, `fastlog` "
            f"{s['fastlog']['at_or_below_bar']} of {s['fastlog']['runs']}."
        )
    lines += [
        "",
        "```bash",
        "export PATH=$HOME/.elan/bin:$PATH",
        ".venv/bin/python tools/evidence.py      # about 10 minutes; rewrites evidence/EVIDENCE.md and evidence.json",
        "```",
        "",
        "## 1. Environment",
        "",
        "| | |",
        "|---|---|",
        *[f"| {k} | {cell(v)} |" for k, v in env],
        "",
        "## 2. Setup from scratch",
        "",
        f"`./setup.sh` on a fresh clone (fetch FloatLib and Mathlib, Mathlib cache, build): **{setup}**.",
        "",
        "## 3. Every fixture through the gate",
        "",
        "Each row is one full `gate.py` pass. `nopolicy` skips the policy stage, to show the stages behind it hold on their own.",
        "",
        "| submission | slot | mode | expected | got | as expected | gate's reason, or time vs incumbent |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        c, v = r["case"], r["verdict"]
        exp = f"{'accept' if c['passed'] else 'reject'} at {c['stage']}"
        got = f"{'accept' if v['passed'] else 'reject'} at {v['stage']}"
        why = f"{v['ratio']:.3f}x" if v["passed"] else cell(v["reason"])
        lines.append(
            f"| `{c['name']}` | {c['slot']} | {c['mode']} | {exp} | {got} | {'yes' if r['as_expected'] else '**NO**'} | {why} |"
        )
    lines += [
        "",
        "## 4. Accepted kernels: timing per operand class",
        "",
        "Nanoseconds per operation, fastest of 25 rounds, 500,000 operand sets per class, fresh seed per run; each ratio is against that slot's incumbent.",
        "",
        "| submission | slot | seed | class | incumbent ns | submission ns | ratio |",
        "|---|---|---:|---|---:|---:|---:|",
    ]
    for r in accepted:
        v = r["verdict"]
        for cls, m in v["classes"].items():
            lines.append(
                f"| `{r['case']['name']}` | {r['case']['slot']} | {v['seed']} | {cls} | {m['incumbent_ns']:.2f} | {m['submission_ns']:.2f} | {m['ratio']:.3f} |"
            )
    cv = contain["verdict"]
    lines += [
        "",
        "## 5. Sandbox containment",
        "",
        '`kernels/exploits/eval` contains `#eval show IO Unit from IO.FS.writeFile "/tmp/pwned" "x"`. With the',
        "policy stage skipped, that code really runs while the gate builds the file. The build runs in",
        "bwrap with a private `/tmp`, no network and a read-only root, so the write lands in the sandbox",
        "and disappears with it.",
        "",
        "| | |",
        "|---|---|",
        f"| `{PWNED}` on the host before | {'exists' if contain['host_file_before'] else 'absent'} |",
        f"| `{PWNED}` on the host after | {'**exists: containment failed**' if contain['host_file_after'] else 'absent: contained'} |",
        f"| gate verdict | {'accept' if cv['passed'] else 'reject'} at {cv['stage']} {cell(cv['reason']) if cv['reason'] else ''} |",
        "",
        "The kernel itself is FloatLib's, so with policy off it is accepted; in real runs policy rejects it",
        "first (section 3, `exploits/eval`, gate mode).",
    ]
    if study:
        lines += [
            "",
            "## 6. Timing stability",
            "",
            f"`tools/timing_study.py` on `add`, measured {study['measured']}: {study['minutes']:.0f} minutes of alternating full gate runs, fresh seed each:",
            "",
            "| submission | runs | mean | stdev | min | max | at or below 0.97 |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ]
        for n, s in study["summary"].items():
            lines.append(
                f"| `{n}` | {s['runs']} | {s['ratio_mean']:.4f} | {s['ratio_stdev']:.4f} | {s['ratio_min']:.4f} | {s['ratio_max']:.4f} | {s['at_or_below_bar']} |"
            )
    lines += [
        "",
        "## 7. What this does not show",
        "",
        "- Other hardware: everything above ran on the machine in section 1. The validator's server needs its own timing study before launch.",
        "- Kernels within about 1.5% of each other cannot be ranked by timing; ties need the earlier-submission rule.",
        f"- Attacks beyond the {exploits} exploit fixtures: the policy is a text scan backed by the sandbox, leanchecker and the axiom audit, not a proof of completeness.",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    # Runs the flow under the gate's lock and writes the evidence document and its data.
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    gate.log_to("evidence", f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}")
    OUT.mkdir(exist_ok=True)
    t0 = time.monotonic()
    with open(HERE / "gate.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        slots = sorted({c.slot for c in selftest.load()})
        digests = {slot: gate.pin(gate.SLOTS[slot]) for slot in slots}
        env = environment(digests)
        rows = gate_rows(digests)
        contain = containment(digests["add"])
    study, setup = timing_study(), setup_time()
    data = {
        "generated": stamp,
        "seconds": round(time.monotonic() - t0),
        "environment": env,
        "setup": setup,
        "rows": rows,
        "containment": contain,
        "timing_study": study,
    }
    (OUT / "evidence.json").write_text(json.dumps(data, indent=2))
    (OUT / "EVIDENCE.md").write_text(render(env, setup, rows, contain, study, stamp))
    failed = [r["case"]["name"] for r in rows if not r["as_expected"]] + (
        ["containment"] if contain["host_file_after"] else []
    )
    logger.info(
        f"[evidence] wrote {OUT / 'EVIDENCE.md'} in {data['seconds']}s, problems: {failed or 'none'}"
    )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
